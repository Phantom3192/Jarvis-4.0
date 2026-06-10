"""
counting.py — Counting channel game cog for Jarvis
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Features
--------
• Set a dedicated counting channel per guild
• Players must count up from 1 in order — no double-counting allowed
• Wrong number or same user counting twice → count resets to 0, bot reacts ❌ and warns
• Correct count → bot reacts ✅
• Tracks the current count + high score per guild
• /countsetup  — set the counting channel (Manage Channels required)
• /countstats  — show current count and high score
• /countreset  — reset the count manually (admin only)
• !countsetup / !countstats / !countreset prefix equivalents
"""

import json
import os
import discord
from discord.ext import commands
from discord import app_commands

# ── Persistence ───────────────────────────────────────────────────────────────

_DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "counting_data.json")


def _load() -> dict:
    if os.path.exists(_DATA_PATH):
        try:
            with open(_DATA_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save(data: dict):
    with open(_DATA_PATH, "w") as f:
        json.dump(data, f, indent=2)


# guild_id (str) → {
#   "channel_id": int,
#   "count": int,
#   "high_score": int,
#   "last_user_id": int | null
# }
_data: dict = _load()


def _guild(guild_id: int) -> dict:
    key = str(guild_id)
    if key not in _data:
        _data[key] = {"channel_id": None, "count": 0, "high_score": 0, "last_user_id": None}
    return _data[key]


# ── Cog ───────────────────────────────────────────────────────────────────────

class Counting(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Message listener ──────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        g = _guild(message.guild.id)
        if not g["channel_id"] or message.channel.id != g["channel_id"]:
            return

        content = message.content.strip()

        # Only process pure integer messages
        try:
            number = int(content)
        except ValueError:
            return  # ignore non-number messages silently

        expected = g["count"] + 1

        # ── Wrong number ──────────────────────────────────────────────────────
        if number != expected:
            old_count = g["count"]
            g["count"]        = 0
            g["last_user_id"] = None
            _save(_data)
            await message.add_reaction("❌")
            await message.reply(
                f"❌ **Wrong number!** The next number was **{expected}**.\n"
                f"Count resets to **0**. (previous count: {old_count})",
                delete_after=8,
            )
            return

        # ── Same user counting twice ──────────────────────────────────────────
        if g["last_user_id"] == message.author.id:
            old_count = g["count"]
            g["count"]        = 0
            g["last_user_id"] = None
            _save(_data)
            await message.add_reaction("❌")
            await message.reply(
                f"❌ **{message.author.display_name}**, you can't count twice in a row!\n"
                f"Count resets to **0**. (previous count: {old_count})",
                delete_after=8,
            )
            return

        # ── Correct count ─────────────────────────────────────────────────────
        g["count"]        = number
        g["last_user_id"] = message.author.id
        if number > g["high_score"]:
            g["high_score"] = number
        _save(_data)
        await message.add_reaction("✅")

        # Milestone celebrations
        if number % 100 == 0:
            await message.channel.send(
                f"🎉 **{number}!** Incredible counting! High score: **{g['high_score']}**"
            )
        elif number % 50 == 0:
            await message.channel.send(f"🔥 **{number}** — keep it going!")

    # ── /countsetup ───────────────────────────────────────────────────────────

    @app_commands.command(name="countsetup", description="Set the counting channel for this server")
    @app_commands.describe(channel="The channel where counting will happen")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slash_countsetup(self, interaction: discord.Interaction,
                               channel: discord.TextChannel):
        g = _guild(interaction.guild_id)
        g["channel_id"] = channel.id
        _save(_data)
        await interaction.response.send_message(
            f"✅ Counting channel set to {channel.mention}!\n"
            f"Players must count up from **1** in order. No double-counting allowed.",
            ephemeral=False,
        )

    @commands.command(name="countsetup")
    @commands.has_permissions(manage_channels=True)
    async def prefix_countsetup(self, ctx: commands.Context, channel: discord.TextChannel = None):
        if channel is None:
            channel = ctx.channel
        g = _guild(ctx.guild.id)
        g["channel_id"] = channel.id
        _save(_data)
        await ctx.reply(
            f"✅ Counting channel set to {channel.mention}!\n"
            f"Players must count up from **1** in order. No double-counting allowed."
        )

    # ── /countstats ───────────────────────────────────────────────────────────

    @app_commands.command(name="countstats", description="Show the current count and high score")
    async def slash_countstats(self, interaction: discord.Interaction):
        await self._send_stats(interaction.guild, lambda **kw: interaction.response.send_message(**kw))

    @commands.command(name="countstats")
    async def prefix_countstats(self, ctx: commands.Context):
        await self._send_stats(ctx.guild, lambda **kw: ctx.reply(**kw))

    async def _send_stats(self, guild: discord.Guild, reply_fn):
        g = _guild(guild.id)
        channel_mention = f"<#{g['channel_id']}>" if g["channel_id"] else "Not set"
        embed = discord.Embed(title="🔢 Counting Stats", color=discord.Color.blurple())
        embed.add_field(name="📍 Channel",      value=channel_mention,       inline=False)
        embed.add_field(name="🔢 Current Count", value=f"**{g['count']}**",  inline=True)
        embed.add_field(name="🏆 High Score",    value=f"**{g['high_score']}**", inline=True)
        last_uid = g.get("last_user_id")
        if last_uid:
            embed.add_field(name="👤 Last Counter", value=f"<@{last_uid}>", inline=True)
        await reply_fn(embed=embed)

    # ── /countreset ───────────────────────────────────────────────────────────

    @app_commands.command(name="countreset", description="Reset the count to 0 (admin only)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def slash_countreset(self, interaction: discord.Interaction):
        await self._do_reset(interaction.guild_id,
                             lambda **kw: interaction.response.send_message(**kw))

    @commands.command(name="countreset")
    @commands.has_permissions(manage_messages=True)
    async def prefix_countreset(self, ctx: commands.Context):
        await self._do_reset(ctx.guild.id, lambda **kw: ctx.reply(**kw))

    async def _do_reset(self, guild_id: int, reply_fn):
        g = _guild(guild_id)
        g["count"]        = 0
        g["last_user_id"] = None
        _save(_data)
        await reply_fn(content="🔄 Count has been reset to **0**.")

    # ── Error handlers ────────────────────────────────────────────────────────

    @slash_countsetup.error
    async def _countsetup_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ You need **Manage Channels** permission to set the counting channel.",
                ephemeral=True,
            )

    @slash_countreset.error
    async def _countreset_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ You need **Manage Messages** permission to reset the count.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Counting(bot))