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
    # Multi-ID support
    allowed_channel_ids: List[int]
    guild_ids: List[int]
    owner_user_id: Optional[int]

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

    # Video player
    enable_video_player: bool
    ffmpeg_path: str
    video_cache_seconds: int
    max_concurrent_streams: int

    # App behavior for public deployment
    enable_prefix_commands: bool
    log_level: str
    admin_token: Optional[str]
    runtime_config_path: str



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


def getenv_int_list(name: str) -> List[int]:
    v = os.getenv(name)
    if not v:
        return []
    out: List[int] = []
    for part in v.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


def _apply_runtime_overrides(cfg: Config) -> Config:
    import json
    path = cfg.runtime_config_path or "runtime_config.json"
    try:
        if not os.path.exists(path):
            return cfg
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return cfg

    # Simple typed setters
    def set_str(name: str):
        v = data.get(name)
        if isinstance(v, str):
            setattr(cfg, name, v)

    def set_opt_str(name: str):
        v = data.get(name)
        if v is None or isinstance(v, str):
            setattr(cfg, name, v)

    def set_bool(name: str):
        v = data.get(name)
        if isinstance(v, bool):
            setattr(cfg, name, v)

    def set_int(name: str):
        v = data.get(name)
        if isinstance(v, int):
            setattr(cfg, name, v)

    def set_list_str(name: str):
        v = data.get(name)
        if isinstance(v, list):
            arr = [str(x) for x in v]
            setattr(cfg, name, arr)
        elif isinstance(v, str):
            parts = [p.strip() for p in v.split(",") if p.strip()]
            setattr(cfg, name, parts)

    def set_list_int(name: str):
        v = data.get(name)
        if isinstance(v, list):
            out = []
            for x in v:
                try:
                    out.append(int(x))
                except Exception:
                    continue
            setattr(cfg, name, out)
        elif isinstance(v, str):
            out = []
            for part in v.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    out.append(int(part))
                except Exception:
                    continue
            setattr(cfg, name, out)

    # Apply known keys
    for key in [
        "sftp_host","sftp_username","sftp_password","ssh_key_path","library_root_path",
        "movies_root_path","tv_root_path","music_root_path","public_base_url","ffmpeg_path",
        "http_host","link_secret","command_prefix","log_level"
    ]:
        set_str(key)
    for key in ["discord_token","admin_token"]:
        set_opt_str(key)
    for key in [
        "enable_downloads","enable_http_links","enable_video_player","enable_prefix_commands"
    ]:
        set_bool(key)
    for key in [
        "sftp_port","page_size","cache_ttl_seconds","http_port","link_ttl_seconds",
        "video_cache_seconds","max_concurrent_streams","owner_user_id","allowed_channel_id","guild_id"
    ]:
        set_int(key)
    for key in ["file_extensions","movie_extensions","tv_extensions","music_extensions"]:
        set_list_str(key)
    for key in ["allowed_channel_ids","guild_ids"]:
        set_list_int(key)

    return cfg


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

    cfg = Config(
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
        allowed_channel_ids=(lambda singles, multi: (multi if multi else ([singles] if singles is not None else [])))(
            getenv_int_optional("ALLOWED_CHANNEL_ID"), getenv_int_list("ALLOWED_CHANNEL_IDS")
        ),
        guild_ids=(lambda single, multi: (multi if multi else ([single] if single is not None else [])))(
            getenv_int_optional("GUILD_ID"), getenv_int_list("GUILD_IDS")
        ),
        owner_user_id=getenv_int_optional("OWNER_USER_ID"),
        enable_downloads=os.getenv("ENABLE_DOWNLOADS", "false").lower() in ("1", "true", "yes"),
        max_upload_bytes=getenv_int("MAX_UPLOAD_BYTES", 8_000_000),
        enable_http_links=os.getenv("ENABLE_HTTP_LINKS", "false").lower() in ("1", "true", "yes"),
        http_host=os.getenv("HTTP_HOST", "0.0.0.0"),
        http_port=getenv_int("HTTP_PORT", 8080),
        public_base_url=os.getenv("PUBLIC_BASE_URL"),
        link_ttl_seconds=getenv_int("LINK_TTL_SECONDS", 900),
        link_secret=os.getenv("LINK_SECRET"),
        enable_video_player=os.getenv("ENABLE_VIDEO_PLAYER", "false").lower() in ("1", "true", "yes"),
        ffmpeg_path=os.getenv("FFMPEG_PATH", "ffmpeg"),
        video_cache_seconds=getenv_int("VIDEO_CACHE_SECONDS", 3600),
        max_concurrent_streams=getenv_int("MAX_CONCURRENT_STREAMS", 3),
        enable_prefix_commands=os.getenv("ENABLE_PREFIX_COMMANDS", "false").lower() in ("1", "true", "yes"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        admin_token=os.getenv("ADMIN_TOKEN"),
        runtime_config_path=os.getenv("RUNTIME_CONFIG_PATH", "runtime_config.json"),
    )
    return _apply_runtime_overrides(cfg)
