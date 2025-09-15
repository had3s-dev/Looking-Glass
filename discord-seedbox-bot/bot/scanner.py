import posixpath
import re
from typing import Dict, List, Optional

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
