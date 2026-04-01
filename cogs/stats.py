import discord
from discord.ext import commands
from discord import app_commands
import time
from cogs.state import get_stats, is_bot_banned
from cogs.admin import is_admin  # reuse existing helper


def _format_stats(user: discord.User | discord.Member, data: dict) -> discord.Embed:
    """Build a stats embed for a user."""
    embed = discord.Embed(
        title=f"📊 Jarvis Stats — {user.display_name}",
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=user.display_avatar.url)

    embed.add_field(name="Messages Sent", value=f"`{data['messages']:,}`", inline=True)
    embed.add_field(name="~Tokens Used",  value=f"`{data['tokens_est']:,}`", inline=True)
    embed.add_field(name="\u200b",        value="\u200b", inline=True)  # spacer

    first = discord.utils.format_dt(
        discord.utils.utcnow().__class__.fromtimestamp(data["first_seen"], tz=discord.utils.utcnow().tzinfo),
        style="R",
    )
    last = discord.utils.format_dt(
        discord.utils.utcnow().__class__.fromtimestamp(data["last_seen"], tz=discord.utils.utcnow().tzinfo),
        style="R",
    )
    embed.add_field(name="First Interaction", value=first, inline=True)
    embed.add_field(name="Last Interaction",  value=last,  inline=True)
    embed.set_footer(text="Token count is an estimate (~4 chars/token)")
    return embed


class Stats(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Prefix command ────────────────────────────────────────────────────────

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
        data = get_stats(target.id)
        if not data:
            if target.id == ctx.author.id:
                await ctx.reply("📭 You haven't interacted with Jarvis yet.")
            else:
                await ctx.reply(f"📭 **{target}** has no recorded interactions with Jarvis.")
            return

        await ctx.reply(embed=_format_stats(target, data))

    # ── Slash command ─────────────────────────────────────────────────────────

    @app_commands.command(name="stats", description="View Jarvis usage stats")
    @app_commands.describe(user="User to look up (admins only — leave empty for your own stats)")
    async def slash_stats(self, interaction: discord.Interaction, user: discord.User = None):
        if user is not None and not is_admin(interaction.user):
            await interaction.response.send_message(
                "🚫 Only admins can view other users' stats.", ephemeral=True
            )
            return

        target = user or interaction.user
        data = get_stats(target.id)
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