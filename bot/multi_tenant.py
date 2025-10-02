import json
import os
import secrets
import time
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Tuple, List

from .config import Config
from .scanner import SeedboxScanner
from .cache import LibraryCache


@dataclass
class GuildConfig:
    sftp_host: Optional[str] = None
    sftp_port: Optional[int] = None
    sftp_username: Optional[str] = None
    sftp_password: Optional[str] = None
    ssh_key_path: Optional[str] = None

    library_root_path: Optional[str] = None
    movies_root_path: Optional[str] = None
    tv_root_path: Optional[str] = None
    music_root_path: Optional[str] = None


class TenantManager:
    """
    Manages per-guild configuration, scanners, and caches.
    Falls back to base Config when no override exists for a guild.
    """

    def __init__(self, base_cfg: Config, tenants_path: Optional[str] = None) -> None:
        self.base_cfg = base_cfg
        self.tenants_path = tenants_path or os.getenv("TENANTS_PATH", "tenants.json")
        self.guild_to_cfg: Dict[int, GuildConfig] = {}
        self.guild_to_scanner: Dict[int, SeedboxScanner] = {}
        self.guild_to_caches: Dict[int, Tuple[LibraryCache, LibraryCache, LibraryCache, LibraryCache]] = {}
        # ephemeral admin tokens: token -> (guild_id, exp_ts)
        self._admin_tokens: Dict[str, Tuple[int, float]] = {}
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        try:
            if not os.path.exists(self.tenants_path):
                return
            with open(self.tenants_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for gid_str, cfg_obj in data.items():
                try:
                    gid = int(gid_str)
                except Exception:
                    continue
                gc = GuildConfig(**cfg_obj)
                self.guild_to_cfg[gid] = gc
        except Exception:
            # Ignore load errors; start fresh
            self.guild_to_cfg = {}

    def _persist(self) -> None:
        try:
            data = {str(gid): asdict(cfg) for gid, cfg in self.guild_to_cfg.items()}
            with open(self.tenants_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def create_admin_token(self, guild_id: int, ttl_seconds: int = 900) -> str:
        token = secrets.token_urlsafe(24)
        self._admin_tokens[token] = (guild_id, time.time() + ttl_seconds)
        return token

    def validate_admin_token(self, token: Optional[str]) -> Optional[int]:
        if not token:
            return None
        tup = self._admin_tokens.get(token)
        if not tup:
            return None
        guild_id, exp = tup
        if time.time() > exp:
            try:
                del self._admin_tokens[token]
            except Exception:
                pass
            return None
        return guild_id

    def get_effective_params(self, guild_id: Optional[int]) -> Dict[str, Optional[str]]:
        gc = self.guild_to_cfg.get(int(guild_id)) if guild_id is not None else None
        # Compose with base
        out = {
            "sftp_host": (gc.sftp_host if gc and gc.sftp_host else self.base_cfg.sftp_host),
            "sftp_port": int(gc.sftp_port if gc and gc.sftp_port is not None else self.base_cfg.sftp_port),
            "sftp_username": (gc.sftp_username if gc and gc.sftp_username else self.base_cfg.sftp_username),
            "sftp_password": (gc.sftp_password if gc and gc.sftp_password else self.base_cfg.sftp_password),
            "ssh_key_path": (gc.ssh_key_path if gc and gc.ssh_key_path else self.base_cfg.ssh_key_path),
            "library_root_path": (gc.library_root_path if gc and gc.library_root_path else self.base_cfg.library_root_path),
            "movies_root_path": (gc.movies_root_path if gc and gc.movies_root_path else self.base_cfg.movies_root_path),
            "tv_root_path": (gc.tv_root_path if gc and gc.tv_root_path else self.base_cfg.tv_root_path),
            "music_root_path": (gc.music_root_path if gc and gc.music_root_path else self.base_cfg.music_root_path),
        }
        return out

    def _build_scanner(self, params: Dict[str, Optional[str]]) -> SeedboxScanner:
        return SeedboxScanner(
            host=str(params["sftp_host"] or ""),
            port=int(params["sftp_port"] or 22),
            username=str(params["sftp_username"] or ""),
            password=params.get("sftp_password"),
            pkey_path=params.get("ssh_key_path"),
            root_path=str(params["library_root_path"] or "/media/books"),
            file_extensions=self.base_cfg.file_extensions,
        )

    def get_scanner(self, guild_id: Optional[int]) -> SeedboxScanner:
        gid = int(guild_id) if guild_id is not None else 0
        sc = self.guild_to_scanner.get(gid)
        if sc is not None:
            return sc
        params = self.get_effective_params(guild_id)
        sc = self._build_scanner(params)
        self.guild_to_scanner[gid] = sc
        return sc

    def get_caches(self, guild_id: Optional[int]) -> Tuple[LibraryCache, LibraryCache, LibraryCache, LibraryCache]:
        gid = int(guild_id) if guild_id is not None else 0
        tup = self.guild_to_caches.get(gid)
        if tup is not None:
            return tup
        ttl = self.base_cfg.cache_ttl_seconds
        tup = (
            LibraryCache(max_age_seconds=ttl),  # books
            LibraryCache(max_age_seconds=ttl),  # movies
            LibraryCache(max_age_seconds=ttl),  # tv
            LibraryCache(max_age_seconds=ttl),  # music
        )
        self.guild_to_caches[gid] = tup
        return tup

    def update_guild_config(self, guild_id: int, data: Dict) -> None:
        gc = self.guild_to_cfg.get(guild_id, GuildConfig())
        # Update known fields if present
        for key in list(asdict(gc).keys()):
            if key in data:
                setattr(gc, key, data.get(key))
        self.guild_to_cfg[guild_id] = gc
        # drop scanner and caches to force rebuild
        try:
            self.guild_to_scanner.pop(guild_id, None)
        except Exception:
            pass
        try:
            self.guild_to_caches.pop(guild_id, None)
        except Exception:
            pass
        self._persist()


