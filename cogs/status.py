import discord
from discord.ext import commands

from cogs.system import _build_usage_embed, _build_server_list_embeds
from cogs.stats import _collect_user_rows, _build_users_embed, USERS_PAGE_SIZE


# ═══════════════════════════════════════════════════════════════════════════════
#  VIEW
# ═══════════════════════════════════════════════════════════════════════════════

class StatusView(discord.ui.View):
    def __init__(self, bot: commands.Bot, author_id: int):
        super().__init__(timeout=180)
        self.bot            = bot
        self.author_id      = author_id
        self.page           = "users"   # current tab
        self.user_subpage   = 0
        self.server_subpage = 0

        # Pre-fetch data once
        self._user_rows     = _collect_user_rows(bot)
        self._server_embeds = _build_server_list_embeds(bot)

        self._rebuild()

    # ── helpers ───────────────────────────────────────────────────────────────

    @property
    def _user_total_pages(self) -> int:
        return max(1, -(-len(self._user_rows) // USERS_PAGE_SIZE))

    @property
    def _server_total_pages(self) -> int:
        return len(self._server_embeds)

    def _current_embed(self) -> discord.Embed:
        if self.page == "users":
            return _build_users_embed(self._user_rows, self.user_subpage, self._user_total_pages)
        elif self.page == "servers":
            return self._server_embeds[self.server_subpage]
        else:  # usage
            return _build_usage_embed(self.bot)

    @property
    def _current_subpage(self) -> int:
        return self.user_subpage if self.page == "users" else self.server_subpage

    @property
    def _current_total(self) -> int:
        return self._user_total_pages if self.page == "users" else self._server_total_pages

    # ── build buttons ─────────────────────────────────────────────────────────

    def _rebuild(self):
        self.clear_items()

        # Row 0 — tab buttons
        for label, page_id in [("👥 Users", "users"), ("🌐 Servers", "servers"), ("📊 Usage", "usage")]:
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary if self.page == page_id else discord.ButtonStyle.secondary,
                row=0,
            )
            btn.callback = self._make_tab_cb(page_id)
            self.add_item(btn)

        # Row 1 — prev/next (only for paginated tabs)
        if self.page in ("users", "servers"):
            total   = self._current_total
            subpage = self._current_subpage

            prev = discord.ui.Button(
                label="◀ Prev", style=discord.ButtonStyle.secondary,
                disabled=(subpage <= 0), row=1,
            )
            indicator = discord.ui.Button(
                label=f"{subpage + 1} / {total}",
                style=discord.ButtonStyle.secondary,
                disabled=True, row=1,
            )
            nxt = discord.ui.Button(
                label="Next ▶", style=discord.ButtonStyle.secondary,
                disabled=(subpage >= total - 1), row=1,
            )
            prev.callback = self._prev_cb
            nxt.callback  = self._next_cb
            self.add_item(prev)
            self.add_item(indicator)
            self.add_item(nxt)

        # Row 2 — refresh
        refresh = discord.ui.Button(label="🔄 Refresh", style=discord.ButtonStyle.success, row=2)
        refresh.callback = self._refresh_cb
        self.add_item(refresh)

    # ── callbacks ─────────────────────────────────────────────────────────────

    def _make_tab_cb(self, target: str):
        async def cb(interaction: discord.Interaction):
            self.page = target
            self._rebuild()
            await interaction.response.edit_message(embed=self._current_embed(), view=self)
        return cb

    async def _prev_cb(self, interaction: discord.Interaction):
        if self.page == "users":
            self.user_subpage = max(0, self.user_subpage - 1)
        else:
            self.server_subpage = max(0, self.server_subpage - 1)
        self._rebuild()
        await interaction.response.edit_message(embed=self._current_embed(), view=self)

    async def _next_cb(self, interaction: discord.Interaction):
        if self.page == "users":
            self.user_subpage = min(self._user_total_pages - 1, self.user_subpage + 1)
        else:
            self.server_subpage = min(self._server_total_pages - 1, self.server_subpage + 1)
        self._rebuild()
        await interaction.response.edit_message(embed=self._current_embed(), view=self)

    async def _refresh_cb(self, interaction: discord.Interaction):
        # Re-fetch fresh data
        self._user_rows     = _collect_user_rows(self.bot)
        self._server_embeds = _build_server_list_embeds(self.bot)
        self.user_subpage   = 0
        self.server_subpage = 0
        self._rebuild()
        await interaction.response.edit_message(embed=self._current_embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("🚫 Only the bot owner can use this.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ═══════════════════════════════════════════════════════════════════════════════
#  COG
# ═══════════════════════════════════════════════════════════════════════════════

class Status(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="status")
    @commands.is_owner()
    async def prefix_status(self, ctx: commands.Context):
        async with ctx.typing():
            view  = StatusView(self.bot, ctx.author.id)
            embed = view._current_embed()
        await ctx.reply(embed=embed, view=view)

    @prefix_status.error
    async def status_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.NotOwner):
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
            await ctx.send("🚫 Owner only.", delete_after=3)


async def setup(bot: commands.Bot):
    await bot.add_cog(Status(bot))