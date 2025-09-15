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

    @bot.event
    async def on_ready():
        logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
        # Start background update task
        if not background_update.is_running():
            background_update.start()
        # Ensure slash commands are synced
        try:
            await bot.tree.sync()
            logger.info("Slash commands synced.")
        except Exception:
            logger.exception("Failed to sync slash commands")
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
    @bot.tree.command(name="browse", description="Browse Books/Movies/TV/Music and get link pages")
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
        await interaction.response.send_message(embed=discord.Embed(title="Browse", description="Choose a category."), view=view, ephemeral=True)

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
