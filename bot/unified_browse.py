from typing import Dict, List, Optional, Callable

import discord


def chunk(items: List[str], size: int) -> List[List[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def build_base_url(http_host: str, http_port: int, public_base_url: Optional[str]) -> str:
    if public_base_url:
        return public_base_url.rstrip('/')
    host = http_host if http_host != '0.0.0.0' else '127.0.0.1'
    return f"http://{host}:{http_port}"


class CategoryButtons(discord.ui.View):
    def __init__(self, on_select):
        super().__init__(timeout=300)
        self.on_select = on_select

    @discord.ui.button(label="Books", style=discord.ButtonStyle.primary)
    async def books(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.on_select(interaction, 'books')

    @discord.ui.button(label="Movies", style=discord.ButtonStyle.primary)
    async def movies(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.on_select(interaction, 'movies')

    @discord.ui.button(label="TV", style=discord.ButtonStyle.primary)
    async def tv(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.on_select(interaction, 'tv')

    @discord.ui.button(label="Music", style=discord.ButtonStyle.primary)
    async def music(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.on_select(interaction, 'music')


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
    ):
        super().__init__(timeout=600)
        self.base_url = base_url
        self.page_size = page_size
        self.get_books_data = get_books_data
        self.get_movies = get_movies
        self.get_tv = get_tv
        self.get_music = get_music
        self.category: Optional[str] = None
        self._show_category_buttons()

    def _embed(self, title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description)

    def _show_category_buttons(self):
        self.clear_items()
        async def on_sel(inter: discord.Interaction, kind: str):
            self.category = kind
            await self._show_category(inter)
        self.add_item(CategoryButtons(on_sel))

    async def _show_category(self, interaction: discord.Interaction):
        self.clear_items()
        title = f"Browse: {self.category.title() if self.category else ''}"
        if self.category == 'books':
            data = self.get_books_data()
            authors = sorted(list(data.keys()))
            if not authors:
                await interaction.response.edit_message(embed=self._embed(title, "No authors found."), view=self)
                return
            self.add_item(ItemSelect("Select an author", authors[:25]))
            await interaction.response.edit_message(embed=self._embed(title, "Pick an author to get links for all their books."), view=self)
        elif self.category == 'movies':
            movies = sorted(self.get_movies())
            if not movies:
                await interaction.response.edit_message(embed=self._embed(title, "No movies found."), view=self)
                return
            self.add_item(ItemSelect("Select a movie", movies[:25]))
            await interaction.response.edit_message(embed=self._embed(title, "Pick a movie to get links."), view=self)
        elif self.category == 'tv':
            shows = sorted(list(self.get_tv().keys()))
            if not shows:
                await interaction.response.edit_message(embed=self._embed(title, "No TV shows found."), view=self)
                return
            self.add_item(ItemSelect("Select a TV show", shows[:25]))
            await interaction.response.edit_message(embed=self._embed(title, "Pick a TV show to get links for all episodes."), view=self)
        elif self.category == 'music':
            artists = sorted(list(self.get_music().keys()))
            if not artists:
                await interaction.response.edit_message(embed=self._embed(title, "No music artists found."), view=self)
                return
            self.add_item(ItemSelect("Select an artist", artists[:25]))
            await interaction.response.edit_message(embed=self._embed(title, "Pick an artist to get links for tracks."), view=self)
        else:
            self._show_category_buttons()
            await interaction.response.edit_message(embed=self._embed("Browse", "Choose a category."), view=self)

    async def on_item_selected(self, interaction: discord.Interaction, item_name: str):
        if not self.category:
            await interaction.response.send_message("No category selected.", ephemeral=True)
            return
        url = f"{self.base_url}/links?kind={self.category}&name={discord.utils.escape_markdown(item_name)}"
        # Offer an "Open Links" button
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open Links", url=url))
        await interaction.response.send_message(f"Links for {self.category.title()}: {item_name}", view=view, ephemeral=True)

    @staticmethod
    async def send(ctx, base_url: str, page_size: int, get_books_data, get_movies, get_tv, get_music):
        view = UnifiedBrowserView(base_url, page_size, get_books_data, get_movies, get_tv, get_music)
        await ctx.send(embed=discord.Embed(title="Browse", description="Choose a category."), view=view)
