import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import time
from cogs.state import (
    get_warnings, add_warning, clear_warnings, remove_warning,
    save_warnings,
)
from cogs.admin import is_admin

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_REASON_LEN  = 512
MAX_PURGE       = 100
WARN_THRESHOLD  = 3   # auto-mute after this many active warnings

# ── Duration parser ───────────────────────────────────────────────────────────

def _parse_duration(s: str) -> int | None:
    """
    Parse a duration string like '10m', '2h', '1d', '1w' into seconds.
    Returns None if unparseable.
    """
    units = {
        "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
        "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
        "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
        "d": 86400, "day": 86400, "days": 86400,
        "w": 604800, "week": 604800, "weeks": 604800,
    }
    s = s.strip().lower()
    for suffix in sorted(units, key=len, reverse=True):
        if s.endswith(suffix) and s[: -len(suffix)].isdigit():
            return int(s[: -len(suffix)]) * units[suffix]
    if s.isdigit():
        return int(s)  # bare number → seconds
    return None


def _fmt_duration(seconds: int) -> str:
    """Format a seconds value into a human-readable string."""
    w, rem  = divmod(seconds, 604800)
    d, rem  = divmod(rem, 86400)
    h, rem  = divmod(rem, 3600)
    m, s    = divmod(rem, 60)
    parts = []
    if w: parts.append(f"{w}w")
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s: parts.append(f"{s}s")
    return " ".join(parts) or "0s"


# ── Permission helpers ────────────────────────────────────────────────────────

def _can_moderate(ctx_or_inter, target: discord.Member) -> str | None:
    """
    Return an error string if the action shouldn't proceed, else None.
    Checks bot hierarchy and target protections.
    """
    guild = target.guild
    if target == guild.owner:
        return "❌ Cannot moderate the server owner."
    if target.top_role >= guild.me.top_role:
        return "❌ My role is not high enough to moderate that member."
    return None


# ── Mod embed builder ─────────────────────────────────────────────────────────

def _mod_embed(
    action: str,
    target: discord.User | discord.Member,
    moderator: discord.User | discord.Member,
    reason: str,
    *,
    color: discord.Color = discord.Color.red(),
    extra: dict | None = None,
) -> discord.Embed:
    embed = discord.Embed(title=f"🔨 {action}", color=color, timestamp=discord.utils.utcnow())
    embed.add_field(name="User",      value=f"{str(target)} (`{target.id}`)", inline=True)
    embed.add_field(name="Moderator", value=str(moderator),                   inline=True)
    embed.add_field(name="Reason",    value=reason,                              inline=False)
    if extra:
        for k, v in extra.items():
            embed.add_field(name=k, value=v, inline=True)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(text="Jarvis Moderation")
    return embed


# ── Cog ───────────────────────────────────────────────────────────────────────

class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Checks ────────────────────────────────────────────────────────────────

    def _require_mod(self, user: discord.Member) -> bool:
        """True if user is a Jarvis admin OR has server Kick/Ban/Manage Messages perms."""
        if is_admin(user):
            return True
        p = user.guild_permissions
        return p.kick_members or p.ban_members or p.manage_messages

    # ═════════════════════════════════════════════════════════════════════════
    # WARN
    # ═════════════════════════════════════════════════════════════════════════

    @commands.command(name="warn")
    @commands.guild_only()
    async def prefix_warn(self, ctx: commands.Context, member: discord.Member = None, *, reason: str = "No reason provided"):
        if not self._require_mod(ctx.author):
            return await ctx.reply("🚫 You need Kick Members permission to warn users.")
        if not member:
            return await ctx.reply("**Usage:** `!warn @user [reason]`")
        if member == ctx.author:
            return await ctx.reply("❌ You cannot warn yourself.")
        if err := _can_moderate(ctx, member):
            return await ctx.reply(err)

        count = add_warning(ctx.guild.id, member.id, ctx.author.id, reason)
        embed = _mod_embed(
            f"Warning #{count}",
            member, ctx.author, reason,
            color=discord.Color.orange(),
            extra={"Total Warnings": str(count)},
        )
        await ctx.reply(embed=embed)

        try:
            await member.send(
                f"⚠️ You received a warning in **{ctx.guild.name}**.\n"
                f"**Reason:** {reason}\n"
                f"This is warning **#{count}**."
            )
        except discord.Forbidden:
            pass

        if count >= WARN_THRESHOLD:
            try:
                duration = 600  # 10-minute auto-mute
                until = discord.utils.utcnow() + discord.timedelta(seconds=duration)
                await member.timeout(until, reason=f"Auto-mute: reached {count} warnings")
                await ctx.send(
                    f"⚠️ **{member.display_name}** has been **auto-muted for 10 minutes** after reaching {count} warnings."
                )
            except discord.Forbidden:
                pass

    @app_commands.command(name="warn", description="Warn a member")
    @app_commands.describe(member="Member to warn", reason="Reason for the warning")
    @app_commands.guild_only()
    async def slash_warn(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not self._require_mod(interaction.user):
            return await interaction.response.send_message("🚫 You need Kick Members permission to warn users.", ephemeral=True)
        if member == interaction.user:
            return await interaction.response.send_message("❌ You cannot warn yourself.", ephemeral=True)
        if err := _can_moderate(interaction, member):
            return await interaction.response.send_message(err, ephemeral=True)

        count = add_warning(interaction.guild_id, member.id, interaction.user.id, reason)
        embed = _mod_embed(
            f"Warning #{count}",
            member, interaction.user, reason,
            color=discord.Color.orange(),
            extra={"Total Warnings": str(count)},
        )
        await interaction.response.send_message(embed=embed)

        try:
            await member.send(
                f"⚠️ You received a warning in **{interaction.guild.name}**.\n"
                f"**Reason:** {reason}\n"
                f"This is warning **#{count}**."
            )
        except discord.Forbidden:
            pass

        if count >= WARN_THRESHOLD:
            try:
                until = discord.utils.utcnow() + discord.timedelta(seconds=600)
                await member.timeout(until, reason=f"Auto-mute: reached {count} warnings")
                await interaction.followup.send(
                    f"⚠️ **{member.display_name}** has been **auto-muted for 10 minutes** after reaching {count} warnings."
                )
            except discord.Forbidden:
                pass

    # ── Warnings list ─────────────────────────────────────────────────────────

    @commands.command(name="warnings")
    @commands.guild_only()
    async def prefix_warnings(self, ctx: commands.Context, member: discord.Member = None):
        target = member or ctx.author
        warnings = get_warnings(ctx.guild.id, target.id)
        if not warnings:
            return await ctx.reply(f"✅ **{target.display_name}** has no warnings in this server.")

        embed = discord.Embed(
            title=f"⚠️ Warnings — {target.display_name}",
            color=discord.Color.orange(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        for i, w in enumerate(warnings, 1):
            ts = discord.utils.format_dt(
                discord.utils.utcnow().__class__.fromtimestamp(w["time"], tz=discord.utils.utcnow().tzinfo),
                style="R",
            )
            mod_member = ctx.guild.get_member(w['mod_id']) if ctx and ctx.guild else None
            mod_label = str(mod_member) if mod_member else f"ID: {w['mod_id']}"
            embed.add_field(
                name=f"#{i} — {ts}",
                value=f"**Reason:** {w['reason']}\n**By:** {mod_label}  •  ID: `{w['id']}`",
                inline=False,
            )
        embed.set_footer(text=f"{len(warnings)} warning(s) total")
        await ctx.reply(embed=embed)

    @app_commands.command(name="warnings", description="View a member's warnings")
    @app_commands.describe(member="Member to check (leave blank for yourself)")
    @app_commands.guild_only()
    async def slash_warnings(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        warnings = get_warnings(interaction.guild_id, target.id)
        if not warnings:
            return await interaction.response.send_message(
                f"✅ **{target.display_name}** has no warnings in this server.", ephemeral=True
            )

        embed = discord.Embed(
            title=f"⚠️ Warnings — {target.display_name}",
            color=discord.Color.orange(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        for i, w in enumerate(warnings, 1):
            ts = discord.utils.format_dt(
                discord.utils.utcnow().__class__.fromtimestamp(w["time"], tz=discord.utils.utcnow().tzinfo),
                style="R",
            )
            mod_member = interaction.guild.get_member(w['mod_id']) if interaction and interaction.guild else None
            mod_label = str(mod_member) if mod_member else f"ID: {w['mod_id']}"
            embed.add_field(
                name=f"#{i} — {ts}",
                value=f"**Reason:** {w['reason']}\n**By:** {mod_label}  •  ID: `{w['id']}`",
                inline=False,
            )
        embed.set_footer(text=f"{len(warnings)} warning(s) total")
        await interaction.response.send_message(embed=embed)

    # ── Remove warning ────────────────────────────────────────────────────────

    @commands.command(name="delwarn")
    @commands.guild_only()
    async def prefix_delwarn(self, ctx: commands.Context, member: discord.Member = None, warn_id: str = None):
        if not self._require_mod(ctx.author):
            return await ctx.reply("🚫 You need Kick Members permission to remove warnings.")
        if not member or not warn_id:
            return await ctx.reply("**Usage:** `!delwarn @user <warn_id>`  (get IDs from `!warnings @user`)")
        removed = remove_warning(ctx.guild.id, member.id, warn_id)
        if removed:
            await ctx.reply(f"✅ Warning `{warn_id}` removed from **{member.display_name}**.")
        else:
            await ctx.reply(f"❌ No warning with ID `{warn_id}` found for **{member.display_name}**.")

    @app_commands.command(name="delwarn", description="Remove a specific warning from a member")
    @app_commands.describe(member="The member", warn_id="Warning ID (from /warnings)")
    @app_commands.guild_only()
    async def slash_delwarn(self, interaction: discord.Interaction, member: discord.Member, warn_id: str):
        if not self._require_mod(interaction.user):
            return await interaction.response.send_message("🚫 You need Kick Members permission.", ephemeral=True)
        removed = remove_warning(interaction.guild_id, member.id, warn_id)
        if removed:
            await interaction.response.send_message(f"✅ Warning `{warn_id}` removed from **{member.display_name}**.")
        else:
            await interaction.response.send_message(f"❌ No warning with ID `{warn_id}` found.", ephemeral=True)

    # ── Clear warnings ────────────────────────────────────────────────────────

    @commands.command(name="clearwarns")
    @commands.guild_only()
    async def prefix_clearwarns(self, ctx: commands.Context, member: discord.Member = None):
        if not self._require_mod(ctx.author):
            return await ctx.reply("🚫 You need Kick Members permission to clear warnings.")
        if not member:
            return await ctx.reply("**Usage:** `!clearwarns @user`")
        count = len(get_warnings(ctx.guild.id, member.id))
        clear_warnings(ctx.guild.id, member.id)
        await ctx.reply(f"✅ Cleared **{count}** warning(s) from **{member.display_name}**.")

    @app_commands.command(name="clearwarns", description="Clear all warnings for a member")
    @app_commands.describe(member="Member to clear warnings for")
    @app_commands.guild_only()
    async def slash_clearwarns(self, interaction: discord.Interaction, member: discord.Member):
        if not self._require_mod(interaction.user):
            return await interaction.response.send_message("🚫 You need Kick Members permission.", ephemeral=True)
        count = len(get_warnings(interaction.guild_id, member.id))
        clear_warnings(interaction.guild_id, member.id)
        await interaction.response.send_message(f"✅ Cleared **{count}** warning(s) from **{member.display_name}**.")

    # ═════════════════════════════════════════════════════════════════════════
    # MUTE  (Discord Timeout)
    # ═════════════════════════════════════════════════════════════════════════

    @commands.command(name="mute")
    @commands.guild_only()
    async def prefix_mute(self, ctx: commands.Context, member: discord.Member = None, duration: str = None, *, reason: str = "No reason provided"):
        if not self._require_mod(ctx.author):
            return await ctx.reply("🚫 You need Moderate Members permission to mute.")
        if not member:
            return await ctx.reply("**Usage:** `!mute @user <duration> [reason]`\nExamples: `!mute @user 10m Spamming` | `!mute @user 2h`")
        if not duration:
            return await ctx.reply("❌ Please provide a duration. E.g. `10m`, `2h`, `1d`.")
        if err := _can_moderate(ctx, member):
            return await ctx.reply(err)

        seconds = _parse_duration(duration)
        if not seconds or seconds < 1:
            return await ctx.reply("❌ Invalid duration. Use formats like `10m`, `2h`, `1d`, `1w`.")
        if seconds > 2419200:  # Discord max: 28 days
            return await ctx.reply("❌ Maximum mute duration is 28 days.")

        until = discord.utils.utcnow() + discord.timedelta(seconds=seconds)
        try:
            await member.timeout(until, reason=f"{ctx.author}: {reason}")
        except discord.Forbidden:
            return await ctx.reply("❌ I don't have permission to timeout this member.")

        embed = _mod_embed(
            "Member Muted", member, ctx.author, reason,
            color=discord.Color.yellow(),
            extra={"Duration": _fmt_duration(seconds), "Expires": discord.utils.format_dt(until, style="R")},
        )
        await ctx.reply(embed=embed)
        try:
            await member.send(f"🔇 You have been muted in **{ctx.guild.name}** for **{_fmt_duration(seconds)}**.\n**Reason:** {reason}")
        except discord.Forbidden:
            pass

    @app_commands.command(name="mute", description="Timeout (mute) a member")
    @app_commands.describe(member="Member to mute", duration="Duration e.g. 10m, 2h, 1d", reason="Reason")
    @app_commands.guild_only()
    async def slash_mute(self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason provided"):
        if not self._require_mod(interaction.user):
            return await interaction.response.send_message("🚫 You need Moderate Members permission.", ephemeral=True)
        if err := _can_moderate(interaction, member):
            return await interaction.response.send_message(err, ephemeral=True)

        seconds = _parse_duration(duration)
        if not seconds or seconds < 1:
            return await interaction.response.send_message("❌ Invalid duration. Use formats like `10m`, `2h`, `1d`.", ephemeral=True)
        if seconds > 2419200:
            return await interaction.response.send_message("❌ Maximum mute duration is 28 days.", ephemeral=True)

        until = discord.utils.utcnow() + discord.timedelta(seconds=seconds)
        try:
            await member.timeout(until, reason=f"{interaction.user}: {reason}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I don't have permission to timeout this member.", ephemeral=True)

        embed = _mod_embed(
            "Member Muted", member, interaction.user, reason,
            color=discord.Color.yellow(),
            extra={"Duration": _fmt_duration(seconds), "Expires": discord.utils.format_dt(until, style="R")},
        )
        await interaction.response.send_message(embed=embed)
        try:
            await member.send(f"🔇 You have been muted in **{interaction.guild.name}** for **{_fmt_duration(seconds)}**.\n**Reason:** {reason}")
        except discord.Forbidden:
            pass

    # ── Unmute ────────────────────────────────────────────────────────────────

    @commands.command(name="unmute")
    @commands.guild_only()
    async def prefix_unmute(self, ctx: commands.Context, member: discord.Member = None, *, reason: str = "No reason provided"):
        if not self._require_mod(ctx.author):
            return await ctx.reply("🚫 You need Moderate Members permission to unmute.")
        if not member:
            return await ctx.reply("**Usage:** `!unmute @user [reason]`")
        if not member.is_timed_out():
            return await ctx.reply(f"ℹ️ **{member.display_name}** is not currently muted.")
        try:
            await member.timeout(None, reason=f"{ctx.author}: {reason}")
        except discord.Forbidden:
            return await ctx.reply("❌ I don't have permission to unmute this member.")
        embed = _mod_embed("Member Unmuted", member, ctx.author, reason, color=discord.Color.green())
        await ctx.reply(embed=embed)
        try:
            await member.send(f"🔊 Your mute in **{ctx.guild.name}** has been lifted.\n**Reason:** {reason}")
        except discord.Forbidden:
            pass

    @app_commands.command(name="unmute", description="Remove a timeout from a member")
    @app_commands.describe(member="Member to unmute", reason="Reason")
    @app_commands.guild_only()
    async def slash_unmute(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not self._require_mod(interaction.user):
            return await interaction.response.send_message("🚫 You need Moderate Members permission.", ephemeral=True)
        if not member.is_timed_out():
            return await interaction.response.send_message(f"ℹ️ **{member.display_name}** is not currently muted.", ephemeral=True)
        try:
            await member.timeout(None, reason=f"{interaction.user}: {reason}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I don't have permission to unmute this member.", ephemeral=True)
        embed = _mod_embed("Member Unmuted", member, interaction.user, reason, color=discord.Color.green())
        await interaction.response.send_message(embed=embed)
        try:
            await member.send(f"🔊 Your mute in **{interaction.guild.name}** has been lifted.\n**Reason:** {reason}")
        except discord.Forbidden:
            pass

    # ═════════════════════════════════════════════════════════════════════════
    # KICK
    # ═════════════════════════════════════════════════════════════════════════

    @commands.command(name="kick")
    @commands.guild_only()
    async def prefix_kick(self, ctx: commands.Context, member: discord.Member = None, *, reason: str = "No reason provided"):
        if not (self._require_mod(ctx.author) and ctx.author.guild_permissions.kick_members) and not is_admin(ctx.author):
            return await ctx.reply("🚫 You need the **Kick Members** permission.")
        if not member:
            return await ctx.reply("**Usage:** `!kick @user [reason]`")
        if member == ctx.author:
            return await ctx.reply("❌ You cannot kick yourself.")
        if err := _can_moderate(ctx, member):
            return await ctx.reply(err)

        try:
            await member.send(f"👢 You were kicked from **{ctx.guild.name}**.\n**Reason:** {reason}")
        except discord.Forbidden:
            pass

        await member.kick(reason=f"{ctx.author}: {reason}")
        embed = _mod_embed("Member Kicked", member, ctx.author, reason, color=discord.Color.orange())
        await ctx.reply(embed=embed)

    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(member="Member to kick", reason="Reason for the kick")
    @app_commands.guild_only()
    async def slash_kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not (interaction.user.guild_permissions.kick_members or is_admin(interaction.user)):
            return await interaction.response.send_message("🚫 You need the **Kick Members** permission.", ephemeral=True)
        if member == interaction.user:
            return await interaction.response.send_message("❌ You cannot kick yourself.", ephemeral=True)
        if err := _can_moderate(interaction, member):
            return await interaction.response.send_message(err, ephemeral=True)

        await interaction.response.defer()
        try:
            await member.send(f"👢 You were kicked from **{interaction.guild.name}**.\n**Reason:** {reason}")
        except discord.Forbidden:
            pass
        await member.kick(reason=f"{interaction.user}: {reason}")
        embed = _mod_embed("Member Kicked", member, interaction.user, reason, color=discord.Color.orange())
        await interaction.followup.send(embed=embed)

    # ═════════════════════════════════════════════════════════════════════════
    # BAN / UNBAN / SOFTBAN
    # ═════════════════════════════════════════════════════════════════════════

    @commands.command(name="ban")
    @commands.guild_only()
    async def prefix_ban(self, ctx: commands.Context, member: discord.Member = None, *, reason: str = "No reason provided"):
        if not (ctx.author.guild_permissions.ban_members or is_admin(ctx.author)):
            return await ctx.reply("🚫 You need the **Ban Members** permission.")
        if not member:
            return await ctx.reply("**Usage:** `!ban @user [reason]`")
        if member == ctx.author:
            return await ctx.reply("❌ You cannot ban yourself.")
        if err := _can_moderate(ctx, member):
            return await ctx.reply(err)

        try:
            await member.send(f"🔨 You were banned from **{ctx.guild.name}**.\n**Reason:** {reason}")
        except discord.Forbidden:
            pass
        await member.ban(reason=f"{ctx.author}: {reason}", delete_message_days=0)
        embed = _mod_embed("Member Banned", member, ctx.author, reason)
        await ctx.reply(embed=embed)

    @app_commands.command(name="ban", description="Ban a member from the server")
    @app_commands.describe(member="Member to ban", reason="Reason", delete_days="Days of messages to delete (0–7)")
    @app_commands.guild_only()
    async def slash_ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_days: int = 0):
        if not (interaction.user.guild_permissions.ban_members or is_admin(interaction.user)):
            return await interaction.response.send_message("🚫 You need the **Ban Members** permission.", ephemeral=True)
        if member == interaction.user:
            return await interaction.response.send_message("❌ You cannot ban yourself.", ephemeral=True)
        if err := _can_moderate(interaction, member):
            return await interaction.response.send_message(err, ephemeral=True)

        delete_days = max(0, min(7, delete_days))
        await interaction.response.defer()
        try:
            await member.send(f"🔨 You were banned from **{interaction.guild.name}**.\n**Reason:** {reason}")
        except discord.Forbidden:
            pass
        await member.ban(reason=f"{interaction.user}: {reason}", delete_message_days=delete_days)
        embed = _mod_embed("Member Banned", member, interaction.user, reason)
        await interaction.followup.send(embed=embed)

    @commands.command(name="unban")
    @commands.guild_only()
    async def prefix_unban(self, ctx: commands.Context, user_id: str = None, *, reason: str = "No reason provided"):
        if not (ctx.author.guild_permissions.ban_members or is_admin(ctx.author)):
            return await ctx.reply("🚫 You need the **Ban Members** permission.")
        if not user_id:
            return await ctx.reply("**Usage:** `!unban <user_id> [reason]`")
        try:
            uid = int(user_id.strip("<@!>"))
            user = await self.bot.fetch_user(uid)
            await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
            embed = _mod_embed("Member Unbanned", user, ctx.author, reason, color=discord.Color.green())
            await ctx.reply(embed=embed)
        except (ValueError, discord.NotFound):
            await ctx.reply("❌ User not found or is not banned.")
        except discord.Forbidden:
            await ctx.reply("❌ I don't have permission to unban members.")

    @app_commands.command(name="unban", description="Unban a user by their ID")
    @app_commands.describe(user_id="Discord user ID to unban", reason="Reason")
    @app_commands.guild_only()
    async def slash_unban(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
        if not (interaction.user.guild_permissions.ban_members or is_admin(interaction.user)):
            return await interaction.response.send_message("🚫 You need the **Ban Members** permission.", ephemeral=True)
        await interaction.response.defer()
        try:
            uid = int(user_id.strip("<@!>"))
            user = await self.bot.fetch_user(uid)
            await interaction.guild.unban(user, reason=f"{interaction.user}: {reason}")
            embed = _mod_embed("Member Unbanned", user, interaction.user, reason, color=discord.Color.green())
            await interaction.followup.send(embed=embed)
        except (ValueError, discord.NotFound):
            await interaction.followup.send("❌ User not found or is not banned.")
        except discord.Forbidden:
            await interaction.followup.send("❌ I don't have permission to unban members.")

    @commands.command(name="softban")
    @commands.guild_only()
    async def prefix_softban(self, ctx: commands.Context, member: discord.Member = None, *, reason: str = "No reason provided"):
        """Ban then immediately unban to delete recent messages without keeping the ban."""
        if not (ctx.author.guild_permissions.ban_members or is_admin(ctx.author)):
            return await ctx.reply("🚫 You need the **Ban Members** permission.")
        if not member:
            return await ctx.reply("**Usage:** `!softban @user [reason]`")
        if err := _can_moderate(ctx, member):
            return await ctx.reply(err)

        try:
            await member.send(f"🔨 You were soft-banned from **{ctx.guild.name}** (your recent messages were cleared).\n**Reason:** {reason}")
        except discord.Forbidden:
            pass
        await member.ban(reason=f"Softban by {ctx.author}: {reason}", delete_message_days=7)
        await ctx.guild.unban(member, reason="Softban — immediate unban")
        embed = _mod_embed("Member Soft-Banned", member, ctx.author, reason, color=discord.Color.dark_orange())
        embed.set_footer(text="Soft-ban: banned then immediately unbanned to purge messages")
        await ctx.reply(embed=embed)

    @app_commands.command(name="softban", description="Ban + unban to delete messages without a permanent ban")
    @app_commands.describe(member="Member to soft-ban", reason="Reason")
    @app_commands.guild_only()
    async def slash_softban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not (interaction.user.guild_permissions.ban_members or is_admin(interaction.user)):
            return await interaction.response.send_message("🚫 You need the **Ban Members** permission.", ephemeral=True)
        if err := _can_moderate(interaction, member):
            return await interaction.response.send_message(err, ephemeral=True)
        await interaction.response.defer()
        try:
            await member.send(f"🔨 You were soft-banned from **{interaction.guild.name}**.\n**Reason:** {reason}")
        except discord.Forbidden:
            pass
        await member.ban(reason=f"Softban by {interaction.user}: {reason}", delete_message_days=7)
        await interaction.guild.unban(member, reason="Softban — immediate unban")
        embed = _mod_embed("Member Soft-Banned", member, interaction.user, reason, color=discord.Color.dark_orange())
        await interaction.followup.send(embed=embed)

    # ═════════════════════════════════════════════════════════════════════════
    # PURGE
    # ═════════════════════════════════════════════════════════════════════════

    @commands.command(name="purge")
    @commands.guild_only()
    async def prefix_purge(self, ctx: commands.Context, amount: int = None, member: discord.Member = None):
        """
        !purge <amount>           — delete last N messages
        !purge <amount> @user     — delete last N messages from that user only
        """
        if not (ctx.author.guild_permissions.manage_messages or is_admin(ctx.author)):
            return await ctx.reply("🚫 You need the **Manage Messages** permission.")
        if not amount or amount < 1:
            return await ctx.reply(
                "**Usage:**\n"
                "`!purge <amount>` — delete last N messages (max 100)\n"
                "`!purge <amount> @user` — delete last N messages from a specific user"
            )
        amount = min(amount, MAX_PURGE)

        await ctx.message.delete()

        if member:
            check = lambda m: m.author == member
        else:
            check = None

        deleted = await ctx.channel.purge(limit=amount, check=check)
        msg = await ctx.send(
            f"🗑️ Deleted **{len(deleted)}** message(s)"
            + (f" from **{member.display_name}**" if member else "")
            + "."
        )
        await asyncio.sleep(5)
        await msg.delete()

    @app_commands.command(name="purge", description="Bulk delete messages from this channel")
    @app_commands.describe(
        amount="Number of messages to delete (max 100)",
        member="Only delete messages from this member (optional)",
    )
    @app_commands.guild_only()
    async def slash_purge(self, interaction: discord.Interaction, amount: int, member: discord.Member = None):
        if not (interaction.user.guild_permissions.manage_messages or is_admin(interaction.user)):
            return await interaction.response.send_message("🚫 You need the **Manage Messages** permission.", ephemeral=True)
        if amount < 1:
            return await interaction.response.send_message("❌ Amount must be at least 1.", ephemeral=True)

        amount = min(amount, MAX_PURGE)
        await interaction.response.defer(ephemeral=True)

        check = (lambda m: m.author == member) if member else None
        deleted = await interaction.channel.purge(limit=amount, check=check)

        await interaction.followup.send(
            f"🗑️ Deleted **{len(deleted)}** message(s)"
            + (f" from **{member.display_name}**" if member else "")
            + ".",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))