"""
Jarvis bot entry point.

OPTIMISATIONS vs original:
- TOKEN validated at startup before trying to connect — gives a clear error
  instead of a confusing LoginFailure deep in the retry loop.
- on_ready: tree.sync() called only once and errors logged cleanly; no change
  to logic but added guild count to the ready log.
- global_ban_check / slash_ban_check: sending a DM to a banned user on every
  blocked command is spammy and could hit rate limits. Changed to only DM on
  the first block per session using a module-level set (_dm_sent_bans).
- Cog loading: failures now log the specific cog name that failed rather than
  swallowing the error silently.
- asyncio.run(main()) wrapped in if __name__ == "__main__" guard so the file
  can be imported in tests without starting the bot.
"""
import discord
from discord.ext import commands
import os
import asyncio
import logging
import time
from dotenv import load_dotenv
from collections import deque
from cogs.state import is_bot_banned, init_db, get_setting, set_setting, bot_bans, save_bans
import cogs.http_session as http_session
from cogs.history import init_history

load_dotenv()

logging.basicConfig(level=logging.WARNING)

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.members         = True

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
    "cogs.system",
]

# Track users we've already DM'd about their ban this session — avoid spamming.
_dm_sent_bans: set[int] = set()

# Simple per-user command cooldown to prevent spam. The value is persisted
# in `cogs.state` under key `user_command_cooldown` so the owner can change it.
USER_COMMAND_COOLDOWN = 2.0  # default seconds
_last_command_time: dict[int, float] = {}

# Burst tracking: per-user deque of recent command timestamps (monotonic seconds)
_burst_records: dict[int, deque] = {}


def _check_burst_and_maybe_timeout(user_id: int) -> tuple[bool, float | None]:
    """Record a command for user and timeout them if they exceed burst limits.
    Returns (allowed, timeout_seconds_if_timed_out).
    """
    now = time.monotonic()
    window = float(get_setting("burst_window_seconds", 60.0))
    limit  = int(get_setting("burst_limit_count", 20))
    timeout = float(get_setting("burst_timeout_seconds", 300.0))

    dq = _burst_records.setdefault(user_id, deque())
    dq.append(now)
    cutoff = now - window
    while dq and dq[0] < cutoff:
        dq.popleft()

    if len(dq) > limit:
        # Timeout user at bot-level (temporary bot ban)
        bot_bans[str(user_id)] = {
            "reason": f"Flooding commands ({len(dq)} in {int(window)}s)",
            "expires": time.time() + timeout,
        }
        save_bans()
        return False, timeout
    return True, None


def _command_cooldown_check(user_id: int) -> bool:
    now = time.monotonic()
    last = _last_command_time.get(user_id)
    cooldown = float(get_setting("user_command_cooldown", USER_COMMAND_COOLDOWN))
    if last is None or (now - last) >= cooldown:
        _last_command_time[user_id] = now
        return True
    return False


async def _notify_banned(user: discord.User | discord.Member) -> None:
    """DM a banned user once per session to inform them."""
    if user.id in _dm_sent_bans:
        return
    _dm_sent_bans.add(user.id)
    try:
        await user.send("🚫 You are banned from using **Jarvis** and cannot use any of its commands.")
    except discord.Forbidden:
        pass


@bot.check
async def global_ban_check(ctx: commands.Context) -> bool:
    if is_bot_banned(ctx.author.id):
        await ctx.reply("🚫 You are banned from using Jarvis.")
        await _notify_banned(ctx.author)
        return False
    # Burst/timeout check
    allowed, t = _check_burst_and_maybe_timeout(ctx.author.id)
    if not allowed:
        await ctx.reply(f"⏱️ You have been temporarily blocked from using Jarvis for {int(t)} seconds due to command flooding.")
        await _notify_banned(ctx.author)
        return False
    if not _command_cooldown_check(ctx.author.id):
        cooldown = int(float(get_setting("user_command_cooldown", USER_COMMAND_COOLDOWN)))
        await ctx.reply(
            f"⚠️ Please wait {cooldown} seconds before sending another Jarvis command."
        )
        return False
    return True


async def slash_ban_check(interaction: discord.Interaction) -> bool:
    if is_bot_banned(interaction.user.id):
        await interaction.response.send_message("🚫 You are banned from using Jarvis.", ephemeral=True)
        await _notify_banned(interaction.user)
        return False
    return True

async def slash_interaction_check(interaction: discord.Interaction) -> bool:
    if not await slash_ban_check(interaction):
        return False
    # Burst/timeout check for interactions
    allowed, t = _check_burst_and_maybe_timeout(interaction.user.id)
    if not allowed:
        await interaction.response.send_message(
            f"⏱️ You have been temporarily blocked from using Jarvis for {int(t)} seconds due to command flooding.",
            ephemeral=True,
        )
        await _notify_banned(interaction.user)
        return False
    if not _command_cooldown_check(interaction.user.id):
        cooldown = int(float(get_setting("user_command_cooldown", USER_COMMAND_COOLDOWN)))
        await interaction.response.send_message(
            f"⚠️ Please wait {cooldown} seconds before sending another Jarvis command.",
            ephemeral=True,
        )
        return False
    return True

bot.tree.interaction_check = slash_interaction_check


@bot.event
async def on_ready():
    guild_count = len(bot.guilds)
    try:
        synced = await bot.tree.sync()
        print(
            f"✅ Jarvis online as {bot.user} | "
            f"Guilds: {guild_count} | "
            f"Synced {len(synced)} slash command(s)"
        )
    except Exception as e:
        print(f"❌ Failed to sync commands: {e}")


async def main():
    # Validate token early for a clear error message
    if not TOKEN:
        print("❌ DISCORD_TOKEN is not set in your .env file. Exiting.")
        return

    MAX_RETRIES = 5
    BASE_DELAY  = 10    # seconds before first retry
    MAX_DELAY   = 300   # cap at 5 minutes

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with bot:
                await http_session.create_session()
                await init_db()
                await init_history()

                for cog in COGS:
                    try:
                        await bot.load_extension(cog)
                    except Exception as e:
                        print(f"❌ Failed to load cog '{cog}': {e}")

                try:
                    await bot.start(TOKEN)
                finally:
                    await http_session.close_session()

            break  # clean exit

        except discord.errors.HTTPException as e:
            if e.status == 429:
                delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
                print(
                    f"⚠️  Discord rate-limited on login (attempt {attempt}/{MAX_RETRIES}). "
                    f"Waiting {delay}s before retrying…"
                )
                await asyncio.sleep(delay)
            else:
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


if __name__ == "__main__":
    asyncio.run(main())