import discord
from discord.ext import commands
from discord import app_commands
import os
from cogs.http_session import get_session

BUGREPORT_WEBHOOK_URL = os.getenv("BUGREPORT_WEBHOOK_URL", "")
MAX_REPORT_LEN = 1000


async def _send_bugreport(user: discord.User | discord.Member, text: str, guild: discord.Guild | None) -> bool:
    if not BUGREPORT_WEBHOOK_URL:
        return False
    try:
        session = get_session()
        webhook = discord.Webhook.from_url(BUGREPORT_WEBHOOK_URL, session=session)
        embed = discord.Embed(
            title="🐛 New Bug Report",
            description=text,
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        embed.add_field(name="User ID", value=f"`{user.id}`", inline=True)
        embed.add_field(name="Server",  value=guild.name if guild else "DM", inline=True)
        embed.set_footer(text="Jarvis Bug Reports")
        await webhook.send(embed=embed, username="Jarvis Bug Reports")
        return True
    except Exception:
        return False


class BugReport(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="bugreport")
    async def prefix_bugreport(self, ctx: commands.Context, *, report: str = None):
        """Submit a bug report. Usage: !bugreport <description>"""
        if not report:
            await ctx.reply(
                "**Usage:** `!bugreport <description>`\n"
                "**Example:** `!bugreport The /chat command stops responding after 5 messages`"
            )
            return
        if len(report) > MAX_REPORT_LEN:
            await ctx.reply(f"❌ Report too long. Maximum is {MAX_REPORT_LEN} characters.")
            return
        success = await _send_bugreport(ctx.author, report, ctx.guild)
        if success:
            await ctx.reply("✅ Bug report submitted! Thanks for helping improve Jarvis.")
        else:
            await ctx.reply("⚠️ Bug reports are not configured yet. Please contact the bot owner.")

    @app_commands.command(name="bugreport", description="Submit a bug report for Jarvis")
    @app_commands.describe(report="Describe the bug you encountered")
    async def slash_bugreport(self, interaction: discord.Interaction, report: str):
        if len(report) > MAX_REPORT_LEN:
            await interaction.response.send_message(
                f"❌ Report too long. Maximum is {MAX_REPORT_LEN} characters.", ephemeral=True
            )
            return
        success = await _send_bugreport(interaction.user, report, interaction.guild)
        if success:
            await interaction.response.send_message(
                "✅ Bug report submitted! Thanks for helping improve Jarvis.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "⚠️ Bug reports are not configured yet. Please contact the bot owner.", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(BugReport(bot))