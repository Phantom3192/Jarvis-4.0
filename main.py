import discord
from discord.ext import commands
import os
import asyncio
from dotenv import load_dotenv
from cogs.state import is_bot_banned

load_dotenv()

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
        for cog in COGS:
            await bot.load_extension(cog)
        await bot.start(TOKEN)


asyncio.run(main())