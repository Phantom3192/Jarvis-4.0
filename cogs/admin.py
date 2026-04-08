import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import json
import os
import aiohttp
from cogs.state import bot_bans, save_bans, is_bot_banned, reset_ai_usage

# ── Config ───────────────────────────────────────────────────────────────────

LOG_WEBHOOK_URL = os.getenv("LOG_WEBHOOK_URL", "")  # Webhook URL for new user alerts

# ── Hardcoded admins ──────────────────────────────────────────────────────────

ADMIN_USERNAMES = [
    "phantom_3192",   # ← replace with your Discord username
]

# ── Ban state (shared via cogs.state) ────────────────────────────────────────
# bot_bans, save_bans, is_bot_banned are all imported from cogs.state


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_admin(user: discord.User | discord.Member) -> bool:
    return user.name.lower() in [u.lower() for u in ADMIN_USERNAMES]


def parse_duration(amount: int, unit: str) -> int:
    unit = unit.lower()
    multipliers = {
        "minute": 60,    "minutes": 60,    "min": 60,   "m": 60,
        "hour":   3600,  "hours":   3600,  "hr":  3600, "h": 3600,
        "day":    86400, "days":    86400, "d":   86400,
        "week":   604800,"weeks":   604800,"w":   604800,
    }
    return amount * multipliers.get(unit, -1)

BAN_USAGE   = ("**Usage:** `!global-ban @user [duration unit] [reason]`\n"
               "**Examples:**\n"
               "`!global-ban @user spamming` — permanent ban\n"
               "`!global-ban @user permanent abusing the bot` — permanent ban\n"
               "`!global-ban @user 7 days flooding` — 7-day temp ban\n"
               "`!global-ban @user 2 hours repeated spam` — 2-hour temp ban")
UNBAN_USAGE = "**Usage:** `!global-unban <user_id>`"

# ── Cog ───────────────────────────────────────────────────────────────────────

class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.temp_ban_tasks: dict[int, asyncio.Task] = {}

        # Re-schedule any active temp bans that survived a restart
        import time
        now = time.time()
        for uid_str, data in list(bot_bans.items()):
            expires = data.get("expires")
            if expires is not None:
                remaining = expires - now
                if remaining <= 0:
                    # Already expired — remove
                    del bot_bans[uid_str]
                    save_bans()
                else:
                    uid = int(uid_str)
                    self.temp_ban_tasks[uid] = asyncio.create_task(
                        self._unban_after(uid, remaining)
                    )

    # ─────────────────────────────────────────────────────────────────────────
    # Shared core logic
    # ─────────────────────────────────────────────────────────────────────────

    async def _do_botban(self, user: discord.User | discord.Member, reason, duration, unit, send_msg):
        import time

        # Permanent ban: duration is 0 OR unit is "permanent"
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
            await send_msg(f"🚫 **{user}** has been permanently banned from using Jarvis.\n**Reason:** {reason}")
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
            await send_msg(f"⏱️ **{user}** is banned from Jarvis for **{duration} {unit}**.\n**Reason:** {reason}")
            task = asyncio.create_task(self._unban_after(user.id, seconds))
            self.temp_ban_tasks[user.id] = task

    async def _do_botunban(self, user_id: int, send_msg):
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

    async def _unban_after(self, user_id: int, seconds: float):
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

    # ─────────────────────────────────────────────────────────────────────────
    # Global check — blocks banned users from ALL bot interactions
    # ─────────────────────────────────────────────────────────────────────────

    async def cog_check(self, ctx: commands.Context) -> bool:
        # Admins are never blocked
        if is_admin(ctx.author):
            return True
        if is_bot_banned(ctx.author.id):
            await ctx.reply("🚫 You are banned from using Jarvis.")
            return False
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # Prefix commands
    # ─────────────────────────────────────────────────────────────────────────

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

        duration = 0
        unit = "permanent"
        reason = "No reason provided"

        if args:
            if args[0].lower() == "permanent":
                # !global-ban @user permanent [reason]
                reason = " ".join(args[1:]) or "No reason provided"
            elif args[0].isdigit() and len(args) >= 2:
                # !global-ban @user 7 days [reason]
                duration = int(args[0])
                unit = args[1]
                # Validate unit before proceeding
                if parse_duration(duration, unit) <= 0:
                    await ctx.reply(
                        f"❌ `{unit}` is not a valid time unit.\n"
                        "Valid units: `minutes`, `hours`, `days`, `weeks`\n\n"
                        + BAN_USAGE
                    )
                    return
                reason = " ".join(args[2:]) or "No reason provided"
            else:
                # No duration prefix — permanent ban with reason
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
        import time
        lines = []
        for uid_str, data in bot_bans.items():
            expires = data.get("expires")
            if expires:
                remaining = int(expires - time.time())
                hrs, rem = divmod(remaining, 3600)
                mins = rem // 60
                duration_str = f"⏱️ {hrs}h {mins}m remaining"
            else:
                duration_str = "🔴 Permanent"
            try:
                user = await self.bot.fetch_user(int(uid_str))
                user_str = f"**{user.name}** (`{uid_str}`)"
            except Exception:
                user_str = f"Unknown User (`{uid_str}`)"
            lines.append(f"{user_str}\n  ↳ {duration_str} | Reason: {data.get('reason', 'None')}\n  ↳ Unban: `!global-unban {uid_str}`")
        chunk = "\n\n".join(lines[:20])
        note  = f"\n*...and {len(lines) - 20} more.*" if len(lines) > 20 else ""
        await ctx.reply(f"🚫 **Bot-banned users ({len(bot_bans)}):**\n\n{chunk}{note}")

    # ─────────────────────────────────────────────────────────────────────────
    # Slash commands
    # ─────────────────────────────────────────────────────────────────────────

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
            await interaction.response.send_message(f"⚠️ **{user}** is already banned from Jarvis.", ephemeral=True)
            return
        await interaction.response.defer()
        await self._do_botban(user, reason, duration, unit,
                              lambda msg: interaction.followup.send(msg))

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
        import time
        lines = []
        for uid_str, data in bot_bans.items():
            expires = data.get("expires")
            if expires:
                remaining = int(expires - time.time())
                hrs, rem = divmod(remaining, 3600)
                mins = rem // 60
                duration_str = f"⏱️ {hrs}h {mins}m remaining"
            else:
                duration_str = "🔴 Permanent"
            try:
                user = await self.bot.fetch_user(int(uid_str))
                user_str = f"**{user.name}** (`{uid_str}`)"
            except Exception:
                user_str = f"Unknown User (`{uid_str}`)"
            lines.append(f"{user_str}\n  ↳ {duration_str} | Reason: {data.get('reason', 'None')}\n  ↳ Unban: `!global-unban {uid_str}`")
        chunk = "\n\n".join(lines[:20])
        note  = f"\n*...and {len(lines) - 20} more.*" if len(lines) > 20 else ""
        await interaction.followup.send(f"🚫 **Bot-banned users ({len(bot_bans)}):**\n\n{chunk}{note}")



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