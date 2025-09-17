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
            web.get('/stream', self.handle_stream),
            web.get('/play', self.handle_play),
            web.get('/transmux', self.handle_transmux),
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

    # ---- Single-use streaming helpers ----
    # token -> {"exp": int, "uses": int}
    _single_use_tokens: Dict[str, Dict[str, int]] = {}

    def _register_single_use_token(self, token: str, exp_ts: int, max_uses: int = 4) -> None:
        import time
        now = int(time.time())
        # Opportunistic cleanup of expired tokens
        for k, v in list(self._single_use_tokens.items()):
            if v.get("exp", 0) < now:
                self._single_use_tokens.pop(k, None)
        self._single_use_tokens[token] = {"exp": exp_ts, "uses": max(1, max_uses)}

    def _consume_single_use_token(self, token: str) -> bool:
        # Decrement usage; remove when reaches zero
        rec = self._single_use_tokens.get(token)
        if not rec:
            return False
        rec["uses"] = max(0, rec.get("uses", 1) - 1)
        if rec["uses"] <= 0:
            self._single_use_tokens.pop(token, None)
        else:
            self._single_use_tokens[token] = rec
        return True

    def _is_token_valid(self, token: str) -> bool:
        import time
        rec = self._single_use_tokens.get(token)
        if not rec:
            return False
        if rec.get("exp", 0) < int(time.time()):
            # expired
            self._single_use_tokens.pop(token, None)
            return False
        if rec.get("uses", 0) <= 0:
            return False
        return True

    def _guess_mime(self, filename: str) -> str:
        lower = filename.lower()
        if lower.endswith('.mp4') or lower.endswith('.m4v'):
            return 'video/mp4'
        if lower.endswith('.webm'):
            return 'video/webm'
        if lower.endswith('.mov'):
            return 'video/quicktime'
        if lower.endswith('.mkv'):
            return 'video/x-matroska'
        if lower.endswith('.mp3'):
            return 'audio/mpeg'
        if lower.endswith('.flac'):
            return 'audio/flac'
        if lower.endswith('.m4a'):
            return 'audio/mp4'
        return 'application/octet-stream'

    def _is_browser_playable(self, filename: str) -> bool:
        lower = filename.lower()
        return lower.endswith('.mp4') or lower.endswith('.m4v') or lower.endswith('.webm') or lower.endswith('.mov')

    def _estimate_duration_seconds(self, size_bytes: int) -> int:
        # Simple estimate: duration = size / bitrate. Default 6 Mbps.
        bitrate_bps = 6_000_000
        try:
            return max(0, int(size_bytes / max(1, bitrate_bps)))
        except Exception:
            return 0

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

        # Build single-use stream links for videos
        stream_items: List[Tuple[str, str, int]] = []
        if kind in ('movies', 'tv'):
            now = int(time.time())
            for path, size in files:
                est = self._estimate_duration_seconds(size)
                s_exp = now + max(self.cfg.link_ttl_seconds, est * 2 if est > 0 else self.cfg.link_ttl_seconds)
                stoken = self.sign_path(path, s_exp)
                self._register_single_use_token(stoken, s_exp)
                surl = f"{base}/play?token={urllib.parse.quote(stoken)}"
                stream_items.append((path.rsplit('/', 1)[-1], surl, size))

        # Simple HTML
        title = f"Links for {html.escape(name)} ({html.escape(kind)})"
        body = [f"<h1>{title}</h1>"]
        if not items:
            body.append("<p>No files found.</p>")
        else:
            body.append("<ul>")
            for filename, url, size in items:
                body.append(f"<li><a href='{html.escape(url)}'>{html.escape(filename)}</a> <small>({size} bytes)</small></li>")
            body.append("</ul>")
        if stream_items:
            body.append("<h2>Stream Now (single-use)</h2>")
            body.append("<ul>")
            for filename, url, size in stream_items:
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

    def build_stream_links(self, kind: str, name: str) -> List[Tuple[str, str, int]]:
        """
        Build single-use streaming links for Movies/TV selection.
        Returns a list of tuples: (filename, url, size_bytes).
        """
        if kind not in ('movies', 'tv'):
            return []
        files: List[Tuple[str, int]] = self._collect_files_sync(kind, name)
        base = self._base_url()
        import time
        now = int(time.time())
        out: List[Tuple[str, str, int]] = []
        for path, size in files:
            est = self._estimate_duration_seconds(size)
            exp = now + max(self.cfg.link_ttl_seconds, est * 2 if est > 0 else self.cfg.link_ttl_seconds)
            token = self.sign_path(path, exp)
            self._register_single_use_token(token, exp)
            url = f"{base}/play?token={urllib.parse.quote(token)}"
            out.append((path.rsplit('/', 1)[-1], url, size))
        return out

    async def handle_play(self, request: web.Request) -> web.Response:
        token = request.query.get('token')
        if not token:
            return web.Response(status=400, text='Missing token')
        path = self.verify_token(token)
        if not path:
            return web.Response(status=403, text='Invalid or expired token')
        filename = path.rsplit('/', 1)[-1]
        mime = self._guess_mime(filename)
        base = self._base_url()
        # We intentionally do NOT consume the token here; /stream will consume it on first response.
        stream_url = f"{base}/stream?token={urllib.parse.quote(token)}"
        esc_name = html.escape(filename)
        esc_stream = html.escape(stream_url)
        # If the format isn't browser-playable and ffmpeg remux is enabled, use transmux URL
        if (not self._is_browser_playable(filename)) and self.cfg.enable_ffmpeg_remux:
            stream_url = f"{base}/transmux?token={urllib.parse.quote(token)}"

        page = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset='utf-8'>
            <meta name='viewport' content='width=device-width, initial-scale=1'>
            <title>Play {esc_name}</title>
            <style>
                html, body {{ height: 100%; margin: 0; background: #111; color: #eee; font-family: system-ui, sans-serif; }}
                .container {{ display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 12px; }}
                video {{ width: 100%; height: auto; max-width: 100%; background: #000; }}
                a.button {{ background: #2d6cdf; color: white; padding: 8px 12px; border-radius: 6px; text-decoration: none; }}
                .note {{ font-size: 0.9em; color: #aaa; }}
                .wrap {{ width: min(1200px, 100%); }}
                .warn {{ color: #ffcc66; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="wrap">
                    <h2>{esc_name}</h2>
                    <video controls autoplay playsinline preload="metadata">
                        <source src="{html.escape(stream_url)}" type="{('video/mp4' if (not self._is_browser_playable(filename)) and self.cfg.enable_ffmpeg_remux else mime)}">
                        Your browser may not support this media type. Try the direct link below.
                    </video>
                    <div>
                        <a class="button" href="{esc_stream}">Open direct stream</a>
                    </div>
                    <div class="note">This is a single-use link. If playback fails to start, you'll need a new link.</div>
                    {('<div class="note warn">This format may not play in your browser. Use the direct link with an external player (e.g., VLC).</div>' if not self._is_browser_playable(filename) else '')}
                </div>
            </div>
        </body>
        </html>
        """
        return web.Response(text=page, content_type='text/html')

    async def handle_transmux(self, request: web.Request) -> web.StreamResponse:
        token = request.query.get('token')
        if not token:
            return web.Response(status=400, text='Missing token')
        path = self.verify_token(token)
        if not path:
            return web.Response(status=403, text='Invalid or expired token')
        if not self.cfg.enable_ffmpeg_remux:
            return web.Response(status=403, text='Remux disabled')
        if not self._is_token_valid(token):
            return web.Response(status=403, text='Token already used or invalid.')

        filename = path.rsplit('/', 1)[-1]

        # Prepare response headers for progressive MP4
        headers = {
            'Content-Type': 'video/mp4',
            'Content-Disposition': f"inline; filename*=UTF-8''{urllib.parse.quote(filename.rsplit('.', 1)[0] + '.mp4')}",
            # We don't know Content-Length ahead of time when transcoding; omit it.
            'Accept-Ranges': 'none',
        }
        resp = web.StreamResponse(status=200, reason='OK', headers=headers)
        await resp.prepare(request)

        loop = asyncio.get_running_loop()

        # Start ffmpeg subprocess reading from stdin ('-') and writing MP4 to stdout ('-')
        # Use copy for video if possible; audio to AAC; faststart flags for progressive playback.
        # Note: On some inputs, copy may fail; a safer default is to transcode video. We start with copy for performance.
        cmd = [
            self.cfg.ffmpeg_path,
            '-hide_banner', '-loglevel', 'error',
            '-i', 'pipe:0',
            '-movflags', '+faststart+frag_keyframe+empty_moov',
            '-c:v', 'copy',
            '-c:a', 'aac', '-b:a', '192k',
            '-f', 'mp4',
            'pipe:1',
        ]

        # Token consumption will occur after first successful write
        consumed = {'done': False}

        async def run_ffmpeg():
            # Launch ffmpeg
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Producer: read from SFTP and feed ffmpeg stdin in a thread to avoid blocking
            stop_flag = {'stop': False}

            def feeder():
                sftp = None
                try:
                    sftp = self.scanner._connect()
                    with sftp.open(path, 'rb') as f:
                        while not stop_flag['stop']:
                            chunk = f.read(128 * 1024)
                            if not chunk:
                                break
                            try:
                                if proc.stdin is not None:
                                    proc.stdin.write(chunk)  # type: ignore
                            except Exception:
                                break
                finally:
                    try:
                        if proc.stdin:
                            proc.stdin.close()  # type: ignore
                    except Exception:
                        pass
                    try:
                        if sftp:
                            sftp.close()
                    except Exception:
                        pass

            feeder_future = loop.run_in_executor(None, feeder)

            try:
                # Consumer: stream ffmpeg stdout to client
                assert proc.stdout is not None
                while True:
                    chunk = await proc.stdout.read(128 * 1024)
                    if not chunk:
                        break
                    try:
                        if not consumed['done']:
                            self._consume_single_use_token(token)
                            consumed['done'] = True
                        await resp.write(chunk)
                    except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
                        break
            finally:
                stop_flag['stop'] = True
                try:
                    await feeder_future
                except Exception:
                    pass
                try:
                    if proc.stdout:
                        proc.stdout.close()  # type: ignore
                except Exception:
                    pass
                try:
                    if proc.stderr:
                        proc.stderr.close()  # type: ignore
                except Exception:
                    pass
                try:
                    if proc.stdin:
                        proc.stdin.close()  # type: ignore
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

    async def handle_stream(self, request: web.Request) -> web.StreamResponse:
        token = request.query.get('token')
        if not token:
            return web.Response(status=400, text='Missing token')
        path = self.verify_token(token)
        if not path:
            return web.Response(status=403, text='Invalid or expired token')

        # Enforce token validity (not expired and has remaining uses)
        if not self._is_token_valid(token):
            return web.Response(status=403, text='Token already used or invalid.')

        filename = path.rsplit('/', 1)[-1]
        mime = self._guess_mime(filename)

        loop = asyncio.get_running_loop()

        # Get file size first
        def _stat_sync() -> Tuple[int, Optional[Exception]]:
            sftp = None
            try:
                sftp = self.scanner._connect()
                size = sftp.stat(path).st_size
                return size, None
            except Exception as e:
                return 0, e
            finally:
                try:
                    if sftp:
                        sftp.close()
                except Exception:
                    pass

        size, err = await loop.run_in_executor(None, _stat_sync)
        if err is not None:
            return web.Response(status=404, text='File not found')

        # Range handling
        range_header = request.headers.get('Range')
        start = 0
        end = size - 1
        status = 200
        headers = {
            'Content-Type': mime,
            'Accept-Ranges': 'bytes',
            'Content-Disposition': f"inline; filename*=UTF-8''{urllib.parse.quote(filename)}",
        }
        if range_header and range_header.startswith('bytes='):
            try:
                spec = range_header.split('=')[1]
                s, e = spec.split('-')
                if s:
                    start = int(s)
                if e:
                    end = int(e)
                if end >= size:
                    end = size - 1
                if start > end:
                    return web.Response(status=416, text='Requested Range Not Satisfiable')
                status = 206
                headers['Content-Range'] = f'bytes {start}-{end}/{size}'
                headers['Content-Length'] = str(end - start + 1)
            except Exception:
                # Malformed range; ignore
                headers['Content-Length'] = str(size)
        else:
            headers['Content-Length'] = str(size)

        resp = web.StreamResponse(status=status, reason='OK', headers=headers)
        await resp.prepare(request)

        # For HEAD requests, do not stream body and do not consume the token
        if request.method.upper() == 'HEAD':
            try:
                await resp.write_eof()
            except Exception:
                pass
            return resp

        queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        stop_flag = {"stop": False}

        def producer_range():
            sftp = None
            try:
                sftp = self.scanner._connect()
                with sftp.open(path, 'rb') as f:
                    # Seek to start position
                    if start > 0:
                        try:
                            f.seek(start)
                        except Exception:
                            remaining = start
                            while remaining > 0:
                                chunk = f.read(min(64 * 1024, remaining))
                                if not chunk:
                                    break
                                remaining -= len(chunk)
                    pos = start
                    limit = end
                    while not stop_flag['stop'] and pos <= limit:
                        to_read = min(64 * 1024, (limit - pos + 1))
                        chunk = f.read(to_read)
                        if not chunk:
                            break
                        pos += len(chunk)
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

        _ = loop.run_in_executor(None, producer_range)

        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                try:
                    # Consume a token use on first successful write
                    if stop_flag['stop'] is False and queue.qsize() == 0:
                        # consume once per response
                        self._consume_single_use_token(token)
                    await resp.write(chunk)
                except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
                    stop_flag['stop'] = True
                    break
        finally:
            try:
                await resp.write_eof()
            except Exception:
                pass
        return resp
