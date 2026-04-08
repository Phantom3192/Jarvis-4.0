import discord
from discord.ext import commands
import os
import asyncio
import logging
from dotenv import load_dotenv
from cogs.state import is_bot_banned
import cogs.http_session as http_session
from cogs.state import init_db
from cogs.history import init_history

load_dotenv()

# Suppress discord's default noisy logging; keep warnings+
logging.basicConfig(level=logging.WARNING)

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
    "cogs.imagine",
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
    # Exponential backoff on repeated 429 / login failures.
    # Prevents crash-looping from hammering Discord's login endpoint.
    MAX_RETRIES   = 5
    BASE_DELAY    = 10   # seconds before first retry
    MAX_DELAY     = 300  # cap at 5 minutes

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with bot:
                await http_session.create_session()
                await init_db()
                await init_history()

                try:
                    for cog in COGS:
                        await bot.load_extension(cog)
                    await bot.start(TOKEN)
                finally:
                    await http_session.close_session()

            # Clean exit — don't retry
            break

        except discord.errors.HTTPException as e:
            if e.status == 429:
                # Discord is rate-limiting us on login
                delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
                print(
                    f"⚠️  Discord rate-limited on login (attempt {attempt}/{MAX_RETRIES}). "
                    f"Waiting {delay}s before retrying…"
                )
                await asyncio.sleep(delay)
            else:
                # Some other HTTP error — re-raise immediately
                raise

        except discord.errors.LoginFailure:
            print("❌ Invalid DISCORD_TOKEN — check your .env file. Not retrying.")
            break

        except Exception as e:
            if attempt < MAX_RETRIES:
                delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
                print(
                    f"❌ Unexpected error (attempt {attempt}/{MAX_RETRIES}): {e}\n"
                    f"   Retrying in {delay}s…"
                )
                await asyncio.sleep(delay)
            else:
                print(f"❌ Failed after {MAX_RETRIES} attempts. Giving up.")
                raise

    else:
        print(f"❌ Exhausted {MAX_RETRIES} login attempts. Exiting.")


asyncio.run(main())