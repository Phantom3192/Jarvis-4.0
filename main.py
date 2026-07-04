import discord
from discord.ext import commands
import os
import asyncio
import logging
import signal
import time
from dotenv import load_dotenv
from cogs.errorhandler import install_stdout_error_forwarding, install_view_error_suppression
from cogs.state import is_bot_banned, init_db, check_burst_and_maybe_timeout, check_cooldown, flush_all_saves
import cogs.http_session as http_session
from cogs.history import init_history, load_all_histories
from cogs.memory import init_memory
import uvicorn
from web.app import create_app

load_dotenv()
install_stdout_error_forwarding()   # forward ❌/error-looking print() lines to the webhook too
install_view_error_suppression()    # silence harmless expired-interaction noise from button clicks

logging.basicConfig(level=logging.WARNING)

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.members         = True
intents.voice_states = True


bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    allowed_mentions=discord.AllowedMentions.none(),
)

COGS = [
    "cogs.ai",
    "cogs.admin",
    "cogs.panel",
    "cogs.stats",
    "cogs.prompts",
    "cogs.announce",
    "cogs.dm",
    "cogs.suggestions",
    "cogs.bugreport",
    "cogs.errorhandler",
    "cogs.help",
    "cogs.game",
    "cogs.system",
    "cogs.image_search",
    "cogs.summary",
    "cogs.presence",
    "cogs.youtube",
    "cogs.music",
    "cogs.economy",
    "cogs.status",
]

# Track users we've already DM'd about their ban this session — avoid spamming
_dm_sent_bans: set[int] = set()


def _is_guild_banned(guild_id: int) -> bool:
    """Lazy import to avoid circular dependency at module load time."""
    try:
        from cogs.admin import _guild_bans
        return guild_id in _guild_bans
    except ImportError:
        return False


async def _notify_banned(user: discord.User | discord.Member) -> None:
    """DM a banned user once per session to inform them."""
    if user.id in _dm_sent_bans:
        return
    _dm_sent_bans.add(user.id)
    try:
        embed = discord.Embed(
            title="🚫 Banned from Jarvis",
            description="You are banned from using **Jarvis** and cannot use any of its commands.",
            color=discord.Color.red(),
        )
        embed.set_footer(text="If you believe this is a mistake, contact Phantom.")
        await user.send(embed=embed)
    except discord.Forbidden:
        pass


@bot.check
async def global_ban_check(ctx: commands.Context) -> bool:
    # Guild-level ban check
    if ctx.guild and _is_guild_banned(ctx.guild.id):
        await ctx.reply("🚫 This server has been banned from using Jarvis.")
        return False

    if is_bot_banned(ctx.author.id):
        await ctx.reply("🚫 You are banned from using Jarvis.")
        await _notify_banned(ctx.author)
        return False

    if await bot.is_owner(ctx.author):
        return True

    # Burst/timeout check
    allowed, t = check_burst_and_maybe_timeout(ctx.author.id)
    if not allowed:
        await ctx.reply(
            f"⏱️ You have been temporarily blocked from using Jarvis for {int(t)} seconds due to command flooding."
        )
        await _notify_banned(ctx.author)
        return False
    if not check_cooldown(ctx.author.id):
        await ctx.message.add_reaction("⏳")
        return False
    return True


async def slash_ban_check(interaction: discord.Interaction) -> bool:
    if is_bot_banned(interaction.user.id):
        await interaction.response.send_message("🚫 You are banned from using Jarvis.", ephemeral=True)
        await _notify_banned(interaction.user)
        return False
    return True

async def slash_interaction_check(interaction: discord.Interaction) -> bool:
    # Guild-level ban check
    if interaction.guild and _is_guild_banned(interaction.guild.id):
        await interaction.response.send_message(
            "🚫 This server has been banned from using Jarvis.", ephemeral=True
        )
        return False

    if not await slash_ban_check(interaction):
        return False

    if await bot.is_owner(interaction.user):
        return True

    allowed, t = check_burst_and_maybe_timeout(interaction.user.id)
    if not allowed:
        await interaction.response.send_message(
            f"⏱️ You have been temporarily blocked from using Jarvis for {int(t)} seconds due to command flooding.",
            ephemeral=True,
        )
        await _notify_banned(interaction.user)
        return False
    if not check_cooldown(interaction.user.id):
        await interaction.response.send_message("⏳", ephemeral=True)
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

async def run_web_server() -> None:
    """Run the Jarvis stats/categories API in this same process.

    Sharing the process means the API's /api/stats route can read
    bot.guilds / seen_users directly — no network hop, no second deployment.
    The actual website (HTML/CSS/JS) is a SEPARATE project deployed on its
    own — it polls this API over HTTP. Failure here should never take the
    bot down, so errors are caught and logged.
    """
    try:
        app = create_app(bot)
        port = int(os.getenv("PORT", "8000"))
        config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()
    except Exception as e:
        print(f"❌ Web server failed to start: {e}")

async def _flush_and_exit() -> None:
    """SIGTERM/SIGINT handler: flush any pending debounced state saves to
    Turso before the process actually terminates, then exit immediately.

    Railway (and most container platforms) send SIGTERM on every
    redeploy/restart. Python's default SIGTERM handling kills the process
    right away without running pending asyncio tasks or `finally` blocks,
    so anything still sitting inside state.py's 2s debounce window would
    otherwise be silently lost — this is what was causing stats like
    Messages/Tokens/Last Active to intermittently revert after a restart.
    """
    print("🛑 Shutdown signal received — flushing pending state before exit…")
    try:
        await flush_all_saves()
    except Exception as e:
        print(f"❌ Error flushing state on shutdown: {e}")
    try:
        # Lazy import — cogs.game is already loaded via load_extension() by
        # the time a shutdown signal can fire, so this just looks up the
        # already-imported module rather than re-running its module-level
        # code (unlike importing it eagerly at the top of this file, which
        # would double-init it the same way cogs.ai/cogs.system avoid above).
        from cogs.game import flush_all_counting_saves
        flush_all_counting_saves()
    except Exception as e:
        print(f"❌ Error flushing counting state on shutdown: {e}")
    os._exit(0)


def _install_signal_handlers() -> None:
    """Registered once at startup — replaces the default SIGTERM/SIGINT
    behaviour with the flush-then-exit sequence above."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_flush_and_exit()))
        except (NotImplementedError, RuntimeError):
            # Not supported on some platforms (e.g. Windows for SIGTERM) —
            # falls back to default signal handling there.
            pass

async def main():
    # Validate token early for a clear error message
    if not TOKEN:
        print("❌ DISCORD_TOKEN is not set in your .env file. Exiting.")
        return

    _install_signal_handlers()

    MAX_RETRIES = 5
    BASE_DELAY  = 10    # seconds before first retry
    MAX_DELAY   = 300   # cap at 5 minutes

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with bot:
                await http_session.create_session()
                await init_db()
                await init_history()
                await init_memory()

                # Load all cog extensions FIRST. discord.py's load_extension()
                # always re-executes a cog module from scratch (it doesn't
                # reuse sys.modules), so this must be the ONLY place cogs.ai
                # gets imported — otherwise its module-level setup (Groq /
                # Gemini client pools) runs twice and you see the pool-loaded
                # logs printed twice at startup.
                for cog in COGS:
                    try:
                        await bot.load_extension(cog)
                    except Exception as e:
                        print(f"❌ Failed to load cog '{cog}': {e}")

                # Restore persisted session histories into in-memory store.
                # Pull private_history from the already-loaded cogs.ai extension
                # instead of doing a fresh "from cogs.ai import private_history",
                # which would trigger a second import of the module.
                ai_ext = bot.extensions.get("cogs.ai")
                if ai_ext is not None:
                    private_history = ai_ext.private_history
                    restored = await load_all_histories()
                    for uid, msgs in restored.items():
                        private_history[uid].extend(msgs)
                    if restored:
                        print(f"✅ Restored session history for {len(restored)} user(s)")
                else:
                    print("❌ cogs.ai failed to load — skipping session history restore.")

                # try:
                #     await bot.start(TOKEN)
                # finally:
                #     await http_session.close_session()
                
                try:
                    await asyncio.gather(
                        bot.start(TOKEN),
                        run_web_server(),
                    )
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