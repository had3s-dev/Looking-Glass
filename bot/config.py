import os
from dataclasses import dataclass
from typing import List, Optional

from dotenv import load_dotenv
import tempfile
import stat


@dataclass
class Config:
    discord_token: str
    command_prefix: str

    # SFTP connection
    sftp_host: str
    sftp_port: int
    sftp_username: str
    sftp_password: Optional[str]
    ssh_key_path: Optional[str]

    # Library scanning
    library_root_path: str  # Books root
    file_extensions: List[str]  # Book extensions

    # Movies scanning
    movies_root_path: Optional[str]
    movie_extensions: List[str]

    # TV scanning
    tv_root_path: Optional[str]
    tv_extensions: List[str]

    # Music scanning
    music_root_path: Optional[str]
    music_extensions: List[str]

    # Behavior
    page_size: int
    cache_ttl_seconds: int
    allowed_channel_id: Optional[int]
    guild_id: Optional[int]

    # Downloads
    enable_downloads: bool
    max_upload_bytes: int

    # HTTP link server
    enable_http_links: bool
    http_host: str
    http_port: int
    public_base_url: Optional[str]
    link_ttl_seconds: int
    link_secret: Optional[str]


def getenv_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def getenv_list(name: str, default: List[str]) -> List[str]:
    v = os.getenv(name)
    if not v:
        return default
    parts = [x.strip() for x in v.split(",")]
    return [p for p in parts if p]


def getenv_int_optional(name: str) -> Optional[int]:
    v = os.getenv(name)
    if v is None or v == "":
        return None
    try:
        return int(v)
    except ValueError:
        return None


def load_config() -> Config:
    # Load .env if present
    load_dotenv()

    # Allow SSH key to be provided via env text and written to a temp file at runtime
    ssh_key_path = os.getenv("SSH_KEY_PATH")
    ssh_key_text = os.getenv("SSH_KEY_TEXT")
    if not ssh_key_path and ssh_key_text:
        # Write key to a secure temp file
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.write(ssh_key_text.encode("utf-8"))
        tmp.flush()
        tmp.close()
        # Restrict permissions to owner read-only
        os.chmod(tmp.name, stat.S_IRUSR | stat.S_IWUSR)
        ssh_key_path = tmp.name

    return Config(
        discord_token=os.getenv("DISCORD_TOKEN", ""),
        command_prefix=os.getenv("COMMAND_PREFIX", "!"),
        sftp_host=os.getenv("SFTP_HOST", ""),
        sftp_port=getenv_int("SFTP_PORT", 22),
        sftp_username=os.getenv("SFTP_USERNAME", ""),
        sftp_password=os.getenv("SFTP_PASSWORD"),
        ssh_key_path=ssh_key_path,
        library_root_path=os.getenv("LIBRARY_ROOT_PATH", "/media/books"),
        file_extensions=getenv_list("FILE_EXTENSIONS", [".epub", ".mobi", ".pdf", ".azw3"]),
        movies_root_path=os.getenv("MOVIES_ROOT_PATH"),
        movie_extensions=getenv_list("MOVIE_EXTENSIONS", [".mp4", ".mkv", ".avi", ".mov"]),
        tv_root_path=os.getenv("TV_ROOT_PATH"),
        tv_extensions=getenv_list("TV_EXTENSIONS", [".mp4", ".mkv", ".avi", ".mov"]),
        music_root_path=os.getenv("MUSIC_ROOT_PATH"),
        music_extensions=getenv_list("MUSIC_EXTENSIONS", [".mp3", ".flac", ".m4a", ".wav"]),
        page_size=getenv_int("PAGE_SIZE", 20),
        cache_ttl_seconds=getenv_int("CACHE_TTL_SECONDS", 900),
        allowed_channel_id=getenv_int_optional("ALLOWED_CHANNEL_ID"),
        guild_id=getenv_int_optional("GUILD_ID"),
        enable_downloads=os.getenv("ENABLE_DOWNLOADS", "false").lower() in ("1", "true", "yes"),
        max_upload_bytes=getenv_int("MAX_UPLOAD_BYTES", 8_000_000),
        enable_http_links=os.getenv("ENABLE_HTTP_LINKS", "false").lower() in ("1", "true", "yes"),
        http_host=os.getenv("HTTP_HOST", "0.0.0.0"),
        http_port=getenv_int("HTTP_PORT", 8080),
        public_base_url=os.getenv("PUBLIC_BASE_URL"),
        link_ttl_seconds=getenv_int("LINK_TTL_SECONDS", 900),
        link_secret=os.getenv("LINK_SECRET"),
    )
