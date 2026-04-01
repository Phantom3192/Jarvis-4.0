import discord
from discord.ext import commands
from discord import app_commands
import os
from cogs.http_session import get_session

SUGGESTION_WEBHOOK_URL = os.getenv("SUGGESTION_WEBHOOK_URL", "")
MAX_SUGGESTION_LEN = 1000


async def _send_suggestion(user: discord.User | discord.Member, text: str, guild: discord.Guild | None) -> bool:
    if not SUGGESTION_WEBHOOK_URL:
        return False
    try:
        session = get_session()
        webhook = discord.Webhook.from_url(SUGGESTION_WEBHOOK_URL, session=session)
        embed = discord.Embed(
            title="💡 New Suggestion",
            description=text,
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        embed.add_field(name="User ID", value=f"`{user.id}`", inline=True)
        embed.add_field(name="Server",  value=guild.name if guild else "DM", inline=True)
        embed.set_footer(text="Jarvis Suggestions")
        await webhook.send(embed=embed, username="Jarvis Suggestions")
        return True
    except Exception as e:
        print(f"❌ Suggestion webhook error: {e}")
        return False


class Suggestions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="feedback")
    async def prefix_suggestion(self, ctx: commands.Context, *, suggestion: str = None):
        """Submit feedback. Usage: !feedback <your feedback>"""
        if not suggestion:
            await ctx.reply(
                "**Usage:** `!feedback <your feedback>`\n"
                "**Example:** `!feedback Add a /help command`"
            )
            return
        if len(suggestion) > MAX_SUGGESTION_LEN:
            await ctx.reply(f"❌ Suggestion too long. Maximum is {MAX_SUGGESTION_LEN} characters.")
            return
        success = await _send_suggestion(ctx.author, suggestion, ctx.guild)
        if success:
            await ctx.reply("✅ Your suggestion has been submitted! Thanks for the feedback.")
        else:
            await ctx.reply("⚠️ Suggestions are not configured yet. Please contact the bot owner.")

    @app_commands.command(name="feedback", description="Submit feedback for Jarvis")
    @app_commands.describe(suggestion="Your feedback")
    async def slash_suggestion(self, interaction: discord.Interaction, suggestion: str):
        if len(suggestion) > MAX_SUGGESTION_LEN:
            await interaction.response.send_message(
                f"❌ Suggestion too long. Maximum is {MAX_SUGGESTION_LEN} characters.", ephemeral=True
            )
            return
        success = await _send_suggestion(interaction.user, suggestion, interaction.guild)
        if success:
            await interaction.response.send_message(
                "✅ Your suggestion has been submitted! Thanks for the feedback.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "⚠️ Suggestions are not configured yet. Please contact the bot owner.", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Suggestions(bot))