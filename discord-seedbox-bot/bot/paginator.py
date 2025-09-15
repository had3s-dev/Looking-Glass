from typing import List, Optional

import discord


class Paginator(discord.ui.View):
    def __init__(self, pages: List[str], title: Optional[str] = None, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.title = title
        self.index = 0
        self._update_state()

    def _embed(self) -> discord.Embed:
        description = self.pages[self.index] if self.pages else "(empty)"
        embed = discord.Embed(description=description)
        if self.title:
            embed.title = self.title
        embed.set_footer(text=f"Page {self.index + 1}/{len(self.pages) if self.pages else 1}")
        return embed

    def _update_state(self):
        self.first.disabled = self.index <= 0
        self.prev.disabled = self.index <= 0
        self.next.disabled = self.index >= (len(self.pages) - 1)
        self.last.disabled = self.index >= (len(self.pages) - 1)

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = 0
        self._update_state()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index > 0:
            self.index -= 1
        self._update_state()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index < len(self.pages) - 1:
            self.index += 1
        self._update_state()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, len(self.pages) - 1)
        self._update_state()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @staticmethod
    async def send(ctx, title: Optional[str], pages: List[str]):
        if not pages:
            await ctx.send("Nothing to display.")
            return
        view = Paginator(pages=pages, title=title)
        await ctx.send(embed=view._embed(), view=view)
