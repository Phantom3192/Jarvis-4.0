import discord
from discord.ext import commands
import os
import asyncio
import logging
import time
from dotenv import load_dotenv
from cogs.errorhandler import install_stdout_error_forwarding, install_view_error_suppression
from cogs.state import is_bot_banned, init_db, check_burst_and_maybe_timeout, check_cooldown
from cogs.state import log_guild_join, get_all_guild_logs
import cogs.http_session as http_session
from cogs.history import init_history, load_all_histories
from cogs.memory import init_memory

load_dotenv()
install_stdout_error_forwarding()   # forward ❌/error-looking print() lines to the webhook too
install_view_error_suppression()    # silence harmless expired-interaction noise from button clicks

logging.basicConfig(level=logging.WARNING)

TOKEN = os.getenv("DISCORD_TOKEN")

# ── Server Log Webhook ────────────────────────────────────────────────────────

def _server_log_webhook_url() -> str:
    return os.getenv("SERVER_LOG_WEBHOOK", "").strip()


async def _send_server_log(guild: discord.Guild, *, is_new: bool = True) -> None:
    """Send a server log embed to the SERVER_LOG_WEBHOOK.

    is_new=True  → bot just joined (green embed, real-time)
    is_new=False → historical entry being replayed on startup (grey embed)
    """
    url = _server_log_webhook_url()
    if not url:
        return
    try:
        import cogs.http_session as _http
        session = _http.get_session()
        webhook = discord.Webhook.from_url(url, session=session)

        joined_ts = (
            guild.me.joined_at.timestamp()
            if guild.me and guild.me.joined_at
            else time.time()
        )

        color = discord.Color.green() if is_new else discord.Color.greyple()
        label = "🆕 New Server Added" if is_new else "📋 Existing Server (startup log)"

        embed = discord.Embed(
            title=label,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.add_field(name="🏷️ Server Name",   value=guild.name,              inline=True)
        embed.add_field(name="🆔 Server ID",      value=f"`{guild.id}`",         inline=True)
        embed.add_field(name="👥 Members",         value=f"`{guild.member_count}`", inline=True)

        owner = guild.owner
        owner_str = str(owner) if owner else f"ID `{guild.owner_id}`"
        embed.add_field(name="👑 Owner",           value=owner_str,              inline=True)
        embed.add_field(name="📅 Bot Joined",      value=f"<t:{int(joined_ts)}:F>", inline=True)
        embed.add_field(name="🌍 Total Servers",   value=f"`{len(bot.guilds)}`", inline=True)

        if guild.description:
            embed.add_field(name="📝 Description", value=guild.description[:512], inline=False)

        embed.set_footer(text="Jarvis Server Log")
        await webhook.send(embed=embed, username="Jarvis Server Log")
    except Exception as e:
        print(f"❌ Server log webhook failed for guild {guild.id}: {e}")

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

# Track users we've already DM'd about their ban this session — avoid spamming.
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

    # ── Server log: send historical entries for every guild bot is in ─────────
    # log_guild_join() returns True only the first time a guild_id is seen,
    # so this block runs once per guild across all restarts (not on every boot).
    if _server_log_webhook_url():
        new_entries = 0
        for guild in bot.guilds:
            joined_at = (
                guild.me.joined_at.timestamp()
                if guild.me and guild.me.joined_at
                else None
            )
            is_new_entry = log_guild_join(
                guild_id=guild.id,
                name=guild.name,
                member_count=guild.member_count,
                owner_id=guild.owner_id or 0,
                joined_at=joined_at,
            )
            if is_new_entry:
                await _send_server_log(guild, is_new=False)
                new_entries += 1
                await asyncio.sleep(0.5)   # rate-limit friendly
        if new_entries:
            print(f"✅ Server log: sent {new_entries} historical guild entr{'y' if new_entries == 1 else 'ies'} to webhook")


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Fires when Jarvis is added to a new server."""
    joined_at = (
        guild.me.joined_at.timestamp()
        if guild.me and guild.me.joined_at
        else None
    )
    log_guild_join(
        guild_id=guild.id,
        name=guild.name,
        member_count=guild.member_count,
        owner_id=guild.owner_id or 0,
        joined_at=joined_at,
    )
    await _send_server_log(guild, is_new=True)


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
                await init_memory()

                # Restore persisted session histories into in-memory store
                from cogs.ai import private_history
                restored = await load_all_histories()
                for uid, msgs in restored.items():
                    private_history[uid].extend(msgs)
                if restored:
                    print(f"✅ Restored session history for {len(restored)} user(s)")

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