import discord
from discord.ext import commands
import os
import asyncio
from dotenv import load_dotenv

load_dotenv()  # Load environment variables from .env file

from cogs.state import is_bot_banned
import cogs.http_session as http_session
from cogs.state import init_db
from cogs.history import init_history


TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

COGS = [
    "cogs.ai",
    "cogs.admin",
    "cogs.stats",
    "cogs.prompts",
    "cogs.announce",
    "cogs.dm",
    "cogs.suggestions",
    "cogs.bugreport",
    "cogs.errorhandler",
    "cogs.help",
    "cogs.fun",
]


# Block banned users from all prefix commands
@bot.check
async def global_ban_check(ctx: commands.Context) -> bool:
    if is_bot_banned(ctx.author.id):
        await ctx.reply("🚫 You are banned from using Jarvis.")
        try:
            await ctx.author.send("🚫 You are banned from using **Jarvis** and cannot use any of its commands.")
        except discord.Forbidden:
            pass
        return False
    return True


# Block banned users from all slash commands
async def slash_ban_check(interaction: discord.Interaction) -> bool:
    if is_bot_banned(interaction.user.id):
        await interaction.response.send_message("🚫 You are banned from using Jarvis.", ephemeral=True)
        try:
            await interaction.user.send("🚫 You are banned from using **Jarvis** and cannot use any of its commands.")
        except discord.Forbidden:
            pass
        return False
    return True

bot.tree.interaction_check = slash_ban_check


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"✅ Jarvis online as {bot.user} | Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"❌ Failed to sync commands: {e}")


async def main():
    async with bot:
        # Create the shared HTTP session before loading cogs (cogs import it at call-time)
        await http_session.create_session()
        await init_db()  
        await init_history()   # ← ADD THIS LINE

        try:
            for cog in COGS:
                await bot.load_extension(cog)
            await bot.start(TOKEN)
        finally:
            # Always close the session cleanly, even on crash
            await http_session.close_session()


asyncio.run(main())