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
from cogs.state import bot_bans, save_bans, is_bot_banned, reset_ai_usage, get_setting, set_setting
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

# ── Helpers ───────────────────────────────────────────────────────────────────

def is_admin(user: discord.User | discord.Member) -> bool:
    """O(1) admin check using a pre-built frozenset."""
    return user.name.lower() in ADMIN_USERNAMES

BAN_USAGE = (
    "**Usage:** `!global-ban @user [duration unit] [reason]`\n"
    "**Examples:**\n"
    "`!global-ban @user spamming` — permanent ban\n"
    "`!global-ban @user permanent abusing the bot` — permanent ban\n"
    "`!global-ban @user 7 days flooding` — 7-day temp ban\n"
    "`!global-ban @user 2 hours repeated spam` — 2-hour temp ban"
)
UNBAN_USAGE = "**Usage:** `!global-unban <user_id>`"


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


# ── Cog ───────────────────────────────────────────────────────────────────────

class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.temp_ban_tasks: dict[int, asyncio.Task] = {}

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
                await user.send(
                    f"🚫 You have been **permanently banned** from using **Jarvis**.\n"
                    f"**Reason:** {reason}"
                )
            except discord.Forbidden:
                pass
            await send_msg(
                f"🚫 **{user}** has been permanently banned from using Jarvis.\n"
                f"**Reason:** {reason}"
            )
        else:
            seconds = parse_duration(duration, unit)
            if seconds <= 0:
                await send_msg("❌ Invalid duration or unit.")
                return
            expires = time.time() + seconds
            bot_bans[str(user.id)] = {"reason": reason, "expires": expires}
            save_bans()
            try:
                await user.send(
                    f"⏱️ You have been **temporarily banned** from using **Jarvis**.\n"
                    f"**Duration:** {duration} {unit}\n"
                    f"**Reason:** {reason}"
                )
            except discord.Forbidden:
                pass
            await send_msg(
                f"⏱️ **{user}** is banned from Jarvis for **{duration} {unit}**.\n"
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
            await user.send("✅ Your temporary ban from **Jarvis** has expired. You can use me again!")
        except (discord.Forbidden, discord.NotFound):
            pass

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
            if args[0].lower() == "permanent":
                reason = " ".join(args[1:]) or "No reason provided"
            elif args[0].isdigit() and len(args) >= 2:
                duration = int(args[0])
                unit     = args[1]
                if parse_duration(duration, unit) <= 0:
                    await ctx.reply(
                        f"❌ `{unit}` is not a valid time unit.\n"
                        "Valid units: `minutes`, `hours`, `days`, `weeks`\n\n" + BAN_USAGE
                    )
                    return
                reason = " ".join(args[2:]) or "No reason provided"
            else:
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

    # ── Slash commands ────────────────────────────────────────────────────────

    @app_commands.command(name="botban", description="Ban a user from using Jarvis")
    @app_commands.describe(
        user="User to ban",
        reason="Reason for the ban",
        duration="Duration (e.g. 7). Leave 0 for permanent.",
        unit="Time unit",
    )
    @app_commands.choices(unit=[
        app_commands.Choice(name="Permanent", value="permanent"),
        app_commands.Choice(name="Minutes",   value="minutes"),
        app_commands.Choice(name="Hours",     value="hours"),
        app_commands.Choice(name="Days",      value="days"),
        app_commands.Choice(name="Weeks",     value="weeks"),
    ])
    async def slash_botban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: str = "No reason provided",
        duration: int = 0,
        unit: str = "permanent",
    ):
        if not is_admin(interaction.user):
            await interaction.response.send_message("🚫 You don't have permission.", ephemeral=True)
            return
        if user.id == interaction.user.id:
            await interaction.response.send_message("❌ You can't ban yourself.", ephemeral=True)
            return
        if is_admin(user):
            await interaction.response.send_message("❌ You can't ban another admin.", ephemeral=True)
            return
        if is_bot_banned(user.id):
            await interaction.response.send_message(
                f"⚠️ **{user}** is already banned from Jarvis.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self._do_botban(
            user, reason, duration, unit,
            lambda msg: interaction.followup.send(msg)
        )

    @app_commands.command(name="botunban", description="Unban a user from Jarvis")
    @app_commands.describe(user_id="The Discord user ID to unban")
    async def slash_botunban(self, interaction: discord.Interaction, user_id: str):
        if not is_admin(interaction.user):
            await interaction.response.send_message("🚫 You don't have permission.", ephemeral=True)
            return
        try:
            uid = int(user_id)
        except ValueError:
            await interaction.response.send_message("❌ Invalid user ID.", ephemeral=True)
            return
        await interaction.response.defer()
        await self._do_botunban(uid, lambda msg: interaction.followup.send(msg))

    @app_commands.command(name="botbans", description="List all users banned from Jarvis")
    async def slash_botbans(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("🚫 You don't have permission.", ephemeral=True)
            return
        await interaction.response.defer()
        if not bot_bans:
            await interaction.followup.send("✅ No users are currently banned from Jarvis.")
            return
        lines = await _format_ban_lines(self.bot)
        await interaction.followup.send(embed=_build_ban_list_embed(lines))

    @app_commands.command(name="resetlimit", description="Reset a user's daily AI message limit (admin only)")
    @app_commands.describe(user="The user whose limit to reset")
    async def slash_resetlimit(self, interaction: discord.Interaction, user: discord.User):
        if not is_admin(interaction.user):
            await interaction.response.send_message("🚫 You don't have permission.", ephemeral=True)
            return
        reset_ai_usage(user.id)
        await interaction.response.send_message(
            f"✅ Daily AI limit reset for **{user}** — they can send AI messages again.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))