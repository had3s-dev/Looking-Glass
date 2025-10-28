import posixpath
import re
from typing import Dict, List, Optional, Tuple

import paramiko


class SeedboxScanner:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: Optional[str],
        pkey_path: Optional[str],
        root_path: str,
        file_extensions: List[str],
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.pkey_path = pkey_path
        self.root_path = root_path
        self.file_extensions = [ext.lower() for ext in file_extensions]

    def _connect(self) -> paramiko.SFTPClient:
        transport = paramiko.Transport((self.host, self.port))
        if self.pkey_path:
            key = paramiko.RSAKey.from_private_key_file(self.pkey_path)
            transport.connect(username=self.username, pkey=key)
        else:
            transport.connect(username=self.username, password=self.password)
        return paramiko.SFTPClient.from_transport(transport)

    def scan_library(self) -> Dict[str, List[str]]:
        """
        Scan the seedbox directory structure for authors and books.
        Expected structure:
          /root/Author Name/Book Title/*.epub
        or files directly under author folder:
          /root/Author Name/Book Title.epub
        Returns: { author: [book titles...] }
        """
        result: Dict[str, List[str]] = {}
        sftp = self._connect()
        try:
            # List author directories/files under root
            for author_entry in sftp.listdir_attr(self.root_path):
                author_name = author_entry.filename
                author_path = posixpath.join(self.root_path, author_name)
                if self._is_dir(sftp, author_path):
                    books = self._collect_books_in_author_dir(sftp, author_path)
                else:
                    # Handle case where files are directly under root named "Author - Book.ext"
                    books = []
                if books:
                    result[author_name] = sorted(list(set(books)))
            # Handle flat files in root shaped as "Author - Book.ext"
            flat_files = self._collect_flat_books_in_root(sftp, self.root_path)
            for author, book in flat_files:
                result.setdefault(author, []).append(book)
            # Deduplicate and sort
            for a in list(result.keys()):
                dedup = sorted(list(set(result[a])))
                if dedup:
                    result[a] = dedup
                else:
                    result.pop(a, None)
        finally:
            try:
                sftp.close()
            except Exception:
                pass
        return result

    # ---- Movies / TV / Music Scanners ----
    def scan_movies(self, root_path: str, exts: List[str]) -> List[str]:
        """
        Collect movie titles from typical structures like:
          /root/Movie Title (Year)/video.ext
          /root/Movie Title.ext
        Returns a sorted list of cleaned movie titles.
        """
        sftp = self._connect()
        titles: List[str] = []
        try:
            for entry in sftp.listdir_attr(root_path):
                name = entry.filename
                path = posixpath.join(root_path, name)
                if self._is_dir(sftp, path):
                    # If the directory contains any matching files, use the directory name as the title
                    if self._dir_has_any_matching(sftp, path, exts):
                        titles.append(self._clean_title(name))
                else:
                    if self._matches_any_ext(name, exts):
                        titles.append(self._clean_title(self._strip_any_ext(name, exts)))
        finally:
            try:
                sftp.close()
            except Exception:
                pass
        return sorted(list(set(titles)))

    def scan_tv(self, root_path: str, exts: List[str]) -> Dict[str, List[str]]:
        """
        Collect TV episodes grouped by show.
        Expected structures:
          /root/Show Name/Season 01/episode.ext
          /root/Show Name/episode.ext
        Returns: { show_name: [episode labels] }
        """
        import logging
        logger = logging.getLogger("Looking-Glass")
        sftp = self._connect()
        result: Dict[str, List[str]] = {}
        try:
            show_entries = sftp.listdir_attr(root_path)
            logger.info(f"TV scan: found {len(show_entries)} shows in root directory")
            for idx, show_entry in enumerate(show_entries):
                show_name = show_entry.filename
                if idx % 10 == 0:
                    logger.info(f"TV scan: processing show {idx+1}/{len(show_entries)}: {show_name}")
                show_path = posixpath.join(root_path, show_name)
                episodes: List[str] = []
                if self._is_dir(sftp, show_path):
                    # First, collect files directly under show dir
                    try:
                        for e in sftp.listdir_attr(show_path):
                            ep_name = e.filename
                            ep_path = posixpath.join(show_path, ep_name)
                            if self._is_dir(sftp, ep_path):
                                # Season or subdir: collect episodes inside
                                episodes.extend(self._collect_matching_files_in_dir(sftp, ep_path, exts))
                            else:
                                if self._matches_any_ext(ep_name, exts):
                                    episodes.append(self._clean_title(self._strip_any_ext(ep_name, exts)))
                    except IOError:
                        pass
                else:
                    # Flat file under root, skip
                    pass
                if episodes:
                    result[show_name] = sorted(list(set(episodes)))
        finally:
            try:
                sftp.close()
            except Exception:
                pass
        return result

    def scan_music(self, root_path: str, exts: List[str]) -> Dict[str, List[str]]:
        """
        Collect music tracks grouped by artist.
        Expected structures:
          /root/Artist/Album/track.ext
          /root/Artist/track.ext
        Returns: { artist: [track titles] }
        """
        sftp = self._connect()
        result: Dict[str, List[str]] = {}
        try:
            for artist_entry in sftp.listdir_attr(root_path):
                artist_name = artist_entry.filename
                artist_path = posixpath.join(root_path, artist_name)
                tracks: List[str] = []
                if self._is_dir(sftp, artist_path):
                    tracks.extend(self._collect_matching_files_in_dir(sftp, artist_path, exts, recurse=True))
                else:
                    if self._matches_any_ext(artist_name, exts):
                        tracks.append(self._clean_title(self._strip_any_ext(artist_name, exts)))
                if tracks:
                    # Deduplicate while preserving cleaned titles
                    result[artist_name] = sorted(list(set(tracks)))
        finally:
            try:
                sftp.close()
            except Exception:
                pass
        return result

    # ---- Download helpers ----
    def find_book_file(self, author: str, book_title: str) -> Optional[Tuple[str, int]]:
        """
        Attempt to locate a concrete file path for a given book. Returns (path, size_bytes) if found.
        Searches under self.root_path in directories matching the author name (case-insensitive),
        and files matching the given book_title (case-insensitive, ignoring extensions and tags).
        """
        sftp = self._connect()
        try:
            # Find candidate author path
            author_path = None
            for e in sftp.listdir_attr(self.root_path):
                name = e.filename
                if name.lower() == author.lower():
                    author_path = posixpath.join(self.root_path, name)
                    break
                if author.lower() in name.lower():
                    author_path = posixpath.join(self.root_path, name)
            if not author_path:
                # Also consider flat files under root in format "Author - Book.ext"
                pattern = re.compile(r"^(.+?)\s+-\s+(.+)$")
                for e in sftp.listdir_attr(self.root_path):
                    if self._matches_extension(e.filename):
                        base = self._strip_extension(e.filename)
                        m = pattern.match(base)
                        if m and m.group(1).strip().lower() == author.lower():
                            # Match book title
                            if self._normalize_title(m.group(2)) == self._normalize_title(book_title):
                                path = posixpath.join(self.root_path, e.filename)
                                size = sftp.stat(path).st_size
                                return path, size
                return None

            # Search files in author directory
            for e in sftp.listdir_attr(author_path):
                name = e.filename
                path = posixpath.join(author_path, name)
                if self._is_dir(sftp, path):
                    # Look inside directory for matching files
                    for f in sftp.listdir_attr(path):
                        if self._matches_extension(f.filename):
                            base = self._strip_extension(f.filename)
                            if self._normalize_title(base) == self._normalize_title(book_title):
                                fpath = posixpath.join(path, f.filename)
                                size = sftp.stat(fpath).st_size
                                return fpath, size
                else:
                    if self._matches_extension(name):
                        base = self._strip_extension(name)
                        if self._normalize_title(base) == self._normalize_title(book_title):
                            size = sftp.stat(path).st_size
                            return path, size
        except IOError:
            return None
        finally:
            try:
                sftp.close()
            except Exception:
                pass
        return None

    def _is_dir(self, sftp: paramiko.SFTPClient, path: str) -> bool:
        try:
            attr = sftp.stat(path)
            # Paramiko: S_ISDIR bit check
            return (attr.st_mode & 0o170000) == 0o040000
        except IOError:
            return False

    def _collect_books_in_author_dir(self, sftp: paramiko.SFTPClient, author_path: str) -> List[str]:
        books: List[str] = []
        try:
            entries = sftp.listdir_attr(author_path)
        except IOError:
            return books
        for e in entries:
            name = e.filename
            path = posixpath.join(author_path, name)
            if self._is_dir(sftp, path):
                # Treat subdir name as book title if it contains matching files
                title = name
                found = self._has_matching_files(sftp, path)
                if found:
                    books.append(self._clean_title(title))
            else:
                # File directly under author dir
                if self._matches_extension(name):
                    title = self._strip_extension(name)
                    # Support patterns like "Book Title (Year).ext"
                    books.append(self._clean_title(title))
        return books

    def _has_matching_files(self, sftp: paramiko.SFTPClient, dir_path: str) -> bool:
        try:
            for e in sftp.listdir_attr(dir_path):
                if self._matches_extension(e.filename):
                    return True
        except IOError:
            return False
        return False

    def _matches_extension(self, filename: str) -> bool:
        lower = filename.lower()
        return any(lower.endswith(ext) for ext in self.file_extensions)

    def _strip_extension(self, filename: str) -> str:
        for ext in self.file_extensions:
            if filename.lower().endswith(ext):
                return filename[: -len(ext)]
        return filename

    def _clean_title(self, title: str) -> str:
        # Remove common tags like [EPUB], {AZW3}, etc.
        t = re.sub(r"[\[\{\(].*?[\]\}\)]", "", title)
        t = re.sub(r"[_]+", " ", t)
        return t.strip()

    def _normalize_title(self, title: str) -> str:
        return self._clean_title(title).lower()

    def _matches_any_ext(self, filename: str, exts: List[str]) -> bool:
        lower = filename.lower()
        return any(lower.endswith(ext.lower()) for ext in exts)

    def _strip_any_ext(self, filename: str, exts: List[str]) -> str:
        for ext in exts:
            if filename.lower().endswith(ext.lower()):
                return filename[: -len(ext)]
        return filename

    def _dir_has_any_matching(self, sftp: paramiko.SFTPClient, dir_path: str, exts: List[str]) -> bool:
        try:
            for e in sftp.listdir_attr(dir_path):
                if self._matches_any_ext(e.filename, exts):
                    return True
        except IOError:
            return False
        return False

    def _collect_matching_files_in_dir(self, sftp: paramiko.SFTPClient, dir_path: str, exts: List[str], recurse: bool = False) -> List[str]:
        collected: List[str] = []
        try:
            for e in sftp.listdir_attr(dir_path):
                name = e.filename
                path = posixpath.join(dir_path, name)
                if self._is_dir(sftp, path):
                    if recurse:
                        collected.extend(self._collect_matching_files_in_dir(sftp, path, exts, recurse=True))
                else:
                    if self._matches_any_ext(name, exts):
                        collected.append(self._clean_title(self._strip_any_ext(name, exts)))
        except IOError:
            return collected
        return collected

    def _collect_flat_books_in_root(self, sftp: paramiko.SFTPClient, root: str) -> List[tuple]:
        matches: List[tuple] = []
        pattern = re.compile(r"^(.+?)\s+-\s+(.+)$")
        try:
            for e in sftp.listdir_attr(root):
                if e.filename.startswith('.'):
                    continue
                if self._matches_extension(e.filename):
                    base = self._strip_extension(e.filename)
                    m = pattern.match(base)
                    if m:
                        author = m.group(1).strip()
                        book = self._clean_title(m.group(2).strip())
                        matches.append((author, book))
        except IOError:
            return matches
        return matches
