import asyncio
import base64
import hmac
import hashlib
import html
import urllib.parse
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
        return web.Response(text="\n".join(body), content_type='text/html')

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

        def read_chunks():
            import paramiko
            sftp = None
            try:
                sftp = self.scanner._connect()
                with sftp.open(path, 'rb') as f:
                    while True:
                        chunk = f.read(64 * 1024)
                        if not chunk:
                            break
                        yield chunk
            finally:
                try:
                    if sftp:
                        sftp.close()
                except Exception:
                    pass

        # Write chunks asynchronously
        for chunk in await loop.run_in_executor(None, lambda: list(read_chunks())):
            await resp.write(chunk)
        await resp.write_eof()
        return resp
