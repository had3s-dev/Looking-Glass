import asyncio
import base64
import hmac
import hashlib
import html
import io
import posixpath
import urllib.parse
import zipfile
from typing import List, Dict, Optional, Tuple

from aiohttp import web

from .config import Config
from .scanner import SeedboxScanner


class LinkServer:
    def __init__(self, cfg: Config, scanner: SeedboxScanner) -> None:
        self.cfg = cfg
        self.scanner = scanner
        self.app = web.Application()
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.app.add_routes([
            web.get('/links', self.handle_links),
            web.get('/d', self.handle_download),
            web.get('/', self.handle_root),
            web.get('/upload', self.handle_upload_form),
            web.post('/upload', self.handle_upload),
            web.get('/video', self.handle_video_player),
            web.get('/stream', self.handle_video_stream),
            web.get('/info', self.handle_video_info),
            web.get('/test-video', self.handle_test_video),
        ])

    async def start(self):
        if self.runner is not None:
            return
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host=self.cfg.http_host, port=self.cfg.http_port)
        await self.site.start()

    async def stop(self):
        if self.site:
            await self.site.stop()
            self.site = None
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    def _base_url(self) -> str:
        if self.cfg.public_base_url:
            return self.cfg.public_base_url.rstrip('/')
        host = self.cfg.http_host if self.cfg.http_host != '0.0.0.0' else '127.0.0.1'
        return f"http://{host}:{self.cfg.http_port}"

    # ---- Signing helpers ----
    def sign_path(self, path: str, exp_ts: int) -> str:
        secret = (self.cfg.link_secret or 'dev-secret').encode('utf-8')
        token = base64.urlsafe_b64encode(path.encode('utf-8')).decode('utf-8')
        payload = f"{token}.{exp_ts}".encode('utf-8')
        sig = hmac.new(secret, payload, hashlib.sha256).hexdigest()
        return f"{token}.{exp_ts}.{sig}"

    def verify_token(self, token: str) -> Optional[str]:
        try:
            token_b64, exp_s, sig = token.split('.')
            exp_ts = int(exp_s)
        except Exception:
            return None
        import time
        if time.time() > exp_ts:
            return None
        secret = (self.cfg.link_secret or 'dev-secret').encode('utf-8')
        payload = f"{token_b64}.{exp_ts}".encode('utf-8')
        expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        try:
            path = base64.urlsafe_b64decode(token_b64.encode('utf-8')).decode('utf-8')
            return path
        except Exception:
            return None



    # ---- Routes ----
    async def handle_upload_form(self, request: web.Request) -> web.Response:
        content = """
            <!DOCTYPE html>
            <html>
            <head>
                <title>Upload Files</title>
            </head>
            <body>
                <h1>Upload Files</h1>
                <form action="/upload" method="post" enctype="multipart/form-data">
                    <label for="kind">Kind:</label>
                    <select name="kind" id="kind">
                        <option value="books">Books</option>
                        <option value="movies">Movies</option>
                        <option value="tv">TV</option>
                        <option value="music">Music</option>
                    </select>
                    <br><br>
                    <label for="name">Name (Author, Movie Title, etc.):</label>
                    <input type="text" id="name" name="name" required>
                    <br><br>
                    <label for="files">Files:</label>
                    <input type="file" id="files" name="files" multiple>
                    <br><br>
                    <input type="submit" value="Upload">
                </form>
            </body>
            </html>
        """
        return web.Response(text=content, content_type='text/html')

    async def handle_upload(self, request: web.Request) -> web.Response:
        reader = await request.multipart()
        data: Dict[str, str] = {}
        # Collect uploaded files as (filename, bytes) to avoid reading from closed streams later
        files: List[Tuple[str, bytes]] = []
        async for part in reader:
            if part.name == 'files':
                filename = getattr(part, 'filename', None)
                if not filename:
                    # Skip unnamed file parts
                    _ = await part.read()  # drain
                    continue
                # Read the entire file content now; for large files consider chunking in future
                file_bytes = await part.read()
                files.append((filename, file_bytes))
            elif part.name:
                try:
                    data[part.name] = (await part.read()).decode('utf-8')
                except Exception:
                    data[part.name] = ""

        kind = data.get('kind')
        name = data.get('name')

        if not kind or not name or not files:
            return web.Response(status=400, text='Missing kind, name, or files.')

        if kind not in ('books', 'movies', 'tv', 'music'):
            return web.Response(status=400, text='Invalid kind')

        sftp = self.scanner._connect()
        try:
            root_path = ''
            if kind == 'books':
                root_path = self.scanner.root_path
            elif kind == 'movies':
                root_path = self.cfg.movies_root_path or ''
            elif kind == 'tv':
                root_path = self.cfg.tv_root_path or ''
            elif kind == 'music':
                root_path = self.cfg.music_root_path or ''

            if not root_path:
                return web.Response(status=500, text=f"Root path for kind '{kind}' is not configured.")

            # Simplified logic to find/create a directory for the upload
            # For 'books', it might be author name. For others, the name given.
            # This is a simplification. A real implementation might need more robust logic.
            dest_dir_name = name
            dest_path = posixpath.join(root_path, dest_dir_name)

            try:
                sftp.stat(dest_path)
            except FileNotFoundError:
                sftp.mkdir(dest_path)

            for filename, file_data in files:
                if not filename:
                    continue

                if filename.lower().endswith('.zip'):
                    with io.BytesIO(file_data) as bio:
                        with zipfile.ZipFile(bio, 'r') as zipf:
                            for zip_info in zipf.infolist():
                                if zip_info.is_dir():
                                    continue
                                remote_filepath = posixpath.join(dest_path, posixpath.basename(zip_info.filename))
                                with sftp.open(remote_filepath, 'wb') as f:
                                    f.write(zipf.read(zip_info.filename))
                else:
                    remote_filepath = posixpath.join(dest_path, filename)
                    with sftp.open(remote_filepath, 'wb') as f:
                        f.write(file_data)

        finally:
            sftp.close()

        return web.Response(text="Files uploaded successfully.")

    async def handle_root(self, request: web.Request) -> web.Response:
        return web.Response(text="OK", content_type='text/plain')

    async def handle_links(self, request: web.Request) -> web.Response:
        kind = request.query.get('kind', '')
        name = request.query.get('name', '')
        if kind not in ('books', 'movies', 'tv', 'music'):
            return web.Response(status=400, text='Invalid kind')
        if not name:
            return web.Response(status=400, text='Missing name')

        # Collect matching files via SFTP based on kind/name
        files: List[Tuple[str, int]] = []  # (path, size)
        # Delegate to thread pool for SFTP operations
        async def collect():
            return await asyncio.get_running_loop().run_in_executor(None, self._collect_files_sync, kind, name)
        files = await collect()

        base = self._base_url()
        import time
        exp = int(time.time()) + self.cfg.link_ttl_seconds
        items = []
        for path, size in files:
            token = self.sign_path(path, exp)
            url = f"{base}/d?token={urllib.parse.quote(token)}"
            items.append((path.rsplit('/', 1)[-1], url, size))


        # Build video player links for movies and TV
        video_items: List[Tuple[str, str, int]] = []
        if kind in ('movies', 'tv') and self.cfg.enable_video_player:
            try:
                video_items = self.build_video_links(kind, name)
            except Exception:
                video_items = []

        # Simple HTML
        title = f"Links for {html.escape(name)} ({html.escape(kind)})"
        body = [f"<h1>{title}</h1>"]
        
        if not items and not video_items:
            body.append("<p>No files found.</p>")
        else:
            # Video player links for movies and TV
            if video_items:
                body.append("<h2>🎬 Watch Online</h2>")
                body.append("<ul>")
                for filename, url, size in video_items:
                    body.append(f"<li><a href='{html.escape(url)}' target='_blank'>{html.escape(filename)}</a> <small>({size} bytes)</small></li>")
                body.append("</ul>")
            
            # Direct download links
            if items:
                if video_items:
                    body.append("<h2>📁 Direct Downloads</h2>")
                body.append("<ul>")
                for filename, url, size in items:
                    body.append(f"<li><a href='{html.escape(url)}'>{html.escape(filename)}</a> <small>({size} bytes)</small></li>")
                body.append("</ul>")
        
        return web.Response(text="\n".join(body), content_type='text/html')

    def build_links(self, kind: str, name: str) -> List[Tuple[str, str, int]]:
        """
        Build signed direct-download links for a given kind/name selection.
        Returns a list of tuples: (filename, url, size_bytes).
        """
        # Collect matching files via SFTP based on kind/name
        files: List[Tuple[str, int]] = []  # (path, size)
        # Delegate to thread pool not needed here; callers should offload if needed
        files = self._collect_files_sync(kind, name)

        base = self._base_url()
        import time
        exp = int(time.time()) + self.cfg.link_ttl_seconds
        items: List[Tuple[str, str, int]] = []
        for path, size in files:
            token = self.sign_path(path, exp)
            url = f"{base}/d?token={urllib.parse.quote(token)}"
            items.append((path.rsplit('/', 1)[-1], url, size))
        return items

    def build_video_links(self, kind: str, name: str) -> List[Tuple[str, str, int]]:
        """
        Build video player links for Movies/TV selection.
        Returns a list of tuples: (filename, url, size_bytes).
        """
        if kind not in ('movies', 'tv'):
            return []
        
        # Collect matching video files via SFTP
        files: List[Tuple[str, int]] = self._collect_files_sync(kind, name)
        base = self._base_url()
        import time
        exp = int(time.time()) + self.cfg.link_ttl_seconds
        out: List[Tuple[str, str, int]] = []
        
        for path, size in files:
            filename = path.rsplit('/', 1)[-1]
            # Only include video files
            if self._is_video_file(filename):
                token = self.sign_path(path, exp)
                url = f"{base}/video?token={urllib.parse.quote(token)}"
                out.append((filename, url, size))
        
        return out




    def _collect_files_sync(self, kind: str, name: str) -> List[Tuple[str, int]]:
        import posixpath
        import re
        sftp = self.scanner._connect()
        out: List[Tuple[str, int]] = []
        try:
            if kind == 'books':
                # Support two modes:
                # 1) name == "Author | Book" -> return files for that specific book
                # 2) name == "Author" -> return all book files under that author
                author = None
                # Try split
                m = re.match(r"^(.+?)\s*\|\s*(.+)$", name)
                book_title = None
                if m:
                    author = m.group(1).strip()
                    book_title = m.group(2).strip()
                else:
                    author = name.strip()

                # Locate author folder
                author_path = None
                for e in sftp.listdir_attr(self.scanner.root_path):
                    nm = e.filename
                    if nm.lower() == (author or '').lower() or (author or '').lower() in nm.lower():
                        author_path = posixpath.join(self.scanner.root_path, nm)
                        break
                if author_path:
                    # Inside author dir
                    for e in sftp.listdir_attr(author_path):
                        p = posixpath.join(author_path, e.filename)
                        if (e.st_mode & 0o170000) == 0o040000:
                            for f in sftp.listdir_attr(p):
                                if self.scanner._matches_extension(f.filename):
                                    base = self.scanner._strip_extension(f.filename)
                                    if (book_title is None) or (self.scanner._normalize_title(base) == self.scanner._normalize_title(book_title)):
                                        fp = posixpath.join(p, f.filename)
                                        out.append((fp, sftp.stat(fp).st_size))
                        else:
                            if self.scanner._matches_extension(e.filename):
                                base = self.scanner._strip_extension(e.filename)
                                if (book_title is None) or (self.scanner._normalize_title(base) == self.scanner._normalize_title(book_title)):
                                    out.append((p, sftp.stat(p).st_size))
                else:
                    # Fallback to flat root files "Author - Book.ext" when book_title present
                    if book_title is not None:
                        pat = re.compile(r"^(.+?)\s+-\s+(.+)$")
                        for e in sftp.listdir_attr(self.scanner.root_path):
                            if self.scanner._matches_extension(e.filename):
                                base = self.scanner._strip_extension(e.filename)
                                mm = pat.match(base)
                                if mm and self.scanner._normalize_title(mm.group(2)) == self.scanner._normalize_title(book_title):
                                    p = posixpath.join(self.scanner.root_path, e.filename)
                                    out.append((p, sftp.stat(p).st_size))
            elif kind == 'movies':
                root = self.cfg.movies_root_path or ''
                if not root:
                    return out
                target = name.lower()
                for e in sftp.listdir_attr(root):
                    nm = e.filename
                    p = posixpath.join(root, nm)
                    if (e.st_mode & 0o170000) == 0o040000:
                        if target in nm.lower():
                            # collect video files under dir
                            for f in sftp.listdir_attr(p):
                                if any(f.filename.lower().endswith(ext) for ext in self.cfg.movie_extensions):
                                    fp = posixpath.join(p, f.filename)
                                    out.append((fp, sftp.stat(fp).st_size))
                    else:
                        if any(nm.lower().endswith(ext) for ext in self.cfg.movie_extensions) and target in self.scanner._strip_any_ext(nm, self.cfg.movie_extensions).lower():
                            out.append((p, sftp.stat(p).st_size))
            elif kind == 'tv':
                root = self.cfg.tv_root_path or ''
                if not root:
                    return out
                target = name.lower()
                for show in sftp.listdir_attr(root):
                    show_name = show.filename
                    show_path = posixpath.join(root, show_name)
                    if target not in show_name.lower():
                        continue
                    if (show.st_mode & 0o170000) == 0o040000:
                        for e in sftp.listdir_attr(show_path):
                            p = posixpath.join(show_path, e.filename)
                            if (e.st_mode & 0o170000) == 0o040000:
                                for f in sftp.listdir_attr(p):
                                    if any(f.filename.lower().endswith(ext) for ext in self.cfg.tv_extensions):
                                        fp = posixpath.join(p, f.filename)
                                        out.append((fp, sftp.stat(fp).st_size))
                            else:
                                if any(e.filename.lower().endswith(ext) for ext in self.cfg.tv_extensions):
                                    out.append((p, sftp.stat(p).st_size))
            elif kind == 'music':
                root = self.cfg.music_root_path or ''
                if not root:
                    return out
                target = name.lower()
                for artist in sftp.listdir_attr(root):
                    art_name = artist.filename
                    art_path = posixpath.join(root, art_name)
                    if target not in art_name.lower():
                        continue
                    if (artist.st_mode & 0o170000) == 0o040000:
                        for e in sftp.listdir_attr(art_path):
                            p = posixpath.join(art_path, e.filename)
                            if (e.st_mode & 0o170000) == 0o040000:
                                for f in sftp.listdir_attr(p):
                                    if any(f.filename.lower().endswith(ext) for ext in self.cfg.music_extensions):
                                        fp = posixpath.join(p, f.filename)
                                        out.append((fp, sftp.stat(fp).st_size))
                            else:
                                if any(e.filename.lower().endswith(ext) for ext in self.cfg.music_extensions):
                                    out.append((p, sftp.stat(p).st_size))
        finally:
            try:
                sftp.close()
            except Exception:
                pass
        # Deduplicate
        seen = set()
        uniq: List[Tuple[str, int]] = []
        for p, s in out:
            if p not in seen:
                seen.add(p)
                uniq.append((p, s))
        return uniq

    async def handle_download(self, request: web.Request) -> web.StreamResponse:
        token = request.query.get('token')
        if not token:
            return web.Response(status=400, text='Missing token')
        path = self.verify_token(token)
        if not path:
            return web.Response(status=403, text='Invalid or expired token')

        # Stream file via SFTP in background thread
        resp = web.StreamResponse(status=200, reason='OK', headers={"Content-Type": "application/octet-stream"})
        filename = path.rsplit('/', 1)[-1]
        resp.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}"
        await resp.prepare(request)

        loop = asyncio.get_running_loop()

        queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        stop_flag = {"stop": False}

        def producer():
            import paramiko
            sftp = None
            try:
                sftp = self.scanner._connect()
                with sftp.open(path, 'rb') as f:
                    while not stop_flag["stop"]:
                        chunk = f.read(64 * 1024)
                        if not chunk:
                            break
                        # push to asyncio queue
                        fut = asyncio.run_coroutine_threadsafe(queue.put(chunk), loop)
                        try:
                            fut.result()
                        except Exception:
                            break
            finally:
                try:
                    fut = asyncio.run_coroutine_threadsafe(queue.put(None), loop)  # type: ignore
                    fut.result(timeout=2)
                except Exception:
                    pass
                try:
                    if sftp:
                        sftp.close()
                except Exception:
                    pass

        # Start producer in background thread
        producer_future = loop.run_in_executor(None, producer)

        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                try:
                    await resp.write(chunk)
                except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
                    # Client disconnected; stop producer and exit gracefully
                    stop_flag["stop"] = True
                    break
        finally:
            try:
                await resp.write_eof()
            except Exception:
                pass
        return resp

    # ---- Video Player Routes ----
    
    def _is_video_file(self, filename: str) -> bool:
        """Check if file is a supported video format"""
        video_extensions = ['.mp4', '.mkv', '.webm', '.mov', '.avi']
        return any(filename.lower().endswith(ext) for ext in video_extensions)
    
    def _get_video_mime_type(self, filename: str) -> str:
        """Get MIME type for video file"""
        lower = filename.lower()
        if lower.endswith('.mp4') or lower.endswith('.m4v'):
            return 'video/mp4'
        elif lower.endswith('.webm'):
            return 'video/webm'
        elif lower.endswith('.mov'):
            return 'video/quicktime'
        elif lower.endswith('.mkv'):
            return 'video/x-matroska'
        elif lower.endswith('.avi'):
            return 'video/x-msvideo'
        return 'video/mp4'  # Default fallback
    
    def _needs_transcoding(self, filename: str) -> bool:
        """Check if file needs transcoding for browser compatibility"""
        lower = filename.lower()
        # MKV and AVI typically need transcoding for browser playback
        return lower.endswith('.mkv') or lower.endswith('.avi')
    
    async def handle_video_player(self, request: web.Request) -> web.Response:
        """Serve the video player HTML page"""
        token = request.query.get('token')
        if not token:
            return web.Response(status=400, text='Missing token')
        
        path = self.verify_token(token)
        if not path:
            return web.Response(status=403, text='Invalid or expired token')
        
        filename = path.rsplit('/', 1)[-1]
        if not self._is_video_file(filename):
            return web.Response(status=400, text='File is not a supported video format')
        
        base_url = self._base_url()
        stream_url = f"{base_url}/stream?token={urllib.parse.quote(token)}"
        info_url = f"{base_url}/info?token={urllib.parse.quote(token)}"
        
        # Modern video player HTML with better UX
        html_content = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Playing: {html.escape(filename)}</title>
            <style>
                * {{
                    margin: 0;
                    padding: 0;
                    box-sizing: border-box;
                }}
                
                body {{
                    background: #0a0a0a;
                    color: #ffffff;
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    overflow: hidden;
                }}
                
                .player-container {{
                    position: relative;
                    width: 100vw;
                    height: 100vh;
                    display: flex;
                    flex-direction: column;
                    background: #000;
                }}
                
                .video-wrapper {{
                    flex: 1;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    position: relative;
                }}
                
                video {{
                    width: 100%;
                    height: 100%;
                    object-fit: contain;
                    background: #000;
                }}
                
                .controls {{
                    position: absolute;
                    bottom: 0;
                    left: 0;
                    right: 0;
                    background: linear-gradient(transparent, rgba(0,0,0,0.8));
                    padding: 20px;
                    display: flex;
                    align-items: center;
                    gap: 15px;
                    opacity: 0;
                    transition: opacity 0.3s ease;
                }}
                
                .player-container:hover .controls {{
                    opacity: 1;
                }}
                
                .play-pause {{
                    background: #ff6b6b;
                    border: none;
                    color: white;
                    width: 50px;
                    height: 50px;
                    border-radius: 50%;
                    font-size: 18px;
                    cursor: pointer;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    transition: background 0.2s ease;
                }}
                
                .play-pause:hover {{
                    background: #ff5252;
                }}
                
                .progress-container {{
                    flex: 1;
                    height: 6px;
                    background: rgba(255,255,255,0.3);
                    border-radius: 3px;
                    cursor: pointer;
                    position: relative;
                }}
                
                .progress-bar {{
                    height: 100%;
                    background: #ff6b6b;
                    border-radius: 3px;
                    width: 0%;
                    transition: width 0.1s ease;
                }}
                
                .time-display {{
                    color: #ccc;
                    font-size: 14px;
                    min-width: 100px;
                    text-align: center;
                }}
                
                .volume-container {{
                    display: flex;
                    align-items: center;
                    gap: 10px;
                }}
                
                .volume-slider {{
                    width: 80px;
                    height: 4px;
                    background: rgba(255,255,255,0.3);
                    border-radius: 2px;
                    outline: none;
                    cursor: pointer;
                }}
                
                .fullscreen {{
                    background: none;
                    border: none;
                    color: #ccc;
                    font-size: 20px;
                    cursor: pointer;
                    padding: 5px;
                }}
                
                .loading {{
                    position: absolute;
                    top: 50%;
                    left: 50%;
                    transform: translate(-50%, -50%);
                    color: #ccc;
                    font-size: 16px;
                }}
                
                .error {{
                    position: absolute;
                    top: 50%;
                    left: 50%;
                    transform: translate(-50%, -50%);
                    color: #ff6b6b;
                    text-align: center;
                    max-width: 400px;
                }}
                
                .download-btn {{
                    position: absolute;
                    top: 20px;
                    right: 20px;
                    background: rgba(0,0,0,0.7);
                    color: white;
                    border: 1px solid #555;
                    padding: 10px 20px;
                    border-radius: 5px;
                    text-decoration: none;
                    font-size: 14px;
                    transition: background 0.2s ease;
                }}
                
                .download-btn:hover {{
                    background: rgba(0,0,0,0.9);
                }}
            </style>
        </head>
        <body>
            <div class="player-container">
                <div class="video-wrapper">
                    <video id="videoPlayer" preload="metadata" controls>
                        <source src="{html.escape(stream_url)}" type="{self._get_video_mime_type(filename)}">
                        Your browser does not support the video tag.
                    </video>
                    <div class="loading" id="loading">Loading video...</div>
                    <div class="error" id="error" style="display: none;"></div>
                </div>
                
                <div class="controls">
                    <button class="play-pause" id="playPause">▶</button>
                    <div class="progress-container" id="progressContainer">
                        <div class="progress-bar" id="progressBar"></div>
                    </div>
                    <div class="time-display" id="timeDisplay">0:00 / 0:00</div>
                    <div class="volume-container">
                        <span>🔊</span>
                        <input type="range" class="volume-slider" id="volumeSlider" min="0" max="1" step="0.1" value="1">
                    </div>
                    <button class="fullscreen" id="fullscreen">⛶</button>
                </div>
                
                <a href="{base_url}/d?token={urllib.parse.quote(token)}" class="download-btn">Download</a>
                <a href="{base_url}/test-video?token={urllib.parse.quote(token)}" class="download-btn" style="right: 120px;">Debug</a>
            </div>
            
            <script>
                const video = document.getElementById('videoPlayer');
                const playPause = document.getElementById('playPause');
                const progressBar = document.getElementById('progressBar');
                const progressContainer = document.getElementById('progressContainer');
                const timeDisplay = document.getElementById('timeDisplay');
                const volumeSlider = document.getElementById('volumeSlider');
                const fullscreen = document.getElementById('fullscreen');
                const loading = document.getElementById('loading');
                const error = document.getElementById('error');
                
                let isPlaying = false;
                
                // Play/Pause functionality
                playPause.addEventListener('click', () => {{
                    if (isPlaying) {{
                        video.pause();
                    }} else {{
                        video.play();
                    }}
                }});
                
                video.addEventListener('play', () => {{
                    isPlaying = true;
                    playPause.textContent = '⏸';
                    loading.style.display = 'none';
                }});
                
                video.addEventListener('pause', () => {{
                    isPlaying = false;
                    playPause.textContent = '▶';
                }});
                
                // Progress bar
                video.addEventListener('timeupdate', () => {{
                    const progress = (video.currentTime / video.duration) * 100;
                    progressBar.style.width = progress + '%';
                    
                    const currentTime = formatTime(video.currentTime);
                    const duration = formatTime(video.duration);
                    timeDisplay.textContent = `${{currentTime}} / ${{duration}}`;
                }});
                
                progressContainer.addEventListener('click', (e) => {{
                    const rect = progressContainer.getBoundingClientRect();
                    const clickX = e.clientX - rect.left;
                    const width = rect.width;
                    const clickTime = (clickX / width) * video.duration;
                    video.currentTime = clickTime;
                }});
                
                // Volume control
                volumeSlider.addEventListener('input', (e) => {{
                    video.volume = e.target.value;
                }});
                
                // Fullscreen
                fullscreen.addEventListener('click', () => {{
                    if (document.fullscreenElement) {{
                        document.exitFullscreen();
                    }} else {{
                        video.requestFullscreen();
                    }}
                }});
                
                // Error handling
                video.addEventListener('error', (e) => {{
                    loading.style.display = 'none';
                    error.style.display = 'block';
                    error.innerHTML = 'Error loading video. Please try downloading the file instead.';
                }});
                
                video.addEventListener('loadeddata', () => {{
                    loading.style.display = 'none';
                }});
                
                // Keyboard shortcuts
                document.addEventListener('keydown', (e) => {{
                    switch(e.code) {{
                        case 'Space':
                            e.preventDefault();
                            if (isPlaying) video.pause();
                            else video.play();
                            break;
                        case 'ArrowLeft':
                            video.currentTime -= 10;
                            break;
                        case 'ArrowRight':
                            video.currentTime += 10;
                            break;
                        case 'KeyF':
                            if (document.fullscreenElement) {{
                                document.exitFullscreen();
                            }} else {{
                                video.requestFullscreen();
                            }}
                            break;
                    }}
                }});
                
                function formatTime(seconds) {{
                    const mins = Math.floor(seconds / 60);
                    const secs = Math.floor(seconds % 60);
                    return `${{mins}}:${{secs.toString().padStart(2, '0')}}`;
                }}
                
                // Auto-hide controls
                let controlsTimeout;
                const controls = document.querySelector('.controls');
                
                function showControls() {{
                    controls.style.opacity = '1';
                    clearTimeout(controlsTimeout);
                    controlsTimeout = setTimeout(() => {{
                        if (isPlaying) {{
                            controls.style.opacity = '0';
                        }}
                    }}, 3000);
                }}
                
                document.addEventListener('mousemove', showControls);
                video.addEventListener('play', showControls);
            </script>
        </body>
        </html>
        """
        
        return web.Response(text=html_content, content_type='text/html')
    
    async def handle_video_info(self, request: web.Request) -> web.Response:
        """Get video file information for the player"""
        token = request.query.get('token')
        if not token:
            return web.Response(status=400, text='Missing token')
        
        path = self.verify_token(token)
        if not path:
            return web.Response(status=403, text='Invalid or expired token')
        
        filename = path.rsplit('/', 1)[-1]
        if not self._is_video_file(filename):
            return web.Response(status=400, text='File is not a supported video format')
        
        # Get file info via SFTP
        loop = asyncio.get_running_loop()
        
        def get_file_info():
            sftp = None
            try:
                sftp = self.scanner._connect()
                stat = sftp.stat(path)
                return {
                    'filename': filename,
                    'size': stat.st_size,
                    'mime_type': self._get_video_mime_type(filename),
                    'needs_transcoding': self._needs_transcoding(filename)
                }
            except Exception as e:
                raise Exception(f"Failed to get file info: {str(e)}")
            finally:
                if sftp:
                    sftp.close()
        
        try:
            info = await loop.run_in_executor(None, get_file_info)
            return web.json_response(info)
        except Exception as e:
            return web.Response(status=500, text=str(e))
    
    async def handle_video_stream(self, request: web.Request) -> web.StreamResponse:
        """Stream video file with proper transcoding if needed"""
        token = request.query.get('token')
        if not token:
            return web.Response(status=400, text='Missing token')
        
        path = self.verify_token(token)
        if not path:
            return web.Response(status=403, text='Invalid or expired token')
        
        filename = path.rsplit('/', 1)[-1]
        if not self._is_video_file(filename):
            return web.Response(status=400, text='File is not a supported video format')
        
        # Check if we need transcoding for browser compatibility
        if self._needs_transcoding(filename) and self.cfg.enable_video_player:
            return await self._stream_with_transcoding(path, filename, request)
        else:
            return await self._stream_direct(path, filename, request)
    
    async def _stream_direct(self, path: str, filename: str, request: web.Request) -> web.StreamResponse:
        """Stream video file directly without transcoding"""
        mime_type = self._get_video_mime_type(filename)
        
        # Get file size and basic info
        loop = asyncio.get_running_loop()
        
        def get_file_info():
            sftp = None
            try:
                sftp = self.scanner._connect()
                stat = sftp.stat(path)
                return stat.st_size
            except Exception as e:
                raise Exception(f"Failed to get file info: {str(e)}")
            finally:
                if sftp:
                    sftp.close()
        
        try:
            file_size = await loop.run_in_executor(None, get_file_info)
        except Exception as e:
            return web.Response(status=404, text=f'File not found: {str(e)}')
        
        # Handle range requests for video seeking
        range_header = request.headers.get('Range')
        start = 0
        end = file_size - 1
        status = 200
        
        # Set proper headers for video streaming
        headers = {
            'Content-Type': mime_type,
            'Accept-Ranges': 'bytes',
            'Content-Disposition': f"inline; filename*=UTF-8''{urllib.parse.quote(filename)}",
            'Cache-Control': 'public, max-age=3600',
            'X-Content-Type-Options': 'nosniff',
        }
        
        if range_header and range_header.startswith('bytes='):
            try:
                spec = range_header.split('=')[1]
                s, e = spec.split('-')
                if s:
                    start = int(s)
                if e:
                    end = int(e)
                if end >= file_size:
                    end = file_size - 1
                if start > end:
                    return web.Response(status=416, text='Requested Range Not Satisfiable')
                status = 206
                headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
                headers['Content-Length'] = str(end - start + 1)
            except Exception as e:
                # If range parsing fails, serve the whole file
                headers['Content-Length'] = str(file_size)
        else:
            headers['Content-Length'] = str(file_size)
        
        resp = web.StreamResponse(status=status, reason='OK', headers=headers)
        await resp.prepare(request)
        
        # Stream file content with better error handling
        queue = asyncio.Queue(maxsize=5)  # Smaller queue for better memory management
        stop_flag = {"stop": False}
        
        def producer():
            sftp = None
            try:
                sftp = self.scanner._connect()
                with sftp.open(path, 'rb') as f:
                    if start > 0:
                        f.seek(start)
                    pos = start
                    limit = end
                    while not stop_flag['stop'] and pos <= limit:
                        to_read = min(128 * 1024, (limit - pos + 1))  # Larger chunks
                        chunk = f.read(to_read)
                        if not chunk:
                            break
                        pos += len(chunk)
                        fut = asyncio.run_coroutine_threadsafe(queue.put(chunk), loop)
                        try:
                            fut.result(timeout=5)  # Add timeout
                        except Exception:
                            break
            except Exception as e:
                # Put error in queue
                try:
                    fut = asyncio.run_coroutine_threadsafe(queue.put(f"ERROR:{str(e)}"), loop)
                    fut.result(timeout=2)
                except Exception:
                    pass
            finally:
                try:
                    fut = asyncio.run_coroutine_threadsafe(queue.put(None), loop)
                    fut.result(timeout=2)
                except Exception:
                    pass
                if sftp:
                    sftp.close()
        
        producer_future = loop.run_in_executor(None, producer)
        
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                if isinstance(chunk, str) and chunk.startswith("ERROR:"):
                    return web.Response(status=500, text=f"Streaming error: {chunk[6:]}")
                try:
                    await resp.write(chunk)
                except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
                    stop_flag['stop'] = True
                    break
        finally:
            stop_flag['stop'] = True
            try:
                await producer_future
            except Exception:
                pass
            try:
                await resp.write_eof()
            except Exception:
                pass
        
        return resp
    
    async def handle_test_video(self, request: web.Request) -> web.Response:
        """Test endpoint to debug video streaming issues"""
        token = request.query.get('token')
        if not token:
            return web.Response(status=400, text='Missing token')
        
        path = self.verify_token(token)
        if not path:
            return web.Response(status=403, text='Invalid or expired token')
        
        filename = path.rsplit('/', 1)[-1]
        
        # Get file info
        loop = asyncio.get_running_loop()
        
        def get_file_info():
            sftp = None
            try:
                sftp = self.scanner._connect()
                stat = sftp.stat(path)
                return {
                    'filename': filename,
                    'size': stat.st_size,
                    'mime_type': self._get_video_mime_type(filename),
                    'is_video': self._is_video_file(filename),
                    'needs_transcoding': self._needs_transcoding(filename)
                }
            except Exception as e:
                return {'error': str(e)}
            finally:
                if sftp:
                    sftp.close()
        
        try:
            info = await loop.run_in_executor(None, get_file_info)
            
            # Create a simple test page
            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head><title>Video Test - {html.escape(filename)}</title></head>
            <body>
                <h1>Video Test: {html.escape(filename)}</h1>
                <pre>{html.escape(str(info))}</pre>
                <h2>Test Links:</h2>
                <ul>
                    <li><a href="/stream?token={token}" target="_blank">Direct Stream</a></li>
                    <li><a href="/video?token={token}" target="_blank">Video Player</a></li>
                    <li><a href="/d?token={token}">Download</a></li>
                </ul>
                <h2>Browser Test:</h2>
                <video controls width="800" height="450">
                    <source src="/stream?token={token}" type="{info.get('mime_type', 'video/mp4')}">
                    Your browser does not support the video tag.
                </video>
            </body>
            </html>
            """
            return web.Response(text=html_content, content_type='text/html')
        except Exception as e:
            return web.Response(status=500, text=f"Test failed: {str(e)}")
    
    async def _stream_with_transcoding(self, path: str, filename: str, request: web.Request) -> web.StreamResponse:
        """Stream video with FFmpeg transcoding for browser compatibility"""
        if not self.cfg.enable_video_player:
            return web.Response(status=403, text='Video player disabled')
        
        # Prepare response headers for MP4 output
        headers = {
            'Content-Type': 'video/mp4',
            'Content-Disposition': f"inline; filename*=UTF-8''{urllib.parse.quote(filename.rsplit('.', 1)[0] + '.mp4')}",
            'Accept-Ranges': 'none',  # Can't seek in transcoded stream
            'Cache-Control': 'no-cache',  # Don't cache transcoded content
        }
        
        resp = web.StreamResponse(status=200, reason='OK', headers=headers)
        await resp.prepare(request)
        
        loop = asyncio.get_running_loop()
        
        # Optimized FFmpeg command for MKV to MP4 transcoding
        cmd = [
            self.cfg.ffmpeg_path,
            '-hide_banner', '-loglevel', 'error', '-stats',
            '-i', 'pipe:0',
            '-c:v', 'libx264',  # H.264 for broad compatibility
            '-preset', 'ultrafast',  # Fastest encoding for streaming
            '-crf', '28',       # Higher CRF for faster encoding (slightly lower quality)
            '-maxrate', '2M',   # Limit bitrate for faster streaming
            '-bufsize', '4M',   # Buffer size
            '-c:a', 'aac',      # AAC audio
            '-b:a', '128k',     # Audio bitrate
            '-ac', '2',         # Stereo audio
            '-movflags', '+faststart+frag_keyframe+empty_moov',  # Progressive download
            '-f', 'mp4',
            '-avoid_negative_ts', 'make_zero',  # Fix timestamp issues
            'pipe:1',
        ]
        
        async def run_ffmpeg():
            # Launch FFmpeg process
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            # Producer: read from SFTP and feed FFmpeg
            stop_flag = {'stop': False}
            
            def feeder():
                sftp = None
                try:
                    sftp = self.scanner._connect()
                    with sftp.open(path, 'rb') as f:
                        while not stop_flag['stop']:
                            chunk = f.read(256 * 1024)  # Larger chunks for transcoding
                            if not chunk:
                                break
                            try:
                                if proc.stdin is not None:
                                    proc.stdin.write(chunk)
                                    proc.stdin.flush()  # Ensure data is sent
                            except Exception:
                                break
                except Exception as e:
                    print(f"Feeder error: {e}")
                finally:
                    try:
                        if proc.stdin:
                            proc.stdin.close()
                    except Exception:
                        pass
                    if sftp:
                        sftp.close()
            
            feeder_future = loop.run_in_executor(None, feeder)
            
            try:
                # Consumer: stream FFmpeg output to client
                if proc.stdout is not None:
                    while True:
                        chunk = await proc.stdout.read(128 * 1024)  # Larger output chunks
                        if not chunk:
                            break
                        try:
                            await resp.write(chunk)
                        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
                            stop_flag['stop'] = True
                            break
            except Exception as e:
                print(f"Consumer error: {e}")
            finally:
                stop_flag['stop'] = True
                try:
                    await feeder_future
                except Exception:
                    pass
                try:
                    if proc.stdout:
                        proc.stdout.close()
                except Exception:
                    pass
                try:
                    if proc.stderr:
                        proc.stderr.close()
                except Exception:
                    pass
                try:
                    if proc.stdin:
                        proc.stdin.close()
                except Exception:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
        
        try:
            await run_ffmpeg()
        finally:
            try:
                await resp.write_eof()
            except Exception:
                pass
        
        return resp

