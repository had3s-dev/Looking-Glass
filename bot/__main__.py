import asyncio
import logging
import io
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .config import Config, load_config
from .scanner import SeedboxScanner
from .cache import LibraryCache
from .web import LinkServer
from .unified_browse import UnifiedBrowserView

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("Looking-Glass")


 


def build_bot(cfg: Config) -> commands.Bot:
    intents = discord.Intents.default()
    # Request message content only when prefix commands are desired
    if getattr(cfg, "enable_prefix_commands", False):
        intents.message_content = True
    bot = commands.Bot(command_prefix=cfg.command_prefix, intents=intents, help_command=None)

    # Single-tenant caches and scanner
    cache = LibraryCache(max_age_seconds=cfg.cache_ttl_seconds)
    movies_cache: LibraryCache = LibraryCache(max_age_seconds=cfg.cache_ttl_seconds)
    tv_cache: LibraryCache = LibraryCache(max_age_seconds=cfg.cache_ttl_seconds)
    music_cache: LibraryCache = LibraryCache(max_age_seconds=cfg.cache_ttl_seconds)
    scanner = SeedboxScanner(
        host=cfg.sftp_host,
        port=cfg.sftp_port,
        username=cfg.sftp_username,
        password=cfg.sftp_password,
        pkey_path=cfg.ssh_key_path,
        root_path=cfg.library_root_path,
        file_extensions=cfg.file_extensions,
    )

    # Restrict commands to channels if configured
    allowed_channel_id = cfg.allowed_channel_id
    allowed_channel_ids = set(cfg.allowed_channel_ids or ([] if allowed_channel_id is None else [allowed_channel_id]))

    @bot.check
    async def channel_gate(ctx: commands.Context) -> bool:
        # If no restrictions configured, allow everywhere
        if not allowed_channel_ids:
            return True
        try:
            return bool(ctx.channel and ctx.channel.id in allowed_channel_ids)
        except Exception:
            return False

    link_server: Optional[LinkServer] = None
    base_link_url: Optional[str] = None

    _last_sync_ts: float = 0.0

    async def register_slash_commands(force: bool = False):
        nonlocal _last_sync_ts
        import time as _time
        # Cooldown 60s between sync attempts
        if (not force) and ((_time.time() - _last_sync_ts) < 60):
            return
        try:
            browse_cmd = app_commands.Command(name="browse", description="Browse Books/Movies/TV/Music (private)", callback=browse_slash)
            help_cmd = app_commands.Command(name="help", description="How to use this bot", callback=help_slash)
            sync_cmd = app_commands.Command(name="sync", description="(Owner) Re-sync slash commands", callback=sync_slash)
            folders_cmd = app_commands.Command(name="folders", description="(Owner) Export Movies/TV top-level folders as text files", callback=folders_slash)
            list_cmd = app_commands.Command(name="list", description="Export full file lists for Movies/TV as text files", callback=list_slash)
            devbadge_cmd = app_commands.Command(name="devbadge", description="(Owner) Get the link to claim the Active Developer badge", callback=devbadge_slash)
            # Prefer multi-guild list; fallback to single guild_id; else global
            target_guild_ids = cfg.guild_ids or ([cfg.guild_id] if cfg.guild_id else [])
            if target_guild_ids:
                # First, ensure we wipe any global commands to avoid UI duplicates
                try:
                    bot.tree.clear_commands(guild=None)
                    synced_global = await bot.tree.sync()
                    logger.info(f"Cleared global commands; now: {[c.name for c in synced_global]}")
                except Exception:
                    logger.exception("Failed to clear global commands prior to guild sync")

                for gid in target_guild_ids:
                    if gid is None:
                        continue
                    guild_obj = discord.Object(id=gid)
                    # Clear guild commands then re-add deterministically
                    try:
                        bot.tree.clear_commands(guild=guild_obj)
                    except Exception:
                        logger.exception(f"Failed to clear commands for guild {gid}")
                    bot.tree.add_command(browse_cmd, guild=guild_obj)
                    bot.tree.add_command(help_cmd, guild=guild_obj)
                    bot.tree.add_command(list_cmd, guild=guild_obj)
                    if cfg.owner_user_id is not None:
                        bot.tree.add_command(sync_cmd, guild=guild_obj)
                        bot.tree.add_command(folders_cmd, guild=guild_obj)
                        bot.tree.add_command(devbadge_cmd, guild=guild_obj)
                    synced = await bot.tree.sync(guild=guild_obj)
                    logger.info(f"Slash commands synced for guild {gid}: {[c.name for c in synced]}")
            else:
                # Global-only mode: clear and re-add globally
                bot.tree.clear_commands(guild=None)
                bot.tree.add_command(browse_cmd)
                bot.tree.add_command(help_cmd)
                bot.tree.add_command(list_cmd)
                if cfg.owner_user_id is not None:
                    bot.tree.add_command(sync_cmd)
                    bot.tree.add_command(folders_cmd)
                    bot.tree.add_command(devbadge_cmd)
                synced = await bot.tree.sync()
                logger.info(f"Global slash commands synced: {[c.name for c in synced]}")
            _last_sync_ts = _time.time()
        except Exception:
            logger.exception("Failed to register/sync slash commands")

    @bot.event
    async def on_ready():
        # Apply runtime log level
        try:
            logging.getLogger().setLevel(getattr(logging, (cfg.log_level or "INFO"), logging.INFO))
        except Exception:
            pass
        logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
        # Presence highlights the public /browse command
        try:
            activity = discord.Activity(type=discord.ActivityType.watching, name="BeepBoopBeepbop")
            await bot.change_presence(status=discord.Status.online, activity=activity)
            logger.info("Updated bot presence")
        except Exception:
            logger.exception("Failed to set bot presence")

        if not background_update.is_running():
            background_update.start()
        await register_slash_commands()
        # Start HTTP link server if enabled
        nonlocal link_server, base_link_url
        try:
            if cfg.enable_http_links and link_server is None:
                link_server = LinkServer(cfg, scanner)
                await link_server.start()
                # Build base URL
                if cfg.public_base_url:
                    base_link_url = cfg.public_base_url.rstrip('/')
                else:
                    host_display = cfg.http_host if cfg.http_host != '0.0.0.0' else '127.0.0.1'
                    base_link_url = f"http://{host_display}:{cfg.http_port}"
                logger.info(f"HTTP link server started at {base_link_url}")
                if cfg.public_base_url and not cfg.link_secret:
                    logger.warning("PUBLIC_BASE_URL is set but LINK_SECRET is not configured. Signed links will use a weak default secret. Set LINK_SECRET for production.")
        except Exception:
            logger.exception("Failed to start HTTP link server")

    @bot.event
    async def on_connect():
        # Re-sync on reconnect
        await register_slash_commands()

    @bot.event
    async def on_guild_available(guild: discord.Guild):
        # Ensure commands are present when guild becomes available
        await register_slash_commands()

    @bot.event
    async def on_command_error(ctx: commands.Context, error: commands.CommandError):
        # Silently ignore commands invoked outside the allowed channel
        if isinstance(error, commands.CheckFailure):
            return
        # Otherwise, log and provide a minimal error message
        logger.exception("Command error:", exc_info=error)
        try:
            await ctx.send("An error occurred while processing the command.")
        except Exception:
            pass

    # Lock to prevent concurrent scans
    _cache_lock = asyncio.Lock()
    _movies_lock = asyncio.Lock()
    _tv_lock = asyncio.Lock()
    _music_lock = asyncio.Lock()

    async def ensure_cache_up_to_date(force: bool = False) -> Dict[str, List[str]]:
        data = cache.get()
        if data is not None and not force:
            return data
        async with _cache_lock:
            # Double-check after acquiring lock
            data = cache.get()
            if data is not None and not force:
                return data
            logger.info("Book cache is empty, scanning library via SFTP...")
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, scanner.scan_library)
            cache.set(data)
            logger.info(f"Book scan complete: found {len(data)} authors")
        return data

    async def ensure_movies_up_to_date(force: bool = False) -> List[str]:
        if not cfg.movies_root_path:
            return []
        data = movies_cache.get()  # type: ignore
        if data is not None and not force:
            return data  # type: ignore
        async with _movies_lock:
            data = movies_cache.get()  # type: ignore
            if data is not None and not force:
                return data  # type: ignore
            loop = asyncio.get_running_loop()
            def _scan_movies():
                try:
                    logger.info("Scanning movies via SFTP...")
                    result = scanner.scan_movies(cfg.movies_root_path or "", cfg.movie_extensions)
                    logger.info(f"Movie scan complete: found {len(result)} movies")
                    return result
                except Exception:
                    logger.exception("Movie scan failed")
                    return []
            data = await loop.run_in_executor(None, _scan_movies)
            movies_cache.set(data)  # type: ignore
        return data  # type: ignore

    async def ensure_tv_up_to_date(force: bool = False) -> Dict[str, List[str]]:
        if not cfg.tv_root_path:
            return {}
        data = tv_cache.get()
        if data is not None and not force:
            return data
        async with _tv_lock:
            data = tv_cache.get()
            if data is not None and not force:
                return data
            loop = asyncio.get_running_loop()
            def _scan_tv():
                try:
                    logger.info("Scanning TV shows via SFTP...")
                    result = scanner.scan_tv(cfg.tv_root_path or "", cfg.tv_extensions)
                    logger.info(f"TV scan complete: found {len(result)} shows")
                    return result
                except Exception:
                    logger.exception("TV scan failed")
                    return {}
            data = await loop.run_in_executor(None, _scan_tv)
            tv_cache.set(data)
        return data

    async def ensure_music_up_to_date(force: bool = False) -> Dict[str, List[str]]:
        if not cfg.music_root_path:
            return {}
        data = music_cache.get()
        if data is not None and not force:
            return data
        async with _music_lock:
            data = music_cache.get()
            if data is not None and not force:
                return data
            loop = asyncio.get_running_loop()
            def _scan_music():
                try:
                    logger.info("Scanning music via SFTP...")
                    result = scanner.scan_music(cfg.music_root_path or "", cfg.music_extensions)
                    logger.info(f"Music scan complete: found {len(result)} artists")
                    return result
                except Exception:
                    logger.exception("Music scan failed")
                    return {}
            data = await loop.run_in_executor(None, _scan_music)
            music_cache.set(data)
        return data

    @tasks.loop(minutes=30)
    async def background_update():
        try:
            logger.info("Background update started")
            await ensure_cache_up_to_date(force=True)
            await ensure_movies_up_to_date(force=True)
            await ensure_tv_up_to_date(force=True)
            await ensure_music_up_to_date(force=True)
            logger.info("Background update completed")
        except Exception:
            logger.exception("Background update failed")

    @bot.command(name="help")
    async def help_cmd(ctx: commands.Context):
        desc = (
            "Commands:\n"
            f"{cfg.command_prefix}browseall - Browse Books/Movies/TV/Music and get link pages.\n"
            f"{cfg.command_prefix}update - Force an update from the seedbox.\n"
        )
        await ctx.send(desc)

    async def rescan_callback(category: str):
        if category == 'book':
            await ensure_cache_up_to_date(force=True)
            logger.info("Rescan of book library triggered by upload.")
        elif category == 'music':
            await ensure_music_up_to_date(force=True)
            logger.info("Rescan of music library triggered by upload.")

    @bot.command(name="browseall")
    async def browseall_cmd(ctx: commands.Context):
        if not cfg.enable_http_links:
            await ctx.send("HTTP links are disabled. Set ENABLE_HTTP_LINKS=true in environment variables.")
            return
        if not base_link_url:
            await ctx.send("HTTP link server is not ready yet. Please try again shortly.")
            return
        # Ensure caches are loaded (non-forced)
        books_data = await ensure_cache_up_to_date()
        movies_list = await ensure_movies_up_to_date()
        tv_data = await ensure_tv_up_to_date()
        music_data = await ensure_music_up_to_date()

        def get_books_data_local():
            return books_data

        def get_movies_local():
            return movies_list

        def get_tv_local():
            return tv_data

        def get_music_local():
            return music_data

        await UnifiedBrowserView.send(
            ctx,
            base_url=base_link_url,
            page_size=cfg.page_size,
            get_books_data=get_books_data_local,
            get_movies=get_movies_local,
            get_tv=get_tv_local,
            get_music=get_music_local,
            build_links=(link_server.build_links if link_server else None),
            build_video_links=(link_server.build_video_links if link_server else None),
            bot=bot,
            config=cfg,
            scanner=scanner,
            rescan_callback=rescan_callback,
        )

    # Slash command providing the same UI, ephemerally to the invoker only (public)
    async def browse_slash(interaction: discord.Interaction):
        if not cfg.enable_http_links:
            await interaction.response.send_message("HTTP links are disabled. Set ENABLE_HTTP_LINKS=true.", ephemeral=True)
            return
        if not base_link_url:
            await interaction.response.send_message("HTTP link server is not ready yet. Please try again shortly.", ephemeral=True)
            return

        # Defer early to avoid interaction timeout - show "thinking" indicator while loading
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            pass

        # Preload caches
        try:
            books_data = await ensure_cache_up_to_date()
            movies_list = await ensure_movies_up_to_date()
            tv_data = await ensure_tv_up_to_date()
            music_data = await ensure_music_up_to_date()
        except Exception as e:
            logger.exception("Failed to load caches for browse command")
            await interaction.followup.send(f"Failed to load library data: {str(e)}", ephemeral=True)
            return

        def get_books_data_local():
            return books_data

        def get_movies_local():
            return movies_list

        def get_tv_local():
            return tv_data

        def get_music_local():
            return music_data

        view = UnifiedBrowserView(
            base_url=base_link_url,
            page_size=cfg.page_size,
            get_books_data=get_books_data_local,
            get_movies=get_movies_local,
            get_tv=get_tv_local,
            get_music=get_music_local,
            build_links=(link_server.build_links if link_server else None),
            build_video_links=(link_server.build_video_links if link_server else None),
            bot=bot,
            config=cfg,
            scanner=scanner,
            rescan_callback=rescan_callback,
        )
        await interaction.followup.send(embed=discord.Embed(title="Browse", description="Choose a category."), view=view, ephemeral=True)

    # Slash help command: non-ephemeral so it can be pinned
    async def help_slash(interaction: discord.Interaction):
        embed = discord.Embed(
            title="Looking-Glass Help",
            description=(
                "Use `/browse` to open the library browser. Your interactions are private and only visible to you.\n\n"
                "Tips:\n"
                "- Select a category, then pick an item to get a links page.\n"
                "- You'll get a DM with direct links when possible, plus a button to open the signed links page.\n"
                "- Links expire automatically; ask an admin if you need longer durations.\n\n"
            ),
            color=discord.Color.blurple()
        )
        embed.add_field(name="Commands", value="`/browse` — browse and get links\n`/help` — show this help", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=False)

    # Owner-only: force re-sync slash commands
    async def sync_slash(interaction: discord.Interaction):
        authorized = False
        # Owner check
        if cfg.owner_user_id is None or interaction.user.id == cfg.owner_user_id:
            authorized = True
        # Fallback: allow server admins (Manage Guild)
        try:
            member = interaction.user  # type: ignore
            if hasattr(member, 'guild_permissions') and member.guild_permissions.manage_guild:
                authorized = True
        except Exception:
            pass
        if not authorized:
            await interaction.response.send_message("Not authorized. Ask a server admin or the bot owner to run /sync.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass
        await register_slash_commands(force=True)
        await interaction.followup.send("Slash commands re-synced.", ephemeral=True)

    # Owner-only: list top-level folders under Movies and TV and return as text files
    async def folders_slash(interaction: discord.Interaction):
        # Permission check
        if cfg.owner_user_id is not None and interaction.user.id != cfg.owner_user_id:
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        # Build lists using SFTP in thread pool to avoid blocking
        async def collect_dirs(root: Optional[str]) -> List[str]:
            if not root:
                return []
            loop = asyncio.get_running_loop()
            def _collect():
                names: List[str] = []
                sftp = None
                try:
                    sftp = scanner._connect()
                    for e in sftp.listdir_attr(root):
                        try:
                            # dir bit
                            if (e.st_mode & 0o170000) == 0o040000:
                                names.append(e.filename)
                        except Exception:
                            continue
                except Exception:
                    return names
                finally:
                    try:
                        if sftp:
                            sftp.close()
                    except Exception:
                        pass
                return sorted(names)
            return await loop.run_in_executor(None, _collect)

        movies_dirs, tv_dirs = await asyncio.gather(
            collect_dirs(cfg.movies_root_path),
            collect_dirs(cfg.tv_root_path),
        )

        # Prepare files
        import io as _io
        files: List[discord.File] = []
        if movies_dirs:
            movies_text = "\n".join(movies_dirs) + "\n"
            files.append(discord.File(fp=_io.BytesIO(movies_text.encode("utf-8")), filename="movies_folders.txt"))
        if tv_dirs:
            tv_text = "\n".join(tv_dirs) + "\n"
            files.append(discord.File(fp=_io.BytesIO(tv_text.encode("utf-8")), filename="tvshow_folders.txt"))
        if not files:
            await interaction.response.send_message("No folders found (check MOVIES_ROOT_PATH and TV_ROOT_PATH).", ephemeral=True)
            return
        await interaction.response.send_message(content="Here are the current top-level folders.", files=files, ephemeral=True)

    # Public: list folder names: Movies (top-level dirs), TV (shows with seasons)
    async def list_slash(interaction: discord.Interaction, kind: Optional[str] = None):
        try:
            await interaction.response.defer(ephemeral=False, thinking=False)
        except Exception:
            pass
        # Normalize filter
        filt = (kind or "").strip().lower()
        movies_only = filt in ("movies", "movie", "m")
        tv_only = filt in ("tv", "shows", "show", "s")
        async def collect_movie_dirs(root: Optional[str]) -> List[str]:
            if not root:
                return []
            loop = asyncio.get_running_loop()
            def _collect():
                out: List[str] = []
                sftp = None
                try:
                    sftp = scanner._connect()
                    for e in sftp.listdir_attr(root):
                        try:
                            if (e.st_mode & 0o170000) == 0o040000:
                                out.append(e.filename)
                        except Exception:
                            continue
                finally:
                    try:
                        if sftp:
                            sftp.close()
                    except Exception:
                        pass
                return sorted(out)
            return await loop.run_in_executor(None, _collect)

        async def collect_tv_dirs_and_seasons(root: Optional[str]) -> Dict[str, List[str]]:
            if not root:
                return {}
            loop = asyncio.get_running_loop()
            def _collect():
                out: Dict[str, List[str]] = {}
                sftp = None
                import posixpath as _pp
                try:
                    sftp = scanner._connect()
                    for show in sftp.listdir_attr(root):
                        try:
                            if (show.st_mode & 0o170000) != 0o040000:
                                continue
                            show_name = show.filename
                            seasons: List[str] = []
                            show_path = _pp.join(root, show_name)
                            for sub in sftp.listdir_attr(show_path):
                                try:
                                    if (sub.st_mode & 0o170000) == 0o040000:
                                        seasons.append(sub.filename)
                                except Exception:
                                    continue
                            out[show_name] = sorted(seasons)
                        except Exception:
                            continue
                finally:
                    try:
                        if sftp:
                            sftp.close()
                    except Exception:
                        pass
                return dict(sorted(out.items(), key=lambda kv: kv[0].lower()))
            return await loop.run_in_executor(None, _collect)

        # Collect according to filter
        movies_dirs: List[str] = []
        tv_map: Dict[str, List[str]] = {}
        if not tv_only:
            movies_dirs = await collect_movie_dirs(cfg.movies_root_path)
        if not movies_only:
            tv_map = await collect_tv_dirs_and_seasons(cfg.tv_root_path)

        if not movies_dirs and not tv_map:
            await interaction.followup.send("No files found (check MOVIES_ROOT_PATH/TV_ROOT_PATH).", ephemeral=False)
            return
        # Build markdown strings and chunk to Discord limits (~2000 chars)
        def make_sections():
            sections: List[str] = []
            if movies_dirs and not tv_only:
                header = f"**Movies (folders) ({len(movies_dirs)})**\n"
                body = "\n".join(f"- {n}" for n in movies_dirs)
                sections.append(header + body)
            if tv_map and not movies_only:
                header = f"**TV Shows (folders) ({len(tv_map)})**\n"
                lines: List[str] = []
                for show, seasons in tv_map.items():
                    lines.append(f"- {show}")
                    for s in seasons:
                        lines.append(f"  - {s}")
                sections.append(header + "\n".join(lines))
            return sections
        sections = make_sections()
        # Send each section split into chunks <= 1900 chars
        for section in sections:
            content = section
            while content:
                chunk = content[:1900]
                # Try to split at last newline to avoid breaking an item
                if len(content) > 1900 and "\n" in chunk:
                    split = chunk.rfind("\n")
                    chunk = content[:split]
                    content = content[split+1:]
                else:
                    content = content[len(chunk):]
                await interaction.followup.send(chunk, ephemeral=False)

    async def devbadge_slash(interaction: discord.Interaction):
        # Permission check
        if cfg.owner_user_id is not None and interaction.user.id != cfg.owner_user_id:
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Active Developer Badge",
            description=(
                "To claim your Active Developer Badge, you need to ensure this bot has registered "
                "at least one application command (like this one!) within the last 30 days.\n\n"
                "Once you've run a slash command, you can claim your badge here:\n"
                "https://discord.com/developers/active-developer"
            ),
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.command(name="update")
    async def update_cmd(ctx: commands.Context):
        await ctx.send("Updating library, please wait...")
        try:
            await ensure_cache_up_to_date(force=True)
            await ensure_movies_up_to_date(force=True)
            await ensure_tv_up_to_date(force=True)
            await ensure_music_up_to_date(force=True)
            await ctx.send("Update complete.")
        except Exception:
            logger.exception("Manual update failed")
            await ctx.send("Update failed. Check logs.")


    

    # Owner-only prefix fallback: !list (posts in channel): Movies top-level folders and TV shows with seasons
    @bot.command(name="list")
    async def list_cmd(ctx: commands.Context, *, kind: Optional[str] = None):
        if cfg.owner_user_id is not None and ctx.author.id != cfg.owner_user_id:
            return
        filt = (kind or "").strip().lower()
        movies_only = filt in ("movies", "movie", "m")
        tv_only = filt in ("tv", "shows", "show", "s")

        async def collect_movie_dirs(root: Optional[str]) -> List[str]:
            if not root:
                return []
            loop = asyncio.get_running_loop()
            def _collect():
                out: List[str] = []
                sftp = None
                try:
                    sftp = scanner._connect()
                    for e in sftp.listdir_attr(root):
                        try:
                            if (e.st_mode & 0o170000) == 0o040000:
                                out.append(e.filename)
                        except Exception:
                            continue
                finally:
                    try:
                        if sftp:
                            sftp.close()
                    except Exception:
                        pass
                return sorted(out)
            return await loop.run_in_executor(None, _collect)

        async def collect_tv_dirs_and_seasons(root: Optional[str]) -> Dict[str, List[str]]:
            if not root:
                return {}
            loop = asyncio.get_running_loop()
            def _collect():
                out: Dict[str, List[str]] = {}
                sftp = None
                import posixpath as _pp
                try:
                    sftp = scanner._connect()
                    for show in sftp.listdir_attr(root):
                        try:
                            if (show.st_mode & 0o170000) != 0o040000:
                                continue
                            show_name = show.filename
                            seasons: List[str] = []
                            show_path = _pp.join(root, show_name)
                            for sub in sftp.listdir_attr(show_path):
                                try:
                                    if (sub.st_mode & 0o170000) == 0o040000:
                                        seasons.append(sub.filename)
                                except Exception:
                                    continue
                            out[show_name] = sorted(seasons)
                        except Exception:
                            continue
                finally:
                    try:
                        if sftp:
                            sftp.close()
                    except Exception:
                        pass
                return dict(sorted(out.items(), key=lambda kv: kv[0].lower()))
            return await loop.run_in_executor(None, _collect)

        movies_dirs: List[str] = []
        tv_map: Dict[str, List[str]] = {}
        if not tv_only:
            movies_dirs = await collect_movie_dirs(cfg.movies_root_path)
        if not movies_only:
            tv_map = await collect_tv_dirs_and_seasons(cfg.tv_root_path)

        if not movies_dirs and not tv_map:
            await ctx.reply("No folders found (check MOVIES_ROOT_PATH/TV_ROOT_PATH).", mention_author=False)
            return

        # Build markdown sections
        sections: List[str] = []
        if movies_dirs and not tv_only:
            header = f"**Movies (folders) ({len(movies_dirs)})**\n"
            body = "\n".join(f"- {n}" for n in movies_dirs)
            sections.append(header + body)
        if tv_map and not movies_only:
            header = f"**TV Shows (folders) ({len(tv_map)})**\n"
            lines: List[str] = []
            for show, seasons in tv_map.items():
                lines.append(f"- {show}")
                for s in seasons:
                    lines.append(f"  - {s}")
            sections.append(header + "\n".join(lines))

        # Chunk and send to the invoking channel
        for section in sections:
            content = section
            while content:
                chunk = content[:1900]
                if len(content) > 1900 and "\n" in chunk:
                    split = chunk.rfind("\n")
                    chunk = content[:split]
                    content = content[split+1:]
                else:
                    content = content[len(chunk):]
                await ctx.send(chunk)

    return bot


def main():
    cfg = load_config()
    bot = build_bot(cfg)
    token = cfg.discord_token
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set")
    bot.run(token)


if __name__ == "__main__":
    main()
