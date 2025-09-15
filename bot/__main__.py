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
logger = logging.getLogger("discord-seedbox-bot")


INTENTS = discord.Intents.default()
INTENTS.message_content = True


def build_bot(cfg: Config) -> commands.Bot:
    bot = commands.Bot(command_prefix=cfg.command_prefix, intents=INTENTS, help_command=None)

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

    # Restrict commands to a single channel if configured
    allowed_channel_id = cfg.allowed_channel_id

    @bot.check
    async def channel_gate(ctx: commands.Context) -> bool:
        if allowed_channel_id is None:
            return True
        try:
            return ctx.channel and ctx.channel.id == allowed_channel_id
        except Exception:
            return False

    link_server: Optional[LinkServer] = None
    base_link_url: Optional[str] = None

    _last_sync_ts: float = 0.0

    async def register_slash_commands():
        nonlocal _last_sync_ts
        import time as _time
        # Cooldown 60s between sync attempts
        if (_time.time() - _last_sync_ts) < 60:
            return
        try:
            browse_cmd = app_commands.Command(name="browse", description="Browse Books/Movies/TV/Music and get link pages", callback=browse_slash)
            folders_cmd = app_commands.Command(name="folders", description="(Owner) Export Movies/TV top-level folders as text files", callback=folders_slash)
            list_cmd = app_commands.Command(name="list", description="(Owner) Export full file lists for Movies/TV as text files", callback=list_slash)
            if cfg.guild_id:
                guild_obj = discord.Object(id=cfg.guild_id)
                existing = [c.name for c in bot.tree.get_commands(guild=guild_obj)]
                if "browse" not in existing:
                    bot.tree.add_command(browse_cmd, guild=guild_obj)
                if "folders" not in existing:
                    bot.tree.add_command(folders_cmd, guild=guild_obj)
                if "list" not in existing:
                    bot.tree.add_command(list_cmd, guild=guild_obj)
                synced = await bot.tree.sync(guild=guild_obj)
                logger.info(f"Slash commands synced for guild {cfg.guild_id}: {[c.name for c in synced]}")
            else:
                existing = [c.name for c in bot.tree.get_commands()]
                if "browse" not in existing:
                    bot.tree.add_command(browse_cmd)
                if "folders" not in existing:
                    bot.tree.add_command(folders_cmd)
                if "list" not in existing:
                    bot.tree.add_command(list_cmd)
                synced = await bot.tree.sync()
                logger.info(f"Global slash commands synced: {[c.name for c in synced]}")
            _last_sync_ts = _time.time()
        except Exception:
            logger.exception("Failed to register/sync slash commands")

    @bot.event
    async def on_ready():
        logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
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

    async def ensure_cache_up_to_date(force: bool = False) -> Dict[str, List[str]]:
        data = cache.get()
        if force or data is None:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, scanner.scan_library)
            cache.set(data)
        return data

    async def ensure_movies_up_to_date(force: bool = False) -> List[str]:
        if not cfg.movies_root_path:
            return []
        data = movies_cache.get()  # type: ignore
        if force or data is None:
            loop = asyncio.get_running_loop()
            def _scan_movies():
                try:
                    return scanner.scan_movies(cfg.movies_root_path or "", cfg.movie_extensions)
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
        if force or data is None:
            loop = asyncio.get_running_loop()
            def _scan_tv():
                try:
                    return scanner.scan_tv(cfg.tv_root_path or "", cfg.tv_extensions)
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
        if force or data is None:
            loop = asyncio.get_running_loop()
            def _scan_music():
                try:
                    return scanner.scan_music(cfg.music_root_path or "", cfg.music_extensions)
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
            f"{cfg.command_prefix}getbook <author> | <book> - Download a book file if downloads are enabled and file is small enough.\n"
            f"{cfg.command_prefix}update - Force an update from the seedbox.\n"
        )
        await ctx.send(desc)

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
        )

    # Slash command providing the same UI, but as an ephemeral message
    async def browse_slash(interaction: discord.Interaction):
        # Channel gate for interactions
        if allowed_channel_id is not None:
            try:
                if interaction.channel_id != allowed_channel_id:
                    await interaction.response.send_message("This command is not available in this channel.", ephemeral=True)
                    return
            except Exception:
                pass
        if not cfg.enable_http_links:
            await interaction.response.send_message("HTTP links are disabled. Set ENABLE_HTTP_LINKS=true.", ephemeral=True)
            return
        if not base_link_url:
            await interaction.response.send_message("HTTP link server is not ready yet. Please try again shortly.", ephemeral=True)
            return

        # Defer early to avoid interaction timeout
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass

        # Preload caches
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

        view = UnifiedBrowserView(
            base_url=base_link_url,
            page_size=cfg.page_size,
            get_books_data=get_books_data_local,
            get_movies=get_movies_local,
            get_tv=get_tv_local,
            get_music=get_music_local,
        )
        await interaction.followup.send(embed=discord.Embed(title="Browse", description="Choose a category."), view=view, ephemeral=True)

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

    # Owner-only: list all files recursively for Movies and TV and send as Markdown lists
    async def list_slash(interaction: discord.Interaction):
        if cfg.owner_user_id is not None and interaction.user.id != cfg.owner_user_id:
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass
        async def collect_files(root: Optional[str], exts: List[str]) -> List[str]:
            if not root:
                return []
            loop = asyncio.get_running_loop()
            def _walk():
                out: List[str] = []
                sftp = None
                import posixpath as _pp
                try:
                    sftp = scanner._connect()
                    stack = [root]
                    while stack:
                        d = stack.pop()
                        try:
                            for e in sftp.listdir_attr(d):
                                p = _pp.join(d, e.filename)
                                if (e.st_mode & 0o170000) == 0o040000:
                                    stack.append(p)
                                else:
                                    if any(e.filename.lower().endswith(ext.lower()) for ext in exts):
                                        rel = p[len(root):].lstrip('/')
                                        out.append(rel or e.filename)
                        except Exception:
                            continue
                finally:
                    try:
                        if sftp:
                            sftp.close()
                    except Exception:
                        pass
                return sorted(out)
            return await loop.run_in_executor(None, _walk)

        movies_files, tv_files = await asyncio.gather(
            collect_files(cfg.movies_root_path, cfg.movie_extensions),
            collect_files(cfg.tv_root_path, cfg.tv_extensions),
        )

        if not movies_files and not tv_files:
            await interaction.followup.send("No files found (check MOVIES_ROOT_PATH/TV_ROOT_PATH).", ephemeral=True)
            return
        # Build markdown strings and chunk to Discord limits (~2000 chars)
        def make_sections():
            sections: List[str] = []
            if movies_files:
                header = f"**Movies files ({len(movies_files)})**\n"
                body = "\n".join(f"- {p}" for p in movies_files)
                sections.append(header + body)
            if tv_files:
                header = f"**TV files ({len(tv_files)})**\n"
                body = "\n".join(f"- {p}" for p in tv_files)
                sections.append(header + body)
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
                await interaction.followup.send(chunk, ephemeral=True)

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


    # ---- Downloads ----
    @bot.command(name="getbook")
    async def getbook_cmd(ctx: commands.Context, *, query: str = ""):
        if not cfg.enable_downloads:
            await ctx.send("Downloads are disabled.")
            return
        if not query or "|" not in query:
            await ctx.send(f"Usage: {cfg.command_prefix}getbook <author> | <book title>")
            return
        author, book = [x.strip() for x in query.split("|", 1)]
        await ctx.send(f"Searching for '{book}' by '{author}'...")
        loop = asyncio.get_running_loop()
        def _find():
            try:
                return scanner.find_book_file(author, book)
            except Exception:
                logger.exception("find_book_file failed")
                return None
        found = await loop.run_in_executor(None, _find)
        if not found:
            await ctx.send("Could not locate that book file.")
            return
        path, size = found
        if size > cfg.max_upload_bytes:
            await ctx.send(f"File is too large to upload ({size} bytes > limit {cfg.max_upload_bytes} bytes).")
            return
        # Download and upload
        def _download_bytes():
            import paramiko
            import posixpath as _pp
            data = b""
            sftp = None
            try:
                sftp = scanner._connect()
                with sftp.open(path, 'rb') as f:
                    data = f.read()
            finally:
                try:
                    if sftp:
                        sftp.close()
                except Exception:
                    pass
            return data, _pp.basename(path)
        try:
            data, fname = await loop.run_in_executor(None, _download_bytes)
        except Exception:
            logger.exception("Download failed")
            await ctx.send("Failed to download the file.")
            return
        if not data:
            await ctx.send("Downloaded data is empty.")
            return
        fileobj = io.BytesIO(data)
        fileobj.seek(0)
        await ctx.send(file=discord.File(fileobj, filename=fname))

    # Owner-only prefix fallback: !list (DMs the owner)
    @bot.command(name="list")
    async def list_cmd(ctx: commands.Context):
        if cfg.owner_user_id is not None and ctx.author.id != cfg.owner_user_id:
            return
        async def collect_files(root: Optional[str], exts: List[str]) -> List[str]:
            if not root:
                return []
            loop = asyncio.get_running_loop()
            def _walk():
                out: List[str] = []
                sftp = None
                import posixpath as _pp
                try:
                    sftp = scanner._connect()
                    stack = [root]
                    while stack:
                        d = stack.pop()
                        try:
                            for e in sftp.listdir_attr(d):
                                p = _pp.join(d, e.filename)
                                if (e.st_mode & 0o170000) == 0o040000:
                                    stack.append(p)
                                else:
                                    if any(e.filename.lower().endswith(ext.lower()) for ext in exts):
                                        rel = p[len(root):].lstrip('/')
                                        out.append(rel or e.filename)
                        except Exception:
                            continue
                finally:
                    try:
                        if sftp:
                            sftp.close()
                    except Exception:
                        pass
                return sorted(out)
            return await loop.run_in_executor(None, _walk)

        movies_files, tv_files = await asyncio.gather(
            collect_files(cfg.movies_root_path, cfg.movie_extensions),
            collect_files(cfg.tv_root_path, cfg.tv_extensions),
        )
        if not movies_files and not tv_files:
            await ctx.reply("No files found (check MOVIES_ROOT_PATH/TV_ROOT_PATH).", mention_author=False)
            return
        # Build markdown sections
        def make_sections():
            sections: List[str] = []
            if movies_files:
                header = f"**Movies files ({len(movies_files)})**\n"
                body = "\n".join(f"- {p}" for p in movies_files)
                sections.append(header + body)
            if tv_files:
                header = f"**TV files ({len(tv_files)})**\n"
                body = "\n".join(f"- {p}" for p in tv_files)
                sections.append(header + body)
            return sections
        sections = make_sections()
        try:
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
                    await ctx.author.send(chunk)
            await ctx.reply("Sent you the file lists via DM.", mention_author=False)
        except discord.Forbidden:
            await ctx.reply("I couldn't DM you. Please enable DMs from server members and try again.", mention_author=False)

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
