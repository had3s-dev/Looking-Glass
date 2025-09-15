import asyncio
import logging
import os
from typing import Dict, List

import discord
from discord.ext import commands, tasks

from .config import Config, load_config
from .scanner import SeedboxScanner
from .cache import LibraryCache
from .paginator import Paginator
from .browse import AuthorBrowserView

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("discord-seedbox-bot")


INTENTS = discord.Intents.default()
INTENTS.message_content = True


def build_bot(cfg: Config) -> commands.Bot:
    bot = commands.Bot(command_prefix=cfg.command_prefix, intents=INTENTS, help_command=None)

    cache = LibraryCache(max_age_seconds=cfg.cache_ttl_seconds)
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

    @bot.event
    async def on_ready():
        logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
        # Start background update task
        if not background_update.is_running():
            background_update.start()

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

    @tasks.loop(minutes=30)
    async def background_update():
        try:
            logger.info("Background update started")
            await ensure_cache_up_to_date(force=True)
            logger.info("Background update completed")
        except Exception:
            logger.exception("Background update failed")

    @bot.command(name="help")
    async def help_cmd(ctx: commands.Context):
        desc = (
            "Commands:\n"
            f"{cfg.command_prefix}authors - List all authors.\n"
            f"{cfg.command_prefix}books <author> - List books for the specified author.\n"
            f"{cfg.command_prefix}update - Force an update from the seedbox.\n"
        )
        await ctx.send(desc)

    @bot.command(name="authors")
    async def authors_cmd(ctx: commands.Context):
        data = await ensure_cache_up_to_date()
        authors = sorted(list(data.keys()))
        if not authors:
            await ctx.send("No authors found.")
            return
        pages = []
        page = []
        for i, author in enumerate(authors, start=1):
            page.append(f"{i}. {author}")
            if len(page) >= cfg.page_size:
                pages.append("\n".join(page))
                page = []
        if page:
            pages.append("\n".join(page))

        title = f"Authors ({len(authors)})"
        await Paginator.send(ctx, title=title, pages=pages)

    @bot.command(name="browse")
    async def browse_cmd(ctx: commands.Context):
        data = await ensure_cache_up_to_date()
        if not data:
            await ctx.send("No authors found.")
            return
        await AuthorBrowserView.send(ctx, data=data)

    @bot.command(name="books")
    async def books_cmd(ctx: commands.Context, *, author: str = ""):
        if not author:
            await ctx.send(f"Usage: {cfg.command_prefix}books <author>")
            return
        data = await ensure_cache_up_to_date()
        # Find best matching author (case-insensitive)
        normalized = {a.lower(): a for a in data.keys()}
        key = author.lower()
        match = None
        if key in normalized:
            match = normalized[key]
        else:
            # partial match
            for k, v in normalized.items():
                if key in k:
                    match = v
                    break
        if not match:
            await ctx.send(f"Author not found: {author}")
            return
        books = sorted(data.get(match, []))
        if not books:
            await ctx.send(f"No books found for author: {match}")
            return
        pages = []
        page = []
        for i, book in enumerate(books, start=1):
            page.append(f"{i}. {book}")
            if len(page) >= cfg.page_size:
                pages.append("\n".join(page))
                page = []
        if page:
            pages.append("\n".join(page))
        title = f"Books by {match} ({len(books)})"
        await Paginator.send(ctx, title=title, pages=pages)

    @bot.command(name="update")
    async def update_cmd(ctx: commands.Context):
        await ctx.send("Updating library, please wait...")
        try:
            await ensure_cache_up_to_date(force=True)
            await ctx.send("Update complete.")
        except Exception:
            logger.exception("Manual update failed")
            await ctx.send("Update failed. Check logs.")

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
