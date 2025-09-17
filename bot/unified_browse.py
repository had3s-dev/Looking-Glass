from typing import Dict, List, Optional, Callable, Tuple

import discord
import urllib.parse
import asyncio

from discord.ext import commands
from .config import Config
from .scanner import SeedboxScanner


def chunk(items: List[str], size: int) -> List[List[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def build_base_url(http_host: str, http_port: int, public_base_url: Optional[str]) -> str:
    if public_base_url:
        return public_base_url.rstrip('/')
    host = http_host if http_host != '0.0.0.0' else '127.0.0.1'
    return f"http://{host}:{http_port}"


def _make_button(label: str, style: discord.ButtonStyle, callback):
    btn = discord.ui.Button(label=label, style=style)
    btn.callback = callback  # type: ignore
    return btn


class ItemSelect(discord.ui.Select):
    def __init__(self, placeholder: str, options_list: List[str]):
        opts = [discord.SelectOption(label=o, value=o) for o in options_list[:25]]  # Discord max 25
        super().__init__(placeholder=placeholder, options=opts, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view: 'UnifiedBrowserView' = self.view  # type: ignore
        await view.on_item_selected(interaction, self.values[0])


class UnifiedBrowserView(discord.ui.View):
    def __init__(
        self,
        base_url: str,
        page_size: int,
        get_books_data: Callable[[], Dict[str, List[str]]],
        get_movies: Callable[[], List[str]],
        get_tv: Callable[[], Dict[str, List[str]]],
        get_music: Callable[[], Dict[str, List[str]]],
        build_links: Optional[Callable[[str, str], List[Tuple[str, str, int]]]] = None,
        build_stream_links: Optional[Callable[[str, str], List[Tuple[str, str, int]]]] = None,
        bot: Optional[commands.Bot] = None,
        config: Optional[Config] = None,
        scanner: Optional[SeedboxScanner] = None,
        rescan_callback: Optional[Callable] = None,
    ):
        super().__init__(timeout=600)
        self.base_url = base_url
        self.page_size = page_size
        self.get_books_data = get_books_data
        self.get_movies = get_movies
        self.get_tv = get_tv
        self.get_music = get_music
        self.build_links = build_links
        self.build_stream_links = build_stream_links
        self.bot = bot
        self.config = config
        self.scanner = scanner
        self.rescan_callback = rescan_callback
        self.category: Optional[str] = None
        self.per_page: int = 25
        self.page_index: int = 0
        self._current_list: List[str] = []  # items for current category
        self._show_category_buttons()

    def _embed(self, title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description)

    def _show_category_buttons(self):
        self.clear_items()
        async def on_books(inter: discord.Interaction):
            self.category = 'books'
            await self._show_category(inter)
        async def on_movies(inter: discord.Interaction):
            self.category = 'movies'
            await self._show_category(inter)
        async def on_tv(inter: discord.Interaction):
            self.category = 'tv'
            await self._show_category(inter)
        async def on_music(inter: discord.Interaction):
            self.category = 'music'
            await self._show_category(inter)
        self.add_item(_make_button("Books", discord.ButtonStyle.primary, on_books))
        self.add_item(_make_button("Movies", discord.ButtonStyle.primary, on_movies))
        self.add_item(_make_button("TV", discord.ButtonStyle.primary, on_tv))
        self.add_item(_make_button("Music", discord.ButtonStyle.primary, on_music))
        if self.base_url:
            upload_url = f"{self.base_url}/upload"
            self.add_item(discord.ui.Button(label="Upload", style=discord.ButtonStyle.success, url=upload_url))

    def _rebuild_category_controls(self, title: str, placeholder: str, total: int):
        # Build select for current page and nav buttons
        start = self.page_index * self.per_page
        end = min(start + self.per_page, total)
        page_items = self._current_list[start:end]
        self.add_item(ItemSelect(placeholder, page_items))
        # Nav buttons
        async def to_first(inter: discord.Interaction):
            self.page_index = 0
            await self._refresh_category(inter)
        async def to_prev(inter: discord.Interaction):
            if self.page_index > 0:
                self.page_index -= 1
            await self._refresh_category(inter)
        async def to_next(inter: discord.Interaction):
            if (self.page_index + 1) * self.per_page < total:
                self.page_index += 1
            await self._refresh_category(inter)
        async def to_last(inter: discord.Interaction):
            self.page_index = max(0, (total - 1) // self.per_page)
            await self._refresh_category(inter)
        first_btn = _make_button("⏮", discord.ButtonStyle.secondary, to_first)
        prev_btn = _make_button("◀", discord.ButtonStyle.secondary, to_prev)
        next_btn = _make_button("▶", discord.ButtonStyle.secondary, to_next)
        last_btn = _make_button("⏭", discord.ButtonStyle.secondary, to_last)
        # Disable according to bounds
        first_btn.disabled = self.page_index <= 0
        prev_btn.disabled = self.page_index <= 0
        last_page_index = max(0, (total - 1) // self.per_page)
        next_btn.disabled = self.page_index >= last_page_index
        last_btn.disabled = self.page_index >= last_page_index
        self.add_item(first_btn)
        self.add_item(prev_btn)
        self.add_item(next_btn)
        self.add_item(last_btn)

    async def _show_category(self, interaction: discord.Interaction):
        self.clear_items()
        title = f"Browse: {self.category.title() if self.category else ''}"
        if self.category == 'books':
            data = self.get_books_data()
            self._current_list = sorted(list(data.keys()))
            self.page_index = 0
            if not self._current_list:
                await interaction.response.edit_message(embed=self._embed(title, "No authors found."), view=self)
                return
            self._rebuild_category_controls(title, "Select an author", len(self._current_list))
            # Back button
            async def on_back(inter: discord.Interaction):
                self.category = None
                self._show_category_buttons()
                await inter.response.edit_message(embed=self._embed("Browse", "Choose a category."), view=self)
            self.add_item(_make_button("Back", discord.ButtonStyle.secondary, on_back))
            await interaction.response.edit_message(embed=self._embed(title, "Pick an author to get links for all their books."), view=self)
        elif self.category == 'movies':
            self._current_list = sorted(self.get_movies())
            self.page_index = 0
            if not self._current_list:
                await interaction.response.edit_message(embed=self._embed(title, "No movies found."), view=self)
                return
            self._rebuild_category_controls(title, "Select a movie", len(self._current_list))
            async def on_back(inter: discord.Interaction):
                self.category = None
                self._show_category_buttons()
                await inter.response.edit_message(embed=self._embed("Browse", "Choose a category."), view=self)
            self.add_item(_make_button("Back", discord.ButtonStyle.secondary, on_back))
            await interaction.response.edit_message(embed=self._embed(title, "Pick a movie to get links."), view=self)
        elif self.category == 'tv':
            self._current_list = sorted(list(self.get_tv().keys()))
            self.page_index = 0
            if not self._current_list:
                await interaction.response.edit_message(embed=self._embed(title, "No TV shows found."), view=self)
                return
            self._rebuild_category_controls(title, "Select a TV show", len(self._current_list))
            async def on_back(inter: discord.Interaction):
                self.category = None
                self._show_category_buttons()
                await inter.response.edit_message(embed=self._embed("Browse", "Choose a category."), view=self)
            self.add_item(_make_button("Back", discord.ButtonStyle.secondary, on_back))
            await interaction.response.edit_message(embed=self._embed(title, "Pick a TV show to get links for all episodes."), view=self)
        elif self.category == 'music':
            self._current_list = sorted(list(self.get_music().keys()))
            self.page_index = 0
            if not self._current_list:
                await interaction.response.edit_message(embed=self._embed(title, "No music artists found."), view=self)
                return
            self._rebuild_category_controls(title, "Select an artist", len(self._current_list))
            async def on_back(inter: discord.Interaction):
                self.category = None
                self._show_category_buttons()
                await inter.response.edit_message(embed=self._embed("Browse", "Choose a category."), view=self)
            self.add_item(_make_button("Back", discord.ButtonStyle.secondary, on_back))
            await interaction.response.edit_message(embed=self._embed(title, "Pick an artist to get links for tracks."), view=self)
        else:
            self._show_category_buttons()
            await interaction.response.edit_message(embed=self._embed("Browse", "Choose a category."), view=self)

    async def on_item_selected(self, interaction: discord.Interaction, item_name: str):
        if not self.category:
            await interaction.response.send_message("No category selected.", ephemeral=True)
            return
        url = f"{self.base_url}/links?kind={self.category}&name={urllib.parse.quote_plus(item_name)}"
        # Offer an "Open Links" button regardless, as a fallback
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open Links", url=url))

        dm_sent = False
        dm_error = None
        if self.build_links:
            # Build links in a thread to avoid blocking the event loop
            loop = asyncio.get_running_loop()
            try:
                items: List[Tuple[str, str, int]] = await loop.run_in_executor(None, lambda: self.build_links(self.category or "", item_name))
                stream_items: List[Tuple[str, str, int]] = []
                if self.build_stream_links and (self.category in ("movies", "tv")):
                    try:
                        stream_items = await loop.run_in_executor(None, lambda: self.build_stream_links(self.category or "", item_name))
                    except Exception:
                        stream_items = []

                if items or stream_items:
                    # Build a concise DM message
                    lines: List[str] = []
                    title = f"Links for {self.category.title()}: {item_name}"
                    lines.append(title)
                    if stream_items:
                        lines.append("Stream now (single-use):")
                        for filename, link, size in stream_items[:50]:
                            lines.append(f"- {filename} — {link}")
                    if items:
                        lines.append("Direct downloads:")
                        for filename, link, size in items[:50]:  # cap to reasonable number
                            lines.append(f"- {filename} — {link}")
                    content = "\n".join(lines)
                    await interaction.user.send(content)
                    dm_sent = True
                else:
                    dm_error = "No files found for that selection."
            except Exception as e:
                dm_error = str(e)

        # Respond ephemerally with status and the fallback button
        if dm_sent:
            msg = "I've sent you a DM with direct download links. If you don't see it, check your DM privacy settings."
        else:
            suffix = f" Reason: {dm_error}" if dm_error else ""
            msg = f"Couldn't send a DM with direct links.{suffix} You can still open the link page."
        await interaction.response.send_message(msg, view=view, ephemeral=True)

    async def _refresh_category(self, interaction: discord.Interaction):
        # Re-render current category keeping new page index
        self.clear_items()
        title = f"Browse: {self.category.title() if self.category else ''}"
        if self.category == 'books':
            self._rebuild_category_controls(title, "Select an author", len(self._current_list))
            async def on_back(inter: discord.Interaction):
                self.category = None
                self._show_category_buttons()
                await inter.response.edit_message(embed=self._embed("Browse", "Choose a category."), view=self)
            self.add_item(_make_button("Back", discord.ButtonStyle.secondary, on_back))
            await interaction.response.edit_message(embed=self._embed(title, "Pick an author to get links for all their books."), view=self)
        elif self.category == 'movies':
            self._rebuild_category_controls(title, "Select a movie", len(self._current_list))
            async def on_back(inter: discord.Interaction):
                self.category = None
                self._show_category_buttons()
                await inter.response.edit_message(embed=self._embed("Browse", "Choose a category."), view=self)
            self.add_item(_make_button("Back", discord.ButtonStyle.secondary, on_back))
            await interaction.response.edit_message(embed=self._embed(title, "Pick a movie to get links."), view=self)
        elif self.category == 'tv':
            self._rebuild_category_controls(title, "Select a TV show", len(self._current_list))
            async def on_back(inter: discord.Interaction):
                self.category = None
                self._show_category_buttons()
                await inter.response.edit_message(embed=self._embed("Browse", "Choose a category."), view=self)
            self.add_item(_make_button("Back", discord.ButtonStyle.secondary, on_back))
            await interaction.response.edit_message(embed=self._embed(title, "Pick a TV show to get links for all episodes."), view=self)
        elif self.category == 'music':
            self._rebuild_category_controls(title, "Select an artist", len(self._current_list))
            async def on_back(inter: discord.Interaction):
                self.category = None
                self._show_category_buttons()
                await inter.response.edit_message(embed=self._embed("Browse", "Choose a category."), view=self)
            self.add_item(_make_button("Back", discord.ButtonStyle.secondary, on_back))
            await interaction.response.edit_message(embed=self._embed(title, "Pick an artist to get links for tracks."), view=self)

    @staticmethod
    async def send(ctx, base_url: str, page_size: int, get_books_data, get_movies, get_tv, get_music, build_links=None, build_stream_links=None, bot=None, config=None, scanner=None, rescan_callback=None):
        view = UnifiedBrowserView(
            base_url,
            page_size,
            get_books_data,
            get_movies,
            get_tv,
            get_music,
            build_links,
            build_stream_links,
            bot=bot,
            config=config,
            scanner=scanner,
            rescan_callback=rescan_callback,
        )
        await ctx.send(embed=discord.Embed(title="Browse", description="Choose a category."), view=view)
