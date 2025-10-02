import asyncio
import base64
import hmac
import hashlib
import html
import io
import posixpath
import time
import urllib.parse
import zipfile
from typing import List, Dict, Optional, Tuple

from aiohttp import web
import aiohttp

from .config import Config
from .scanner import SeedboxScanner


class LinkServer:
    def __init__(self, cfg: Config, scanner: SeedboxScanner) -> None:
        self.cfg = cfg
        self.scanner = scanner
        self.app = web.Application()
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        routes = [
            web.get('/links', self.handle_links),
            web.get('/d', self.handle_download),
            web.get('/', self.handle_root),
            web.get('/upload', self.handle_upload_form),
            web.post('/upload', self.handle_upload),
            web.get('/video', self.handle_video_player),
            web.get('/stream', self.handle_video_stream),
            web.get('/info', self.handle_video_info),
            web.get('/subtitle', self.handle_subtitle),
            web.get('/test-video', self.handle_test_video),
        ]
        self.app.add_routes(routes)

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

    def _is_admin(self, request: web.Request) -> bool:
        token = request.headers.get('x-admin-token') or request.query.get('token') or (request.cookies.get('admin_token') if request.cookies else None)
        expected = getattr(self.cfg, 'admin_token', None)
        return bool(expected and token and hmac.compare_digest(expected, token))

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

    # --- Multi-tenant admin helpers ---
    # Removed tenant endpoints in single-tenant mode
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

    # --- Public Onboarding (OAuth) ---

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
        verified = self.verify_token(token)
        if not verified:
            return web.Response(status=403, text='Invalid or expired token')
        path = verified

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
    
    def _get_original_mime_type(self, filename: str) -> str:
        """Return the MIME type that matches the file's original container."""
        lower = filename.lower()
        if lower.endswith('.mp4') or lower.endswith('.m4v'):
            return 'video/mp4'
        if lower.endswith('.webm'):
            return 'video/webm'
        if lower.endswith('.mov'):
            return 'video/quicktime'
        if lower.endswith('.mkv'):
            return 'video/x-matroska'
        if lower.endswith('.avi'):
            return 'video/x-msvideo'
        return 'application/octet-stream'

    def _get_video_mime_type(self, filename: str) -> str:
        """Prefer MP4 container for best compatibility (remux/transcode target)."""
        lower = filename.lower()
        if lower.endswith('.mp4') or lower.endswith('.m4v'):
            return 'video/mp4'
        if lower.endswith('.webm'):
            return 'video/webm'
        if lower.endswith('.mov'):
            return 'video/quicktime'
        return 'video/mp4'
    
    def _needs_transcoding(self, filename: str) -> bool:
        """Conservative: MP4/M4V direct, others prefer remux/transcode path."""
        lower = filename.lower()
        return not (lower.endswith('.mp4') or lower.endswith('.m4v'))
    
    def _find_subtitle_files(self, video_path: str) -> List[Dict[str, str]]:
        """Find sidecar subtitles next to the remote video via SFTP."""
        import posixpath as _pp
        out: List[Dict[str, str]] = []
        sftp = None
        try:
            sftp = self.scanner._connect()
            video_dir = _pp.dirname(video_path)
            video_name = _pp.splitext(_pp.basename(video_path))[0]
            for e in sftp.listdir_attr(video_dir):
                name = e.filename
                base, ext = _pp.splitext(name)
                ext = ext.lower()
                if base != video_name:
                    continue
                if ext not in ('.srt', '.vtt', '.ass', '.ssa'):
                    continue
                lang = 'en'
                low = name.lower()
                for code in ('en','es','fr','de','it','pt','ru','ja','ko','zh'):
                    if f'.{code}.' in low or low.endswith(f'.{code}{ext}'):
                        lang = code
                        break
                out.append({
                    'path': _pp.join(video_dir, name),
                    'language': lang,
                    'label': f"Subtitle ({lang.upper()})",
                    'extension': ext,
                })
        except Exception:
            pass
        finally:
            try:
                if sftp:
                    sftp.close()
            except Exception:
                pass
        return out
    
    def _generate_subtitle_tracks(self, subtitle_files: List[Dict[str, str]], token: str, base_url: str) -> str:
        """Generate HTML for subtitle tracks"""
        if not subtitle_files:
            return ""
        
        tracks_html = []
        for i, subtitle_file in enumerate(subtitle_files):
            subtitle_url = f"{base_url}/subtitle?token={urllib.parse.quote(token)}&lang={subtitle_file['language']}"
            tracks_html.append(
                f'<track kind="subtitles" src="{html.escape(subtitle_url)}" '
                f'srclang="{subtitle_file["language"]}" label="{html.escape(subtitle_file["label"])}" '
                f'{"default" if i == 0 else ""}>'
            )
        
        return '\n                        '.join(tracks_html)
    
    async def handle_video_player(self, request: web.Request) -> web.Response:
        """Serve the video player HTML page using Video.js"""
        token = request.query.get('token')
        if not token:
            return web.Response(status=400, text='Missing token')
        
        verified = self.verify_token(token)
        if not verified:
            return web.Response(status=403, text='Invalid or expired token')
        path = verified
        
        filename = path.rsplit('/', 1)[-1]
        if not self._is_video_file(filename):
            return web.Response(status=400, text='File is not a supported video format')
        
        base_url = self._base_url()
        default_quality = 'direct' if filename.lower().endswith(('.mp4', '.m4v')) else 'remux'
        stream_url = f"{base_url}/stream?token={urllib.parse.quote(token)}&quality={urllib.parse.quote(default_quality)}"
        
        # Find subtitle files
        subtitle_files = self._find_subtitle_files(path)
        
        # Video.js player HTML - much more reliable than custom implementation
        html_content = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Playing: {html.escape(filename)}</title>
            
            <!-- Video.js CSS -->
            <link href="https://vjs.zencdn.net/8.6.1/video-js.css" rel="stylesheet">
            <style>
                * {{
                    margin: 0;
                    padding: 0;
                    box-sizing: border-box;
                }}
                
                body {{
                    background: #000;
                    color: #fff;
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    overflow: hidden;
                }}
                
                .player-container {{
                    width: 100vw;
                    height: 100vh;
                    display: flex;
                    flex-direction: column;
                    background: #000;
                }}
                
                .video-wrapper {{
                    flex: 1;
                    position: relative;
                }}
                
                .video-js {{
                    width: 100% !important;
                    height: 100% !important;
                }}
                
                .download-btn {{
                    position: absolute;
                    top: 20px;
                    right: 20px;
                    background: rgba(0,0,0,0.8);
                    color: white;
                    border: 1px solid #555;
                    padding: 10px 20px;
                    border-radius: 5px;
                    text-decoration: none;
                    font-size: 14px;
                    transition: background 0.2s ease;
                    z-index: 1000;
                }}
                
                .download-btn:hover {{
                    background: rgba(0,0,0,0.9);
                }}
                
                
                .loading {{
                    position: absolute;
                    top: 50%;
                    left: 50%;
                    transform: translate(-50%, -50%);
                    color: #fff;
                    font-size: 18px;
                    z-index: 100;
                }}
                .controls {{ position: absolute; left: 20px; bottom: 20px; display: flex; gap: 12px; z-index: 1001; }}
                .controls select, .controls button {{ background: rgba(0,0,0,0.7); color:#fff; border: 1px solid #555; padding: 8px 12px; border-radius: 4px; }}
            </style>
        </head>
        <body>
            <div class="player-container">
                <div class="video-wrapper">
                    <video-js
                        id="videoPlayer"
                        class="vjs-default-skin"
                        controls
                        preload="auto"
                        data-setup='{{}}'>
                        <!-- Initial source; omit type so the browser relies on response headers -->
                        <source src="{html.escape(stream_url)}">
                        {self._generate_subtitle_tracks(subtitle_files, token, base_url)}
                        <p class="vjs-no-js">
                            To view this video please enable JavaScript, and consider upgrading to a web browser that
                            <a href="https://videojs.com/html5-video-support/" target="_blank">supports HTML5 video</a>.
                        </p>
                    </video-js>
                    <div class="loading" id="loading">Loading video...</div>
                    <div class="controls"> 
                        <select id="quality"> 
                            <option value="direct">Direct</option> 
                            <option value="remux">Original (Remux)</option> 
                            <option value="1080p">1080p</option> 
                            <option value="720p">720p</option> 
                            <option value="480p">480p</option> 
                        </select> 
                        <button id="apply">Apply</button> 
                    </div>
                </div>
                
                <a href="{base_url}/d?token={urllib.parse.quote(token)}" class="download-btn">Download</a>
            </div>
            
            <!-- Video.js JavaScript -->
            <script src="https://vjs.zencdn.net/8.6.1/video.min.js"></script>
            
            <script>
                // Initialize Video.js player
                const player = videojs('videoPlayer', {{
                    fluid: true,
                    responsive: true,
                    html5: {{
                        vhs: {{
                            overrideNative: true
                        }},
                        nativeVideoTracks: false,
                        nativeAudioTracks: false,
                        nativeTextTracks: true  // Enable native text tracks for subtitles
                    }},
                    playbackRates: [0.5, 1, 1.25, 1.5, 2],
                    controls: true,
                    preload: 'auto',
                    textTrackSettings: {{
                        persistTextTrackSettings: false
                    }}
                }});
                
                const loading = document.getElementById('loading');
                const qualitySel = document.getElementById('quality');
                const applyBtn = document.getElementById('apply');
                
                // Event listeners for debugging
                player.ready(() => {{
                    console.log('Video.js player is ready');
                    console.log('Stream URL:', '{html.escape(stream_url)}');
                    console.log('Initial type: video/mp4');
                    console.log('Subtitle files found:', {len(subtitle_files)});
                    loading.style.display = 'none';
                }});
                
                player.on('loadstart', () => {{
                    console.log('Video load started');
                }});
                
                player.on('loadeddata', () => {{
                    console.log('Video data loaded');
                    loading.style.display = 'none';
                }});
                
                player.on('canplay', () => {{
                    console.log('Video can start playing');
                }});
                
                player.on('waiting', () => {{
                    console.log('Video is waiting for data');
                }});
                
                player.on('stalled', () => {{
                    console.log('Video stalled - no data received');
                }});
                
                player.on('error', (e) => {{
                    console.error('Video player error:', e);
                    console.log('Error details:', player.error());
                    loading.style.display = 'none';
                    
                    // Test if the stream URL is accessible
                    fetch('{html.escape(stream_url)}', {{ method: 'HEAD' }})
                        .then(response => {{
                            console.log('Stream URL response:', response.status, response.statusText);
                            console.log('Content-Type:', response.headers.get('content-type'));
                        }})
                        .catch(err => {{
                            console.error('Stream URL fetch error:', err);
                        }});
                    
                    player.error({{
                        code: 4,
                        message: 'Error loading video. Please try downloading the file instead.'
                    }});
                }});
                
                player.on('play', () => {{
                    console.log('Video started playing');
                }});
                
                player.on('pause', () => {{
                    console.log('Video paused');
                }});
                
                // Keyboard shortcuts
                document.addEventListener('keydown', (e) => {{
                    switch(e.code) {{
                        case 'Space':
                            e.preventDefault();
                            if (player.paused()) {{
                                player.play();
                            }} else {{
                                player.pause();
                            }}
                            break;
                        case 'ArrowLeft':
                            player.currentTime(player.currentTime() - 10);
                            break;
                        case 'ArrowRight':
                            player.currentTime(player.currentTime() + 10);
                            break;
                        case 'KeyF':
                            if (player.isFullscreen()) {{
                                player.exitFullscreen();
                            }} else {{
                                player.requestFullscreen();
                            }}
                            break;
                    }}
                }});
                if (applyBtn) {{
                  applyBtn.addEventListener('click', () => {{
                      const url = new URL('{html.escape(stream_url)}');
                      url.searchParams.set('quality', qualitySel.value);
                      // Use original container type for direct; MP4 for remux/transcode
                      const newType = qualitySel.value === 'direct' ? '{self._get_original_mime_type(filename)}' : 'video/mp4';
                      player.src({{ src: url.toString(), type: newType }});
                      player.play();
                  }});
                }}
            </script>
        </body>
        </html>
        """
        
        return web.Response(text=html_content, content_type='text/html')
    
    async def handle_subtitle(self, request: web.Request) -> web.Response:
        """Serve subtitle files"""
        token = request.query.get('token')
        if not token:
            return web.Response(status=400, text='Missing token')
        
        verified = self.verify_token(token)
        if not verified:
            return web.Response(status=403, text='Invalid or expired token')
        path = verified
        
        # Find subtitle files for this video
        subtitle_files = self._find_subtitle_files(path)
        
        if not subtitle_files:
            return web.Response(status=404, text='No subtitle files found')
        
        # Get requested language or default to first available
        requested_lang = request.query.get('lang', 'en')
        subtitle_file = None
        
        # Try to find subtitle in requested language
        for sub in subtitle_files:
            if sub['language'] == requested_lang:
                subtitle_file = sub
                break
        
        # Fallback to first available subtitle
        if not subtitle_file:
            subtitle_file = subtitle_files[0]
        
        # Read and serve the subtitle file from SFTP
        try:
            sftp = self.scanner._connect()
            try:
                with sftp.open(subtitle_file['path'], 'rb') as f:
                    raw = f.read()
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass

            content = raw.decode('utf-8', errors='replace')

            # Convert SRT to VTT if needed
            if subtitle_file['extension'] == '.srt':
                content = self._convert_srt_to_vtt(content)
                content_type = 'text/vtt'
            elif subtitle_file['extension'] == '.vtt':
                content_type = 'text/vtt'
            elif subtitle_file['extension'] in ['.ass', '.ssa']:
                content_type = 'text/plain'
            else:
                content_type = 'text/plain'

            return web.Response(
                text=content,
                content_type=content_type,
                headers={
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'GET',
                    'Access-Control-Allow-Headers': 'Range'
                }
            )
        except Exception as e:
            return web.Response(status=500, text=f'Error reading subtitle file: {str(e)}')
    
    def _convert_srt_to_vtt(self, srt_content: str) -> str:
        """Convert SRT subtitle format to VTT format"""
        lines = srt_content.strip().split('\n')
        vtt_lines = ['WEBVTT', '']
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            # Skip empty lines
            if not line:
                i += 1
                continue
            
            # Skip subtitle number
            if line.isdigit():
                i += 1
                continue
            
            # Process time line
            if '-->' in line:
                # Convert SRT time format to VTT format
                time_line = line.replace(',', '.')
                vtt_lines.append(time_line)
                i += 1
                
                # Add subtitle text
                subtitle_text = []
                while i < len(lines) and lines[i].strip():
                    subtitle_text.append(lines[i].strip())
                    i += 1
                
                if subtitle_text:
                    vtt_lines.append(' '.join(subtitle_text))
                    vtt_lines.append('')
            else:
                i += 1
        
        return '\n'.join(vtt_lines)
    
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
                    'mime_type': self._get_original_mime_type(filename),
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
        """Stream video file with quality selector: direct, remux, or scaled transcode."""
        token = request.query.get('token')
        if not token:
            return web.Response(status=400, text='Missing token')
        
        path = self.verify_token(token)
        if not path:
            return web.Response(status=403, text='Invalid or expired token')
        
        filename = path.rsplit('/', 1)[-1]
        if not self._is_video_file(filename):
            return web.Response(status=400, text='File is not a supported video format')
        
        quality = (request.query.get('quality') or '').lower()
        if quality not in ('direct','remux','1080p','720p','480p'):
            quality = 'direct' if filename.lower().endswith(('.mp4','.m4v')) else 'remux'

        # Direct path for native MP4/M4V
        if quality == 'direct' and (filename.lower().endswith('.mp4') or filename.lower().endswith('.m4v')):
            return await self._stream_direct(path, filename, request)

        # FFmpeg required for remux/transcode
        ffmpeg_path = await self._find_ffmpeg()
        if not ffmpeg_path:
            return await self._stream_direct(path, filename, request)

        if quality == 'remux':
            # Probe to decide whether copy is browser-compatible; else transcode video
            ffprobe_path = await self._find_ffprobe(ffmpeg_path)
            if ffprobe_path:
                try:
                    vstream = await self._probe_video_stream(path, ffprobe_path)
                except Exception:
                    vstream = None
            else:
                vstream = None

            if self._is_codec_browser_compatible(vstream):
                return await self._stream_remux_to_mp4(path, filename, request, ffmpeg_path)
            # Fallback: transcode video for compatibility
            return await self._stream_with_transcoding(path, filename, request, ffmpeg_path, target_height=None)

        target_height = 1080 if quality == '1080p' else 720 if quality == '720p' else 480
        return await self._stream_with_transcoding(path, filename, request, ffmpeg_path, target_height)
    
    async def _stream_direct(self, path: str, filename: str, request: web.Request) -> web.StreamResponse:
        """Stream video file directly without transcoding"""
        mime_type = self._get_original_mime_type(filename)
        
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
            'X-Accel-Buffering': 'no',
        }
        
        print(f"Streaming {filename} with MIME type: {mime_type}")
        print(f"Needs transcoding: {self._needs_transcoding(filename)}")
        print(f"Video player enabled: {self.cfg.enable_video_player}")
        
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
                        to_read = min(512 * 1024, (limit - pos + 1))
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
    
    async def _find_ffmpeg(self) -> Optional[str]:
        """Find FFmpeg executable path"""
        import shutil
        import subprocess
        import os
        
        # Respect configured path first
        cfg_path = getattr(self.cfg, 'ffmpeg_path', None)
        if cfg_path:
            try:
                result = subprocess.run([cfg_path, '-version'], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    print(f"Found FFmpeg from config: {cfg_path}")
                    return cfg_path
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass

        # Try common paths
        common_paths = [
            'ffmpeg',
            '/usr/bin/ffmpeg',
            '/usr/local/bin/ffmpeg',
            '/opt/homebrew/bin/ffmpeg',
            '/snap/bin/ffmpeg',
            'ffmpeg.exe',
            r'C:\\Program Files\\ffmpeg\\bin\\ffmpeg.exe',
            r'C:\\ffmpeg\\bin\\ffmpeg.exe',
        ]
        
        for path in common_paths:
            try:
                result = subprocess.run([path, '-version'], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    print(f"Found FFmpeg at: {path}")
                    return path
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                continue
        
        # Try using shutil.which
        ffmpeg_path = shutil.which('ffmpeg')
        if ffmpeg_path:
            print(f"Found FFmpeg via shutil.which: {ffmpeg_path}")
            return ffmpeg_path
        
        print("FFmpeg not found in any common locations")
        return None

    async def _find_ffprobe(self, ffmpeg_path: Optional[str]) -> Optional[str]:
        """Find FFprobe executable path, trying near ffmpeg first, then PATH/common locations."""
        import shutil
        import subprocess
        import os

        candidates = []
        if ffmpeg_path:
            # Try replacing basename with ffprobe in same directory
            base = os.path.basename(ffmpeg_path)
            d = os.path.dirname(ffmpeg_path) or '.'
            if base.lower().startswith('ffmpeg'):
                alt = os.path.join(d, base.replace('ffmpeg', 'ffprobe'))
                candidates.append(alt)
            candidates.append(os.path.join(d, 'ffprobe'))
            candidates.append(os.path.join(d, 'ffprobe.exe'))

        candidates.extend([
            'ffprobe',
            '/usr/bin/ffprobe',
            '/usr/local/bin/ffprobe',
            '/opt/homebrew/bin/ffprobe',
            '/snap/bin/ffprobe',
            r'C:\\Program Files\\ffmpeg\\bin\\ffprobe.exe',
            r'C:\\ffmpeg\\bin\\ffprobe.exe',
        ])

        for path in candidates:
            try:
                result = subprocess.run([path, '-version'], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    return path
            except Exception:
                continue
        # Fallback to PATH lookup
        which = shutil.which('ffprobe')
        return which

    async def _probe_video_stream(self, path: str, ffprobe_path: str) -> Optional[dict]:
        """Probe remote video stream via SFTP piping into ffprobe. Returns stream info dict or None."""
        import json
        probe_cmd = [
            ffprobe_path,
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name,profile,pix_fmt,width,height',
            '-of', 'json',
            '-i', 'pipe:0',
        ]

        proc = await asyncio.create_subprocess_exec(
            *probe_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stop_flag = {'stop': False}

        async def feeder():
            sftp = None
            sent = 0
            max_bytes = 8 * 1024 * 1024  # 8MB should be enough for probing
            try:
                sftp = self.scanner._connect()
                with sftp.open(path, 'rb') as f:
                    while not stop_flag['stop'] and sent < max_bytes:
                        chunk = f.read(min(256 * 1024, max_bytes - sent))
                        if not chunk:
                            break
                        sent += len(chunk)
                        if proc.stdin is None:
                            break
                        try:
                            proc.stdin.write(chunk)
                            await proc.stdin.drain()
                        except Exception:
                            break
            finally:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                        await proc.stdin.wait_closed()
                except Exception:
                    pass
                try:
                    if sftp:
                        sftp.close()
                except Exception:
                    pass

        feeder_task = asyncio.create_task(feeder())
        try:
            stdout, stderr = await proc.communicate()
        finally:
            stop_flag['stop'] = True
            try:
                await feeder_task
            except Exception:
                pass

        if not stdout:
            return None
        try:
            data = json.loads(stdout.decode('utf-8', errors='ignore'))
            streams = data.get('streams') or []
            return streams[0] if streams else None
        except Exception:
            return None

    def _is_codec_browser_compatible(self, stream_info: Optional[dict]) -> bool:
        """Heuristic: consider compatible if H.264 and 4:2:0."""
        if not stream_info:
            return False
        codec = (stream_info.get('codec_name') or '').lower()
        pix_fmt = (stream_info.get('pix_fmt') or '').lower()
        if codec in ('h264', 'avc1') and ('420' in pix_fmt or pix_fmt == 'yuvj420p'):
            return True
        return False
    
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
    
    async def _stream_remux_to_mp4(self, path: str, filename: str, request: web.Request, ffmpeg_path: str) -> web.StreamResponse:
        """Remux original to MP4: copy video if possible, transcode audio to AAC for compatibility."""
        headers = {
            'Content-Type': 'video/mp4',
            'Content-Disposition': f"inline; filename*=UTF-8''{urllib.parse.quote(filename.rsplit('.', 1)[0] + '.mp4')}",
            'Cache-Control': 'no-cache',
        }
        resp = web.StreamResponse(status=200, reason='OK', headers=headers)
        await resp.prepare(request)
        loop = asyncio.get_running_loop()

        cmd = [
            ffmpeg_path,
            '-hide_banner', '-loglevel', 'error', '-stats',
            '-i', 'pipe:0',
            '-c:v', 'copy',
            '-c:a', 'aac', '-b:a', '192k', '-ac', '2',
            '-movflags', '+faststart',
            '-f', 'mp4',
            'pipe:1',
        ]

        async def run_ffmpeg():
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stop_flag = {'stop': False}

            async def feeder():
                sftp = None
                try:
                    sftp = self.scanner._connect()
                    with sftp.open(path, 'rb') as f:
                        while not stop_flag['stop']:
                            chunk = f.read(256 * 1024)
                            if not chunk:
                                break
                            if proc.stdin is not None:
                                proc.stdin.write(chunk)
                                await proc.stdin.drain()
                finally:
                    try:
                        if proc.stdin:
                            proc.stdin.close()
                            await proc.stdin.wait_closed()
                    except Exception:
                        pass
                    if sftp:
                        sftp.close()

            feeder_task = asyncio.create_task(feeder())

            try:
                if proc.stdout is not None:
                    while True:
                        chunk = await proc.stdout.read(128 * 1024)
                        if not chunk:
                            break
                        try:
                            await resp.write(chunk)
                        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
                            stop_flag['stop'] = True
                            break
            finally:
                stop_flag['stop'] = True
                try:
                    await feeder_task
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

    async def _stream_with_transcoding(self, path: str, filename: str, request: web.Request, ffmpeg_path: str, target_height: Optional[int] = None) -> web.StreamResponse:
        """Transcode to MP4; if target_height provided, scale with good quality settings."""
        if not self.cfg.enable_video_player:
            return web.Response(status=403, text='Video player disabled')

        headers = {
            'Content-Type': 'video/mp4',
            'Content-Disposition': f"inline; filename*=UTF-8''{urllib.parse.quote(filename.rsplit('.', 1)[0] + '.mp4')}",
            'Cache-Control': 'no-cache',
        }
        resp = web.StreamResponse(status=200, reason='OK', headers=headers)
        await resp.prepare(request)
        loop = asyncio.get_running_loop()

        vf = []
        if target_height:
            vf = ['-vf', f"scale=-2:{target_height}:flags=lanczos"]

        cmd = [
            ffmpeg_path,
            '-hide_banner', '-loglevel', 'error', '-stats',
            '-i', 'pipe:0',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-maxrate', '6M', '-bufsize', '12M',
            *vf,
            '-c:a', 'aac', '-b:a', '192k', '-ac', '2',
            '-movflags', '+faststart',
            '-f', 'mp4',
            'pipe:1',
        ]

        async def run_ffmpeg():
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stop_flag = {'stop': False}

            async def feeder():
                sftp = None
                try:
                    sftp = self.scanner._connect()
                    with sftp.open(path, 'rb') as f:
                        while not stop_flag['stop']:
                            chunk = f.read(256 * 1024)
                            if not chunk:
                                break
                            if proc.stdin is not None:
                                proc.stdin.write(chunk)
                                await proc.stdin.drain()
                finally:
                    try:
                        if proc.stdin:
                            proc.stdin.close()
                            await proc.stdin.wait_closed()
                    except Exception:
                        pass
                    if sftp:
                        sftp.close()

            feeder_task = asyncio.create_task(feeder())

            try:
                if proc.stdout is not None:
                    while True:
                        chunk = await proc.stdout.read(128 * 1024)
                        if not chunk:
                            break
                        try:
                            await resp.write(chunk)
                        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
                            stop_flag['stop'] = True
                            break
            finally:
                stop_flag['stop'] = True
                try:
                    await feeder_task
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

