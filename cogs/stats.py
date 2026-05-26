"""
Stats cog.

CHANGES:
- !users / /users: paginated embed with ◀ ▶ arrow buttons (same pattern as
  help.py). Shows username, user ID, messages, ~tokens, AI usage today,
  first/last seen, ban status. Admin-only.
- Buttons are locked to the invoking admin (interaction_check).
- Buttons and view time out after 120 s of inactivity (same as help).
"""
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone
import time
from cogs.state import get_stats, get_all_stats, get_all_bans, get_all_rate_limits, _today_utc, seen_users
from cogs.admin import is_admin

USERS_PAGE_SIZE = 10   # users per embed page


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts_to_discord(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return discord.utils.format_dt(dt, style="R")

def _ts_to_short(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _format_stats(user: discord.User | discord.Member, data: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"📊 Jarvis Stats — {user.display_name}",
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Messages Sent", value=f"`{data['messages']:,}`",   inline=True)
    embed.add_field(name="~Tokens Used",  value=f"`{data['tokens_est']:,}`", inline=True)
    embed.add_field(name="\u200b",        value="\u200b",                    inline=True)
    embed.add_field(name="First Interaction", value=_ts_to_discord(data["first_seen"]), inline=True)
    embed.add_field(name="Last Interaction",  value=_ts_to_discord(data["last_seen"]),  inline=True)
    embed.set_footer(text="Token count is an estimate (~4 chars/token)")
    return embed


# ── Data collection ───────────────────────────────────────────────────────────

def _collect_user_rows(bot: commands.Bot) -> list[dict]:
    """
    Merge seen + stats + bans + rate_limits into a list of row dicts,
    sorted by message count descending.

    seen stores int user IDs; stats/bans/rate_limits use str keys.
    We normalise everything to str so the union is complete and all
    24 (or however many) users appear even if stats weren't recorded yet.
    """
    all_stats = get_all_stats()
    all_bans  = get_all_bans()
    all_rl    = get_all_rate_limits()
    today     = _today_utc()

    # seen_users stores ints — convert to str to match the other stores
    seen_str = {str(uid) for uid in seen_users}
    all_uids = seen_str | set(all_stats) | set(all_bans) | set(all_rl)
    rows = []

    for uid_str in all_uids:
        stats = all_stats.get(uid_str, {})
        ban   = all_bans.get(uid_str)
        rl    = all_rl.get(uid_str, {})

        try:
            user     = bot.get_user(int(uid_str))
            username = str(user) if user else f"Unknown#{uid_str[-4:]}"
        except Exception:
            username = f"Unknown#{uid_str[-4:]}"

        is_banned   = False
        ban_reason  = None
        ban_expires = None
        if ban:
            expires = ban.get("expires")
            if expires is None or time.time() < expires:
                is_banned   = True
                ban_reason  = ban.get("reason")
                ban_expires = expires

        ai_today = rl.get("count", 0) if rl.get("day") == today else 0

        rows.append({
            "uid":         uid_str,
            "username":    username,
            "messages":    stats.get("messages",   0),
            "tokens_est":  stats.get("tokens_est", 0),
            "first_seen":  stats.get("first_seen", 0),
            "last_seen":   stats.get("last_seen",  0),
            "ai_today":    ai_today,
            "is_banned":   is_banned,
            "ban_reason":  ban_reason,
            "ban_expires": ban_expires,
        })

    rows.sort(key=lambda r: r["messages"], reverse=True)
    return rows


# ── Embed builder ─────────────────────────────────────────────────────────────

def _build_users_embed(rows: list[dict], page: int, total_pages: int) -> discord.Embed:
    """Build a single page embed for the users list."""
    total   = len(rows)
    today   = _today_utc()
    start   = page * USERS_PAGE_SIZE
    chunk   = rows[start : start + USERS_PAGE_SIZE]

    embed = discord.Embed(
        title=f"👥 Jarvis Users — {total:,} total",
        color=discord.Color.blurple(),
        description=(
            f"Page **{page + 1}/{total_pages}** • "
            f"Sorted by most messages • "
            f"`{today}`"
        ),
    )

    for r in chunk:
        ban_tag   = " 🚫" if r["is_banned"] else ""
        limit_tag = " ⏳" if r["ai_today"] >= 50 else ""

        if r["is_banned"]:
            if r["ban_expires"]:
                exp_dt  = datetime.fromtimestamp(r["ban_expires"], tz=timezone.utc)
                exp_str = discord.utils.format_dt(exp_dt, style="R")
                ban_line = f"\n  🚫 Banned — expires {exp_str} | {r['ban_reason'] or 'No reason'}"
            else:
                ban_line = f"\n  🚫 Permanently banned | {r['ban_reason'] or 'No reason'}"
        else:
            ban_line = ""

        # Skip time fields if no stats recorded yet
        time_line = ""
        if r["first_seen"]:
            time_line = (
                f" • **First:** `{_ts_to_short(r['first_seen'])}`"
                f" • **Last:** {_ts_to_discord(r['last_seen'])}"
            )

        embed.add_field(
            name=f"`{r['username']}`{ban_tag}{limit_tag}",
            value=(
                f"**ID:** `{r['uid']}`\n"
                f"**Messages:** `{r['messages']:,}` "
                f"• **~Tokens:** `{r['tokens_est']:,}` "
                f"• **AI today:** `{r['ai_today']}/50`"
                f"{time_line}"
                f"{ban_line}"
            ),
            inline=False,
        )

    embed.set_footer(
        text="🚫 = banned  ⏳ = daily limit hit  •  Token count is an estimate"
    )
    return embed


# ── Paginated View ────────────────────────────────────────────────────────────

class UsersView(discord.ui.View):
    def __init__(self, author_id: int, rows: list[dict]):
        super().__init__(timeout=120)
        self.author_id   = author_id
        self.rows        = rows
        self.page        = 0
        self.total_pages = max(1, -(-len(rows) // USERS_PAGE_SIZE))  # ceiling div
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.total_pages - 1
        self.page_btn.label    = f"{self.page + 1} / {self.total_pages}"

    def current_embed(self) -> discord.Embed:
        return _build_users_embed(self.rows, self.page, self.total_pages)

    async def _update(self, interaction: discord.Interaction):
        self._update_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "⚠️ Only the admin who ran this command can use these buttons.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self._update(interaction)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.primary, disabled=True)
    async def page_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Non-interactive — just shows current page. Required: must have a callback.
        await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self._update(interaction)


# ── Cog ───────────────────────────────────────────────────────────────────────

class Stats(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /stats & !stats ───────────────────────────────────────────────────────

    @commands.command(name="stats")
    async def prefix_stats(self, ctx: commands.Context, user: discord.User = None):
        """
        !stats          — view your own stats
        !stats @user    — view another user's stats (admins only)
        """
        if user is not None and not is_admin(ctx.author):
            await ctx.reply("🚫 Only admins can view other users' stats.")
            return

        target = user or ctx.author
        data   = get_stats(target.id)
        if not data:
            msg = (
                "📭 You haven't interacted with Jarvis yet."
                if target.id == ctx.author.id
                else f"📭 **{target}** has no recorded interactions with Jarvis."
            )
            await ctx.reply(msg)
            return

        await ctx.reply(embed=_format_stats(target, data))

    @app_commands.command(name="stats", description="View Jarvis usage stats")
    @app_commands.describe(user="User to look up (admins only — leave empty for your own stats)")
    async def slash_stats(self, interaction: discord.Interaction, user: discord.User = None):
        if user is not None and not is_admin(interaction.user):
            await interaction.response.send_message(
                "🚫 Only admins can view other users' stats.", ephemeral=True
            )
            return

        target = user or interaction.user
        data   = get_stats(target.id)
        if not data:
            msg = (
                "📭 You haven't interacted with Jarvis yet."
                if target.id == interaction.user.id
                else f"📭 **{target}** has no recorded interactions with Jarvis."
            )
            await interaction.response.send_message(msg, ephemeral=True)
            return

        await interaction.response.send_message(embed=_format_stats(target, data))

    # ── !users & /users ───────────────────────────────────────────────────────

    @commands.command(name="users")
    async def prefix_users(self, ctx: commands.Context):
        """[Admin] Browse all users Jarvis has ever seen, with arrow buttons."""
        if not is_admin(ctx.author):
            await ctx.reply("🚫 This command is for admins only.")
            return

        rows = _collect_user_rows(self.bot)
        if not rows:
            await ctx.reply("📭 No users recorded yet.")
            return

        view = UsersView(author_id=ctx.author.id, rows=rows)
        await ctx.reply(embed=view.current_embed(), view=view)

    @app_commands.command(name="users", description="[Admin] Browse all users Jarvis has seen")
    async def slash_users(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "🚫 This command is for admins only.", ephemeral=True
            )
            return

        rows = _collect_user_rows(self.bot)
        if not rows:
            await interaction.response.send_message("📭 No users recorded yet.", ephemeral=True)
            return

        view = UsersView(author_id=interaction.user.id, rows=rows)
        await interaction.response.send_message(embed=view.current_embed(), view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Stats(bot))