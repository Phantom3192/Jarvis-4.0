"""
Stats cog.

OPTIMISATIONS vs original:
- _format_stats used a convoluted chain of
    discord.utils.utcnow().__class__.fromtimestamp(...)
  to convert a Unix timestamp to a timezone-aware datetime. This works but is
  fragile and hard to read. Replaced with the standard:
    datetime.fromtimestamp(ts, tz=timezone.utc)
- Removed unused `import time` — time is not used in this file.
"""
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone
from cogs.state import get_stats
from cogs.admin import is_admin


def _ts_to_discord(ts: float) -> str:
    """Convert a Unix timestamp to a Discord relative timestamp string."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return discord.utils.format_dt(dt, style="R")


def _format_stats(user: discord.User | discord.Member, data: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"📊 Jarvis Stats — {user.display_name}",
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Messages Sent", value=f"`{data['messages']:,}`",   inline=True)
    embed.add_field(name="~Tokens Used",  value=f"`{data['tokens_est']:,}`", inline=True)
    embed.add_field(name="\u200b",        value="\u200b",                    inline=True)  # spacer
    embed.add_field(name="First Interaction", value=_ts_to_discord(data["first_seen"]), inline=True)
    embed.add_field(name="Last Interaction",  value=_ts_to_discord(data["last_seen"]),  inline=True)
    embed.set_footer(text="Token count is an estimate (~4 chars/token)")
    return embed


class Stats(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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


async def setup(bot: commands.Bot):
    await bot.add_cog(Stats(bot))