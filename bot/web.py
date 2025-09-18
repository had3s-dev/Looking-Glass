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
            web.get('/subtitle', self.handle_subtitle),
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
        """Get MIME type for video file - returns MP4 for transcoded files"""
        lower = filename.lower()
        if lower.endswith('.mp4') or lower.endswith('.m4v'):
            return 'video/mp4'
        elif lower.endswith('.webm'):
            return 'video/webm'
        elif lower.endswith('.mov'):
            return 'video/quicktime'
        elif lower.endswith('.mkv') or lower.endswith('.avi'):
            # MKV and AVI files are transcoded to MP4, so return MP4 MIME type
            return 'video/mp4'
        return 'video/mp4'  # Default fallback
    
    def _needs_transcoding(self, filename: str) -> bool:
        """Check if file needs transcoding for browser compatibility"""
        lower = filename.lower()
        # MKV and AVI typically need transcoding for browser playback
        # But we'll try direct streaming first if FFmpeg is not available
        return lower.endswith('.mkv') or lower.endswith('.avi')
    
    def _find_subtitle_files(self, video_path: str) -> List[Dict[str, str]]:
        """Find subtitle files for a video"""
        import os
        video_dir = os.path.dirname(video_path)
        video_name = os.path.splitext(os.path.basename(video_path))[0]
        
        subtitle_files = []
        subtitle_extensions = ['.srt', '.vtt', '.ass', '.ssa']
        
        for ext in subtitle_extensions:
            subtitle_path = os.path.join(video_dir, f"{video_name}{ext}")
            if os.path.exists(subtitle_path):
                # Determine language from filename or default to 'en'
                lang = 'en'
                if '.en.' in subtitle_path:
                    lang = 'en'
                elif '.es.' in subtitle_path:
                    lang = 'es'
                elif '.fr.' in subtitle_path:
                    lang = 'fr'
                elif '.de.' in subtitle_path:
                    lang = 'de'
                elif '.it.' in subtitle_path:
                    lang = 'it'
                elif '.pt.' in subtitle_path:
                    lang = 'pt'
                elif '.ru.' in subtitle_path:
                    lang = 'ru'
                elif '.ja.' in subtitle_path:
                    lang = 'ja'
                elif '.ko.' in subtitle_path:
                    lang = 'ko'
                elif '.zh.' in subtitle_path:
                    lang = 'zh'
                
                subtitle_files.append({
                    'path': subtitle_path,
                    'language': lang,
                    'label': f"Subtitle ({lang.upper()})",
                    'extension': ext
                })
        
        return subtitle_files
    
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
        
        path = self.verify_token(token)
        if not path:
            return web.Response(status=403, text='Invalid or expired token')
        
        filename = path.rsplit('/', 1)[-1]
        if not self._is_video_file(filename):
            return web.Response(status=400, text='File is not a supported video format')
        
        base_url = self._base_url()
        stream_url = f"{base_url}/stream?token={urllib.parse.quote(token)}"
        
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
                        <source src="{html.escape(stream_url)}" type="{self._get_video_mime_type(filename)}">
                        {self._generate_subtitle_tracks(subtitle_files, token, base_url)}
                        <p class="vjs-no-js">
                            To view this video please enable JavaScript, and consider upgrading to a web browser that
                            <a href="https://videojs.com/html5-video-support/" target="_blank">supports HTML5 video</a>.
                        </p>
                    </video-js>
                    <div class="loading" id="loading">Loading video...</div>
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
                
                // Event listeners for debugging
                player.ready(() => {{
                    console.log('Video.js player is ready');
                    console.log('Stream URL:', '{html.escape(stream_url)}');
                    console.log('MIME type:', '{self._get_video_mime_type(filename)}');
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
        
        path = self.verify_token(token)
        if not path:
            return web.Response(status=403, text='Invalid or expired token')
        
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
        
        # Read and serve the subtitle file
        try:
            with open(subtitle_file['path'], 'r', encoding='utf-8') as f:
                content = f.read()
            
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
        needs_transcoding = self._needs_transcoding(filename)
        print(f"Stream request for {filename}: needs_transcoding={needs_transcoding}, video_player_enabled={self.cfg.enable_video_player}")
        
        if needs_transcoding and self.cfg.enable_video_player:
            # Check if FFmpeg is available
            ffmpeg_path = await self._find_ffmpeg()
            print(f"FFmpeg path: {ffmpeg_path}")
            if ffmpeg_path:
                print(f"Using transcoding for {filename}")
                return await self._stream_with_transcoding(path, filename, request)
            else:
                print("FFmpeg not available for MKV/AVI transcoding")
                return web.Response(status=503, text='Video transcoding not available. Please download the file instead.')
        else:
            print(f"Using direct streaming for {filename}")
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
    
    async def _find_ffmpeg(self) -> Optional[str]:
        """Find FFmpeg executable path"""
        import shutil
        import subprocess
        
        # Try common paths
        common_paths = [
            'ffmpeg',
            '/usr/bin/ffmpeg',
            '/usr/local/bin/ffmpeg',
            '/opt/homebrew/bin/ffmpeg',
            '/snap/bin/ffmpeg',
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
        
        # Find FFmpeg path
        ffmpeg_path = await self._find_ffmpeg()
        if not ffmpeg_path:
            return web.Response(status=500, text='FFmpeg not found on this system')
        
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
        
        # Re-encode for better compatibility (stream copy can be unreliable)
        cmd = [
            ffmpeg_path,
            '-hide_banner', '-loglevel', 'error', '-stats',
            '-i', 'pipe:0',
            '-c:v', 'libx264',  # Re-encode video for compatibility
            '-preset', 'ultrafast',  # Fast encoding
            '-crf', '28',  # Good quality/size balance
            '-maxrate', '2M',  # Limit bitrate
            '-bufsize', '4M',
            '-c:a', 'aac',  # Re-encode audio
            '-b:a', '128k',  # Audio bitrate
            '-ac', '2',  # Stereo
            '-movflags', '+faststart+frag_keyframe+empty_moov',  # Progressive download
            '-f', 'mp4',
            '-avoid_negative_ts', 'make_zero',  # Fix timestamp issues
            'pipe:1',
        ]
        
        async def run_ffmpeg():
            # Launch FFmpeg process
            print(f"Starting FFmpeg with command: {' '.join(cmd)}")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            print(f"FFmpeg process started with PID: {proc.pid}")
            
            # Producer: read from SFTP and feed FFmpeg
            stop_flag = {'stop': False}
            
            async def feeder():
                sftp = None
                try:
                    sftp = self.scanner._connect()
                    with sftp.open(path, 'rb') as f:
                        chunk_count = 0
                        total_bytes = 0
                        while not stop_flag['stop']:
                            chunk = f.read(256 * 1024)  # Larger chunks for transcoding
                            if not chunk:
                                break
                            try:
                                if proc.stdin is not None:
                                    proc.stdin.write(chunk)
                                    await proc.stdin.drain()  # Proper async drain
                                    chunk_count += 1
                                    total_bytes += len(chunk)
                                    if chunk_count % 10 == 0:  # Log every 10 chunks
                                        print(f"Fed {chunk_count} chunks, {total_bytes} bytes total")
                            except Exception as e:
                                print(f"Error writing to FFmpeg stdin: {e}")
                                break
                        print(f"Feeder finished: {chunk_count} chunks, {total_bytes} bytes total")
                except Exception as e:
                    print(f"Feeder error: {e}")
                finally:
                    try:
                        if proc.stdin:
                            proc.stdin.close()
                            await proc.stdin.wait_closed()
                    except Exception as e:
                        print(f"Error closing stdin: {e}")
                    if sftp:
                        sftp.close()
            
            feeder_task = asyncio.create_task(feeder())
            
            # Start stderr reader for debugging
            async def stderr_reader():
                try:
                    if proc.stderr is not None:
                        while True:
                            line = await proc.stderr.readline()
                            if not line:
                                break
                            line_str = line.decode('utf-8', errors='ignore').strip()
                            if line_str:
                                print(f"FFmpeg stderr: {line_str}")
                except Exception as e:
                    print(f"Stderr reader error: {e}")
            
            stderr_task = asyncio.create_task(stderr_reader())
            
            try:
                # Consumer: stream FFmpeg output to client with timeout
                if proc.stdout is not None:
                    first_chunk = True
                    chunk_count = 0
                    total_bytes = 0
                    last_activity = time.time()
                    
                    while True:
                        try:
                            # Add timeout to prevent hanging
                            chunk = await asyncio.wait_for(proc.stdout.read(128 * 1024), timeout=30.0)
                            if not chunk:
                                print("FFmpeg output ended")
                                break
                            
                            if first_chunk:
                                print(f"First chunk received: {len(chunk)} bytes")
                                first_chunk = False
                            
                            chunk_count += 1
                            total_bytes += len(chunk)
                            last_activity = time.time()
                            
                            if chunk_count % 10 == 0:
                                print(f"Output {chunk_count} chunks, {total_bytes} bytes total")
                            
                            try:
                                await resp.write(chunk)
                                await resp.drain()  # Ensure data is sent
                            except (ConnectionResetError, asyncio.CancelledError, RuntimeError) as e:
                                print(f"Connection lost during streaming: {e}")
                                stop_flag['stop'] = True
                                break
                                
                        except asyncio.TimeoutError:
                            print("Timeout waiting for FFmpeg output")
                            stop_flag['stop'] = True
                            break
                        except Exception as e:
                            print(f"Error reading from FFmpeg: {e}")
                            break
                    
                    print(f"Streaming completed: {chunk_count} chunks, {total_bytes} bytes total")
                else:
                    print("No stdout from FFmpeg")
            except Exception as e:
                print(f"Consumer error: {e}")
            finally:
                stop_flag['stop'] = True
                stderr_task.cancel()
                try:
                    await feeder_task
                except Exception as e:
                    print(f"Error waiting for feeder: {e}")
                try:
                    await stderr_task
                except asyncio.CancelledError:
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
            # Add timeout for FFmpeg startup
            await asyncio.wait_for(run_ffmpeg(), timeout=300)  # 5 minute timeout
        except asyncio.TimeoutError:
            print("FFmpeg transcoding timed out")
            return web.Response(status=408, text='Transcoding timeout')
        except Exception as e:
            print(f"FFmpeg error: {e}")
            return web.Response(status=500, text=f'Transcoding failed: {str(e)}')
        finally:
            try:
                await resp.write_eof()
            except Exception:
                pass
        
        return resp

