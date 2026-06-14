"""
Admin cog — bot-ban management and admin utilities.

OPTIMISATIONS vs original:
- is_admin: precompute a frozenset of lowercase names instead of rebuilding
  a list comprehension on every call (was O(n) per check, now O(1))
- parse_duration: unchanged but moved multipliers to a module-level constant
  so the dict is not rebuilt on every call
- prefix_botbans / slash_botbans: shared helper _format_ban_lines() removes
  ~20 lines of duplicated code between the two commands
- import time moved to module level (was buried inside __init__ and two methods)
- Removed bare `import json`, `import aiohttp` — neither was used in this file
"""
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import time
from cogs.state import bot_bans, save_bans, is_bot_banned, reset_ai_usage, get_setting, set_setting, _data, _schedule_save
from cogs.help import AdminHelpView, _build_admin_overview_embed
import re


def _parse_time_arg(val: str) -> float:
    """Parse a time argument like '60', '60s', '5m', '1h' into seconds."""
    if val is None:
        return None
    val = val.strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)([smh]?)$", val)
    if not m:
        raise ValueError("invalid time")
    num = float(m.group(1))
    suf = m.group(2)
    if suf == "s" or suf == "":
        return num
    if suf == "m":
        return num * 60.0
    if suf == "h":
        return num * 3600.0
    return num

# ── Config ────────────────────────────────────────────────────────────────────

LOG_WEBHOOK_URL = os.getenv("LOG_WEBHOOK_URL", "")

ADMIN_USERNAMES: frozenset[str] = frozenset({
    "phantom_3192",   # ← add more Discord usernames here (all lowercase)
})

# ── Duration parser ───────────────────────────────────────────────────────────

_UNIT_MULTIPLIERS: dict[str, int] = {
    "minute": 60,    "minutes": 60,    "min": 60,   "m": 60,
    "hour":   3600,  "hours":   3600,  "hr":  3600, "h": 3600,
    "day":    86400, "days":    86400, "d":   86400,
    "week":   604800,"weeks":   604800,"w":   604800,
}

def parse_duration(amount: int, unit: str) -> int:
    return amount * _UNIT_MULTIPLIERS.get(unit.lower(), -1)

_DURATION_RE = re.compile(
    r'^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)$',
    re.IGNORECASE
)

def parse_duration_str(s: str):
    """Parse '7d', '2h', '30m', '1week' etc.
    Returns (seconds, label) or None if invalid."""
    match = _DURATION_RE.match(s.strip())
    if not match:
        return None
    amount = int(match.group(1))
    unit   = match.group(2).lower()
    secs   = parse_duration(amount, unit)
    if secs <= 0:
        return None
    short = {
        "m":"m","min":"m","mins":"m","minute":"m","minutes":"m",
        "h":"h","hr":"h","hrs":"h","hour":"h","hours":"h",
        "d":"d","day":"d","days":"d",
        "w":"w","week":"w","weeks":"w",
    }
    return secs, f"{amount}{short[unit]}"

# ── Helpers ───────────────────────────────────────────────────────────────────

def is_admin(user: discord.User | discord.Member) -> bool:
    """O(1) admin check using a pre-built frozenset."""
    return user.name.lower() in ADMIN_USERNAMES

BAN_USAGE = (
    "**Usage:** `!global-ban @user [duration] [reason]`\n"
    "**Duration formats:** `30m` `2h` `7d` `1w` *(omit for permanent)*\n"
    "**Examples:**\n"
    "`!global-ban @user spamming` — permanent ban\n"
    "`!global-ban @user 7d flooding` — 7-day temp ban\n"
    "`!global-ban @user 2h repeated spam` — 2-hour temp ban\n"
    "`!global-ban @user 30m calm down` — 30-minute temp ban"
)
UNBAN_USAGE = "**Usage:** `!global-unban <user_id>`"

# ── Guild-ban storage — persisted via state._data["guild_bans"] ───────────────
# Structure: { guild_id (int): {"reason": str, "banned_at": float} }
# Loaded from Turso at startup via _load_guild_bans(), saved on every change.

_guild_bans: dict[int, dict] = {}


def _load_guild_bans() -> None:
    """Load guild bans from the shared state store into _guild_bans."""
    raw = _data.get("guild_bans", {})
    _guild_bans.clear()
    for k, v in raw.items():
        try:
            _guild_bans[int(k)] = v
        except (ValueError, TypeError):
            pass


def _save_guild_bans() -> None:
    """Persist guild bans back to the shared state store (debounced write)."""
    _data["guild_bans"] = {str(k): v for k, v in _guild_bans.items()}
    _schedule_save("guild_bans")


def is_guild_banned(guild_id: int) -> bool:
    return guild_id in _guild_bans


async def _format_ban_lines(bot: commands.Bot) -> list[str]:
    """Build one display line per banned user. Shared by prefix and slash commands."""
    lines = []
    now = time.time()
    for uid_str, data in bot_bans.items():
        expires = data.get("expires")
        if expires:
            remaining = max(0, int(expires - now))
            hrs, rem  = divmod(remaining, 3600)
            mins      = rem // 60
            duration_str = f"⏱️ {hrs}h {mins}m remaining"
        else:
            duration_str = "🔴 Permanent"
        try:
            user     = await bot.fetch_user(int(uid_str))
            user_str = f"**{user.name}** (`{uid_str}`)"
        except Exception:
            user_str = f"Unknown User (`{uid_str}`)"
        lines.append(
            f"{user_str}\n"
            f"  ↳ {duration_str} | Reason: {data.get('reason', 'None')}\n"
            f"  ↳ Unban: `!global-unban {uid_str}`"
        )
    return lines


def _build_ban_list_embed(lines: list[str]) -> discord.Embed:
    chunk = "\n\n".join(lines[:20])
    note  = f"\n*...and {len(lines) - 20} more.*" if len(lines) > 20 else ""
    description = f"{chunk}{note}"
    if len(description) > 4000:
        description = description[:3997] + "..."
    embed = discord.Embed(
        title=f"🚫 Bot-banned users ({len(bot_bans)})",
        description=description,
        color=discord.Color.red(),
    )
    embed.set_footer(text="Use !global-unban <user_id> to unban a user.")
    return embed


async def _build_guild_ban_embed(bot: commands.Bot) -> discord.Embed:
    """Build an embed listing all guild-banned servers."""
    if not _guild_bans:
        return discord.Embed(
            title="✅ No guild bans",
            description="No guilds are currently banned from Jarvis.",
            color=discord.Color.green(),
        )
    lines = []
    for gid, data in _guild_bans.items():
        guild = bot.get_guild(gid)
        name  = guild.name if guild else "Unknown Server"
        ts    = discord.utils.format_dt(
            discord.utils.utcnow().__class__.fromtimestamp(data["banned_at"]),
            style="R",
        )
        lines.append(
            f"**{name}** (`{gid}`)\n"
            f"  ↳ Reason: {data['reason']}\n"
            f"  ↳ Banned: {ts}  |  Unban: `!guild-unban {gid}`"
        )
    chunk = "\n\n".join(lines[:20])
    if len(chunk) > 4000:
        chunk = chunk[:3997] + "..."
    embed = discord.Embed(
        title=f"🚫 Guild-banned servers ({len(_guild_bans)})",
        description=chunk,
        color=discord.Color.red(),
    )
    embed.set_footer(text="Use !guild-unban <guild_id> to unban a server.")
    return embed


# ── Cog ───────────────────────────────────────────────────────────────────────

class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.temp_ban_tasks: dict[int, asyncio.Task] = {}

        # Load persisted guild bans from Turso into _guild_bans
        _load_guild_bans()

        # Re-schedule any active temp bans that survived a restart
        now = time.time()
        for uid_str, data in list(bot_bans.items()):
            expires = data.get("expires")
            if expires is not None:
                remaining = expires - now
                if remaining <= 0:
                    del bot_bans[uid_str]
                    save_bans()
                else:
                    uid = int(uid_str)
                    self.temp_ban_tasks[uid] = asyncio.create_task(
                        self._unban_after(uid, remaining)
                    )

    # ── Shared core logic ─────────────────────────────────────────────────────

    async def _do_botban(
        self,
        user: discord.User | discord.Member,
        reason: str,
        duration: int,
        unit: str,
        send_msg,
    ) -> None:
        is_permanent = (duration == 0 or unit == "permanent")

        if is_permanent:
            bot_bans[str(user.id)] = {"reason": reason, "expires": None}
            save_bans()
            try:
                embed = discord.Embed(
                    title="🚫 Permanently Banned from Jarvis",
                    color=discord.Color.red(),
                )
                embed.add_field(name="Reason", value=reason, inline=False)
                embed.set_footer(text="If you believe this is a mistake, contact Phantom.")
                await user.send(embed=embed)
            except discord.Forbidden:
                pass
            await send_msg(
                f"🚫 **{user}** has been permanently banned from using Jarvis.\n"
                f"**Reason:** {reason}"
            )
        else:
            # duration is already in seconds, unit is the display label (e.g. "7d", "2h")
            seconds = duration
            expires = time.time() + seconds
            bot_bans[str(user.id)] = {"reason": reason, "expires": expires}
            save_bans()
            try:
                embed = discord.Embed(
                    title="⏱️ Temporarily Banned from Jarvis",
                    color=discord.Color.orange(),
                )
                embed.add_field(name="Duration", value=unit, inline=True)
                embed.add_field(name="Reason", value=reason, inline=False)
                embed.set_footer(text="Your access will be restored automatically when the ban expires.")
                await user.send(embed=embed)
            except discord.Forbidden:
                pass
            await send_msg(
                f"⏱️ **{user}** is banned from Jarvis for **{unit}**.\n"
                f"**Reason:** {reason}"
            )
            self.temp_ban_tasks[user.id] = asyncio.create_task(
                self._unban_after(user.id, seconds)
            )

    async def _do_botunban(self, user_id: int, send_msg) -> None:
        uid_str = str(user_id)
        if uid_str not in bot_bans:
            await send_msg("❌ That user is not bot-banned.")
            return
        del bot_bans[uid_str]
        save_bans()
        task = self.temp_ban_tasks.pop(user_id, None)
        if task:
            task.cancel()
        await send_msg(f"✅ User `{user_id}` has been unbanned from Jarvis.")

    async def _unban_after(self, user_id: int, seconds: float) -> None:
        await asyncio.sleep(seconds)
        uid_str = str(user_id)
        if uid_str in bot_bans:
            del bot_bans[uid_str]
            save_bans()
        self.temp_ban_tasks.pop(user_id, None)
        try:
            user = await self.bot.fetch_user(user_id)
            embed = discord.Embed(
                title="✅ Your Jarvis Ban Has Expired",
                description="Your temporary ban from **Jarvis** has lifted. You can use me again!",
                color=discord.Color.green(),
            )
            embed.set_footer(text="Welcome back!")
            await user.send(embed=embed)
        except (discord.Forbidden, discord.NotFound):
            pass

    async def _do_guild_ban(self, guild_id: int, reason: str, send_msg) -> None:
        """Ban a guild: record it and notify the owner if possible."""
        if guild_id in _guild_bans:
            await send_msg(f"⚠️ Guild `{guild_id}` is already banned.")
            return
        _guild_bans[guild_id] = {"reason": reason, "banned_at": time.time()}
        _save_guild_bans()

        guild = self.bot.get_guild(guild_id)
        if guild:
            # Try to notify the guild owner
            if guild.owner:
                try:
                    embed = discord.Embed(
                        title="🚫 Server Banned from Jarvis",
                        color=discord.Color.red(),
                    )
                    embed.add_field(name="Server", value=guild.name, inline=True)
                    embed.add_field(name="Reason", value=reason, inline=False)
                    embed.set_footer(text="If you believe this is a mistake, please contact Phantom.")
                    await guild.owner.send(embed=embed)
                except discord.Forbidden:
                    pass
            await send_msg(
                f"🚫 Guild **{guild.name}** (`{guild_id}`) has been banned from using Jarvis.\n"
                f"**Reason:** {reason}"
            )
        else:
            await send_msg(
                f"🚫 Guild `{guild_id}` has been banned from using Jarvis.\n"
                f"**Reason:** {reason}"
            )

    async def _do_guild_unban(self, guild_id: int, send_msg) -> None:
        """Remove a guild ban."""
        if guild_id not in _guild_bans:
            await send_msg(f"❌ Guild `{guild_id}` is not guild-banned.")
            return
        del _guild_bans[guild_id]
        _save_guild_bans()
        await send_msg(f"✅ Guild `{guild_id}` has been unbanned. Jarvis can be re-invited to that server.")

    async def cog_check(self, ctx: commands.Context) -> bool:
        if is_admin(ctx.author):
            return True
        if is_bot_banned(ctx.author.id):
            await ctx.reply("🚫 You are banned from using Jarvis.")
            return False
        return True

    # ── Prefix commands ───────────────────────────────────────────────────────

    @commands.command(name="global-ban")
    async def prefix_botban(self, ctx: commands.Context, user: discord.User = None, *args):
        if not is_admin(ctx.author):
            await ctx.reply("🚫 You don't have permission to use this command.")
            return
        if user is None:
            await ctx.reply(BAN_USAGE)
            return
        if user.id == ctx.author.id:
            await ctx.reply("❌ You can't ban yourself.")
            return
        if is_admin(user):
            await ctx.reply("❌ You can't ban another admin.")
            return
        if is_bot_banned(user.id):
            await ctx.reply(f"⚠️ **{user}** is already banned from Jarvis.")
            return

        duration, unit, reason = 0, "permanent", "No reason provided"

        if args:
            parsed = parse_duration_str(args[0])
            if parsed:
                # First arg is a duration like 7d, 2h, 30m
                secs, label = parsed
                duration = secs
                unit     = label
                reason   = " ".join(args[1:]) or "No reason provided"
            elif args[0].lower() == "permanent":
                reason = " ".join(args[1:]) or "No reason provided"
            else:
                # No duration — treat everything as reason (permanent ban)
                reason = " ".join(args)

        await self._do_botban(user, reason, duration, unit, ctx.reply)

    @commands.command(name="global-unban")
    async def prefix_botunban(self, ctx: commands.Context, user_id: str = None):
        if not is_admin(ctx.author):
            await ctx.reply("🚫 You don't have permission to use this command.")
            return
        if user_id is None:
            await ctx.reply(UNBAN_USAGE)
            return
        try:
            uid = int(user_id)
        except ValueError:
            await ctx.reply("❌ Invalid user ID.")
            return
        await self._do_botunban(uid, ctx.reply)

    @commands.command(name="global-bans")
    async def prefix_botbans(self, ctx: commands.Context):
        if not is_admin(ctx.author):
            await ctx.reply("🚫 You don't have permission to use this command.")
            return
        if not bot_bans:
            await ctx.reply("✅ No users are currently banned from Jarvis.")
            return
        lines = await _format_ban_lines(self.bot)
        await ctx.reply(embed=_build_ban_list_embed(lines))

    @commands.command(name="resetlimit")
    async def prefix_resetlimit(self, ctx: commands.Context, user: discord.User = None):
        """Admin only — reset a user's daily AI message limit."""
        if not is_admin(ctx.author):
            await ctx.reply("🚫 You don't have permission to use this command.")
            return
        if user is None:
            await ctx.reply("**Usage:** `!resetlimit @user`")
            return
        reset_ai_usage(user.id)
        await ctx.reply(f"✅ Daily AI limit reset for **{user}** — they can send AI messages again.")

    @commands.command(name="set-cooldown")
    @commands.is_owner()
    async def prefix_set_cooldown(self, ctx: commands.Context, seconds: str = None):
        """Bot owner only: set per-user command cooldown in seconds."""
        if seconds is None:
            current = get_setting("user_command_cooldown", 2.0)
            await ctx.reply(
                f"**Usage:** `!set-cooldown <seconds>`\nCurrent cooldown: {current} seconds"
            )
            return
        try:
            sec = float(seconds)
        except ValueError:
            await ctx.reply("❌ Invalid number.")
            return
        if sec < 0:
            await ctx.reply("❌ Cooldown must be >= 0.")
            return
        set_setting("user_command_cooldown", sec)
        await ctx.reply(f"✅ Command cooldown set to {sec} seconds.")

    @commands.command(name="set-burst")
    @commands.is_owner()
    async def prefix_set_burst(self, ctx: commands.Context, limit: str = None, window: str = None, timeout: str = None):
        """Bot owner only: configure burst protection.
        Usage: `!set-burst [limit] [window_seconds] [timeout_seconds]`
        Running without args shows current values.
        """
        if limit is None and window is None and timeout is None:
            cur_limit = get_setting("burst_limit_count", 20)
            cur_window = get_setting("burst_window_seconds", 60.0)
            cur_timeout = get_setting("burst_timeout_seconds", 300.0)
            await ctx.reply(
                f"Current burst settings:\n- Limit: {cur_limit} commands\n- Window: {cur_window} seconds\n- Timeout: {cur_timeout} seconds"
            )
            return

        try:
            if limit is not None:
                l = int(limit)
                if l < 1:
                    raise ValueError
                set_setting("burst_limit_count", l)
            if window is not None:
                w = _parse_time_arg(window)
                if w <= 0:
                    raise ValueError
                set_setting("burst_window_seconds", w)
            if timeout is not None:
                t = _parse_time_arg(timeout)
                if t < 0:
                    raise ValueError
                set_setting("burst_timeout_seconds", t)
        except ValueError:
            await ctx.reply("❌ Invalid arguments. Usage: `!set-burst [limit] [window_seconds] [timeout_seconds]`")
            return

        await ctx.reply("✅ Burst settings updated.")

    @commands.command(name="settings", aliases=["config"])
    async def prefix_settings(self, ctx: commands.Context, option: str = None, value: str = None):
        """View or manage channel settings. Only admins can modify settings."""
        if ctx.guild is None:
            await ctx.reply("⚠️ Channel settings can only be configured in a server.")
            return

        auto_key = f"auto_respond_channel_{ctx.channel.id}"
        restrict_key = f"restrict_channel_{ctx.channel.id}"
        current_auto = bool(get_setting(auto_key, False))
        current_restrict = bool(get_setting(restrict_key, False))

        # Allow everyone to view settings
        if option is None:
            embed = discord.Embed(
                title="Channel Settings",
                description=(
                    f"**auto_respond** — `{'on' if current_auto else 'off'}`\n"
                    "Respond to every message in this channel without needing a mention.\n"
                    f"**restrict_mode** — `{'on' if current_restrict else 'off'}`\n"
                    "Prevent Jarvis from replying in this channel entirely.\n\n"
                    "_(Only moderators and admins can change these settings)_"
                ),
                color=discord.Color.blurple(),
            )
            await ctx.reply(embed=embed)
            return

        # Require manage_channels permission to modify settings
        if not ctx.author.guild_permissions.manage_channels:
            await ctx.reply("🚫 You need Manage Channels permission to modify channel settings.")
            return

        normalized = option.lower()
        if normalized not in {
            "auto_respond", "autorespond", "respond_all",
            "restrict_mode", "restrict", "no_respond", "mute_channel",
        }:
            await ctx.reply("⚠️ Unknown setting. Available: `auto_respond`, `restrict_mode`.")
            return

        if value is None:
            current_value = (
                current_auto if normalized in {"auto_respond", "autorespond", "respond_all"}
                else current_restrict
            )
            await ctx.reply(
                f"`{normalized}` is currently `{'on' if current_value else 'off'}`."
            )
            return

        normalized_value = value.lower()
        if normalized_value in {"on", "true", "yes", "1"}:
            new_value = True
        elif normalized_value in {"off", "false", "no", "0"}:
            new_value = False
        else:
            await ctx.reply("❌ Invalid value. Use `on` or `off`.")
            return

        setting_key = auto_key if normalized in {"auto_respond", "autorespond", "respond_all"} else restrict_key
        set_setting(setting_key, new_value)

        if setting_key == auto_key:
            await ctx.reply(f"✅ `auto_respond` has been turned {'on' if new_value else 'off'} for this channel.")
        else:
            await ctx.reply(f"✅ `restrict_mode` has been turned {'on' if new_value else 'off'} for this channel.")
    @app_commands.command(name="adminhelp", description="Browse admin and moderator commands")
    @commands.is_owner()
    async def slash_adminhelp(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Admin help is only available in servers.", ephemeral=True
            )
            return
        if not (is_admin(interaction.user) or interaction.user.guild_permissions.manage_channels):
            await interaction.response.send_message(
                "🚫 You need Manage Channels permission to view admin help.", ephemeral=True
            )
            return
        view = AdminHelpView(author_id=interaction.user.id)
        await interaction.response.send_message(embed=_build_admin_overview_embed(), view=view, ephemeral=True)

    @commands.command(name="adminhelp")
    @commands.is_owner()
    async def prefix_adminhelp(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.reply("Admin help is only available in servers.")
            return
        if not (is_admin(ctx.author) or ctx.author.guild_permissions.manage_channels):
            await ctx.reply("🚫 You need Manage Channels permission to view admin help.")
            return
        view = AdminHelpView(author_id=ctx.author.id)
        await ctx.reply(embed=_build_admin_overview_embed(), view=view)
    # ── Slash commands ────────────────────────────────────────────────────────




    # ── Guild-ban commands ────────────────────────────────────────────────────

    @commands.command(name="guild-ban")
    async def prefix_guild_ban(self, ctx: commands.Context, guild_id: str = None, *, reason: str = "No reason provided"):
        """Ban a guild from using Jarvis (admin only)."""
        if not is_admin(ctx.author):
            await ctx.reply("🚫 You don't have permission to use this command.")
            return
        if guild_id is None:
            await ctx.reply("**Usage:** `!guild-ban <guild_id> [reason]`")
            return
        try:
            gid = int(guild_id)
        except ValueError:
            await ctx.reply("❌ Invalid guild ID.")
            return
        await self._do_guild_ban(gid, reason, ctx.reply)

    @commands.command(name="guild-unban")
    async def prefix_guild_unban(self, ctx: commands.Context, guild_id: str = None):
        """Unban a guild from Jarvis (admin only)."""
        if not is_admin(ctx.author):
            await ctx.reply("🚫 You don't have permission to use this command.")
            return
        if guild_id is None:
            await ctx.reply("**Usage:** `!guild-unban <guild_id>`")
            return
        try:
            gid = int(guild_id)
        except ValueError:
            await ctx.reply("❌ Invalid guild ID.")
            return
        await self._do_guild_unban(gid, ctx.reply)

    @commands.command(name="guild-bans")
    async def prefix_guild_bans(self, ctx: commands.Context):
        """List all guild-banned servers (admin only)."""
        if not is_admin(ctx.author):
            await ctx.reply("🚫 You don't have permission to use this command.")
            return
        embed = await _build_guild_ban_embed(self.bot)
        await ctx.reply(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))