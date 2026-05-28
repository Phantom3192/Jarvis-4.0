"""
Stats cog.

CHANGES vs previous version:
- /stats personal embed: activity-based colour, daily AI usage bar with %,
  rank badge (by message count), memory count, cleaner layout.
- /users admin embed: server-wide summary header (total msgs, tokens, active
  today, banned count), rank numbers on each row, cleaner value lines.
- "No data" responses now use a tidy embed instead of a bare string.
"""
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone
import time
from cogs.state import (
    get_stats, get_all_stats, get_all_bans, get_all_rate_limits,
    _today_utc, seen_users, DAILY_AI_LIMIT, get_ai_usage, get_ai_limit,
)
from cogs.admin import is_admin

USERS_PAGE_SIZE = 10


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts_to_discord(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return discord.utils.format_dt(dt, style="R")

def _ts_to_short(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%d %b %Y")

def _bar(filled: int, total: int = 10, fill: str = "█", empty: str = "░") -> str:
    f = round((filled / total) * 10) if total else 0
    return fill * f + empty * (10 - f)

def _activity_color(messages: int) -> discord.Color:
    """Shift embed colour based on how active a user is."""
    if messages >= 500:
        return discord.Color.gold()
    if messages >= 100:
        return discord.Color.blurple()
    if messages >= 20:
        return discord.Color.teal()
    return discord.Color.greyple()

def _rank_badge(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"#{rank}")


# ── Personal stats embed ──────────────────────────────────────────────────────

def _format_stats(
    user: discord.User | discord.Member,
    data: dict,
    rank: int | None = None,
    memory_count: int = 0,
) -> discord.Embed:
    msgs       = data.get("messages", 0)
    tokens     = data.get("tokens_est", 0)
    first_seen = data.get("first_seen", 0)
    last_seen  = data.get("last_seen", 0)

    ai_count, _ = get_ai_usage(user.id)
    ai_limit    = get_ai_limit()
    ai_pct      = round((ai_count / ai_limit) * 100) if ai_limit else 0
    ai_bar      = _bar(ai_count, ai_limit)

    if ai_pct >= 90:
        ai_status = "🔴 Almost out"
    elif ai_pct >= 60:
        ai_status = "🟡 Running low"
    else:
        ai_status = "🟢 Good to go"

    color = _activity_color(msgs)
    rank_str = f"  •  Rank {_rank_badge(rank)}" if rank else ""

    embed = discord.Embed(
        title=f"📊 {user.display_name}'s Jarvis Stats{rank_str}",
        color=color,
    )
    embed.set_thumbnail(url=user.display_avatar.url)

    # Row 1 — core numbers
    embed.add_field(name="💬 Messages",   value=f"`{msgs:,}`",    inline=True)
    embed.add_field(name="🔤 ~Tokens",    value=f"`{tokens:,}`",  inline=True)
    if memory_count:
        embed.add_field(name="🧠 Memories", value=f"`{memory_count}`", inline=True)
    else:
        embed.add_field(name="\u200b", value="\u200b", inline=True)

    # Row 2 — timeline
    if first_seen:
        embed.add_field(name="📅 First Seen", value=_ts_to_discord(first_seen), inline=True)
        embed.add_field(name="🕐 Last Active", value=_ts_to_discord(last_seen),  inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)

    # Row 3 — daily AI bar
    embed.add_field(
        name="⚡ Today's AI Usage",
        value=(
            f"`{ai_bar}` **{ai_count}/{ai_limit}** ({ai_pct}%)\n"
            f"{ai_status}  •  Resets midnight UTC"
        ),
        inline=False,
    )

    embed.set_footer(text="Token count is an estimate (~4 chars/token)")
    return embed


def _no_stats_embed(user: discord.User | discord.Member, is_self: bool) -> discord.Embed:
    embed = discord.Embed(
        title="📭 No Stats Yet",
        description=(
            "You haven't interacted with Jarvis yet. Send a message to get started!"
            if is_self
            else f"**{user.display_name}** hasn't interacted with Jarvis yet."
        ),
        color=discord.Color.greyple(),
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    return embed


# ── Admin users list ──────────────────────────────────────────────────────────

def _collect_user_rows(bot: commands.Bot) -> list[dict]:
    all_stats = get_all_stats()
    all_bans  = get_all_bans()
    all_rl    = get_all_rate_limits()
    today     = _today_utc()

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


def _server_summary(rows: list[dict]) -> str:
    """One-line summary stats for the embed description."""
    today        = _today_utc()
    total_msgs   = sum(r["messages"]   for r in rows)
    total_tokens = sum(r["tokens_est"] for r in rows)
    active_today = sum(1 for r in rows if r["ai_today"] > 0)
    banned_count = sum(1 for r in rows if r["is_banned"])
    limit_hit    = sum(1 for r in rows if r["ai_today"] >= DAILY_AI_LIMIT)

    lines = [
        f"💬 **{total_msgs:,}** total messages  •  🔤 **{total_tokens:,}** ~tokens",
        f"⚡ **{active_today}** active today  •  🚫 **{banned_count}** banned  •  ⏳ **{limit_hit}** at daily limit",
    ]
    return "\n".join(lines)


def _build_users_embed(rows: list[dict], page: int, total_pages: int) -> discord.Embed:
    total = len(rows)
    today = _today_utc()
    start = page * USERS_PAGE_SIZE
    chunk = rows[start : start + USERS_PAGE_SIZE]

    embed = discord.Embed(
        title=f"👥 Jarvis Users — {total:,} total",
        description=(
            _server_summary(rows) +
            f"\n\n*Page **{page + 1} / {total_pages}** • sorted by messages • `{today}`*"
        ),
        color=discord.Color.blurple(),
    )

    for i, r in enumerate(chunk, start=start + 1):
        rank     = _rank_badge(i)
        ban_tag  = " 🚫" if r["is_banned"] else ""
        limit_tag= " ⏳" if r["ai_today"] >= DAILY_AI_LIMIT else ""

        if r["is_banned"]:
            if r["ban_expires"]:
                exp_dt  = datetime.fromtimestamp(r["ban_expires"], tz=timezone.utc)
                exp_str = discord.utils.format_dt(exp_dt, style="R")
                ban_line = f"\n  🚫 Banned — expires {exp_str}  •  _{r['ban_reason'] or 'No reason'}_"
            else:
                ban_line = f"\n  🚫 Permanently banned  •  _{r['ban_reason'] or 'No reason'}_"
        else:
            ban_line = ""

        time_line = ""
        if r["first_seen"]:
            time_line = (
                f"  •  📅 `{_ts_to_short(r['first_seen'])}`"
                f"  •  🕐 {_ts_to_discord(r['last_seen'])}"
            )

        ai_bar = _bar(r["ai_today"], DAILY_AI_LIMIT)

        embed.add_field(
            name=f"{rank} `{r['username']}`{ban_tag}{limit_tag}",
            value=(
                f"ID: `{r['uid']}`{time_line}\n"
                f"💬 `{r['messages']:,}` msgs  •  🔤 `{r['tokens_est']:,}` tokens  •  "
                f"⚡ `{ai_bar}` {r['ai_today']}/{DAILY_AI_LIMIT}"
                f"{ban_line}"
            ),
            inline=False,
        )

    embed.set_footer(text="🚫 banned  ⏳ daily limit hit  •  Token count is an estimate")
    return embed


# ── Paginated View ────────────────────────────────────────────────────────────

class UsersView(discord.ui.View):
    def __init__(self, author_id: int, rows: list[dict]):
        super().__init__(timeout=120)
        self.author_id   = author_id
        self.rows        = rows
        self.page        = 0
        self.total_pages = max(1, -(-len(rows) // USERS_PAGE_SIZE))
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
        await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self._update(interaction)


# ── Cog ───────────────────────────────────────────────────────────────────────

class Stats(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _user_rank(self, user_id: int) -> int | None:
        """Return 1-based rank of user by message count, or None if no stats."""
        all_stats = get_all_stats()
        uid_str   = str(user_id)
        if uid_str not in all_stats:
            return None
        sorted_ids = sorted(all_stats, key=lambda k: all_stats[k].get("messages", 0), reverse=True)
        try:
            return sorted_ids.index(uid_str) + 1
        except ValueError:
            return None

    async def _memory_count(self, user_id: int) -> int:
        try:
            from cogs.memory import get_facts_count
            return await get_facts_count(user_id)
        except Exception:
            return 0

    # ── /stats & !stats ───────────────────────────────────────────────────────

    @commands.command(name="stats")
    async def prefix_stats(self, ctx: commands.Context, user: discord.User = None):
        """!stats — your stats  |  !stats @user — admin only"""
        if user is not None and not is_admin(ctx.author):
            await ctx.reply("🚫 Only admins can view other users' stats.")
            return

        target = user or ctx.author
        data   = get_stats(target.id)
        if not data:
            await ctx.reply(embed=_no_stats_embed(target, target.id == ctx.author.id))
            return

        rank   = self._user_rank(target.id)
        mem    = await self._memory_count(target.id)
        await ctx.reply(embed=_format_stats(target, data, rank=rank, memory_count=mem))

    @app_commands.command(name="stats", description="View your Jarvis usage stats")
    @app_commands.describe(user="User to look up (admins only — leave empty for your own stats)")
    async def slash_stats(self, interaction: discord.Interaction, user: discord.User = None):
        if user is not None and not is_admin(interaction.user):
            await interaction.response.send_message(
                "🚫 Only admins can view other users' stats.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=bool(user))
        target = user or interaction.user
        data   = get_stats(target.id)
        if not data:
            await interaction.followup.send(
                embed=_no_stats_embed(target, target.id == interaction.user.id),
                ephemeral=True,
            )
            return

        rank = self._user_rank(target.id)
        mem  = await self._memory_count(target.id)
        await interaction.followup.send(embed=_format_stats(target, data, rank=rank, memory_count=mem))

    # ── !users & /users ───────────────────────────────────────────────────────

    @commands.command(name="users")
    async def prefix_users(self, ctx: commands.Context):
        """[Admin] Browse all users Jarvis has seen, with arrow buttons."""
        if not is_admin(ctx.author):
            await ctx.reply("🚫 This command is for admins only.")
            return

        rows = _collect_user_rows(self.bot)
        if not rows:
            await ctx.reply(embed=discord.Embed(
                title="📭 No Users Yet",
                description="No one has interacted with Jarvis yet.",
                color=discord.Color.greyple(),
            ))
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
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="📭 No Users Yet",
                    description="No one has interacted with Jarvis yet.",
                    color=discord.Color.greyple(),
                ),
                ephemeral=True,
            )
            return

        view = UsersView(author_id=interaction.user.id, rows=rows)
        await interaction.response.send_message(embed=view.current_embed(), view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Stats(bot))