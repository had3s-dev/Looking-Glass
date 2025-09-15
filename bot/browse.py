from typing import Dict, List, Optional

import discord


def chunk_list(items: List[str], size: int) -> List[List[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class BooksView(discord.ui.View):
    def __init__(self, author: str, books: List[str], on_back, page_size: int = 20, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.author = author
        self.pages = ["\n".join(page) for page in chunk_list([f"{i+1}. {b}" for i, b in enumerate(books)], page_size)]
        if not self.pages:
            self.pages = ["(no books)"]
        self.index = 0
        self.on_back = on_back  # callable(ctx or interaction)
        self._update_state()

    def _embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"Books by {self.author} ({self.total_books})", description=self.pages[self.index])
        embed.set_footer(text=f"Page {self.index + 1}/{len(self.pages)}")
        return embed

    @property
    def total_books(self) -> int:
        # Count numeric lines in current pages list
        count = 0
        for p in self.pages:
            count += sum(1 for _ in p.splitlines())
        return count

    def _update_state(self):
        self.first_btn.disabled = self.index <= 0
        self.prev_btn.disabled = self.index <= 0
        self.next_btn.disabled = self.index >= len(self.pages) - 1
        self.last_btn.disabled = self.index >= len(self.pages) - 1

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary)
    async def first_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = 0
        self._update_state()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index > 0:
            self.index -= 1
        self._update_state()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index < len(self.pages) - 1:
            self.index += 1
        self._update_state()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary)
    async def last_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, len(self.pages) - 1)
        self._update_state()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Back to Authors", style=discord.ButtonStyle.primary)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.on_back(interaction)


class AuthorSelect(discord.ui.Select):
    def __init__(self, authors: List[str], page_index: int, per_page: int):
        self.authors = authors
        self.page_index = page_index
        self.per_page = per_page
        start = page_index * per_page
        end = start + per_page
        page_authors = authors[start:end]
        options = [discord.SelectOption(label=a, value=a) for a in page_authors]
        super().__init__(placeholder=f"Select an author ({start+1}-{min(end, len(authors))} of {len(authors)})", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view: AuthorBrowserView = self.view  # type: ignore
        author = self.values[0]
        await view.show_books(interaction, author)


class AuthorBrowserView(discord.ui.View):
    def __init__(self, data: Dict[str, List[str]], per_page: int = 25, title: Optional[str] = None, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.data = data
        self.title = title or "Authors"
        self.per_page = max(1, min(25, per_page))  # Discord select max options = 25
        self.index = 0
        self.authors = sorted(list(self.data.keys()))
        self.total = len(self.authors)
        self._rebuild_components()

    def _embed(self) -> discord.Embed:
        start = self.index * self.per_page
        end = min(start + self.per_page, self.total)
        description = "Choose an author from the dropdown below."
        embed = discord.Embed(title=f"{self.title} ({self.total})", description=description)
        embed.set_footer(text=f"Page {self.index + 1}/{self.page_count}")
        return embed

    @property
    def page_count(self) -> int:
        return max(1, (self.total + self.per_page - 1) // self.per_page)

    def _rebuild_components(self):
        # Clear existing items
        self.clear_items()
        # Add select for current page
        self.add_item(AuthorSelect(self.authors, self.index, self.per_page))
        # Add navigation buttons
        self.add_item(self.first_btn)
        self.add_item(self.prev_btn)
        self.add_item(self.next_btn)
        self.add_item(self.last_btn)
        # Disable buttons appropriately
        self.first_btn.disabled = self.index <= 0
        self.prev_btn.disabled = self.index <= 0
        self.next_btn.disabled = self.index >= self.page_count - 1
        self.last_btn.disabled = self.index >= self.page_count - 1

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary)
    async def first_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = 0
        self._rebuild_components()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index > 0:
            self.index -= 1
        self._rebuild_components()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index < self.page_count - 1:
            self.index += 1
        self._rebuild_components()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary)
    async def last_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.page_count - 1)
        self._rebuild_components()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def show_books(self, interaction: discord.Interaction, author: str):
        books = sorted(self.data.get(author, []))
        async def go_back(inter: discord.Interaction):
            # Repost the authors view
            self._rebuild_components()
            await inter.response.edit_message(embed=self._embed(), view=self)
        view = BooksView(author=author, books=books, on_back=go_back)
        await interaction.response.edit_message(embed=view._embed(), view=view)

    @staticmethod
    async def send(ctx, data: Dict[str, List[str]]):
        view = AuthorBrowserView(data=data)
        await ctx.send(embed=view._embed(), view=view)
