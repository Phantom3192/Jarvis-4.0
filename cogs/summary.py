"""
Summary cog — summarises the invoking user's personal conversation with Jarvis.

Only messages from the user themselves OR from the bot are included.
Other users' messages are ignored entirely.

Usage:
    !summary            → summarise your last 50 exchanges with Jarvis
    !summary 30m        → your conversation with Jarvis in the last 30 minutes
    !summary 1h         → last 1 hour
    !summary 2h         → last 2 hours
    !summary 6h         → last 6 hours
    !summary 24h / 1d   → last 24 hours (max)

    /summary [window]   → slash command equivalent

Time window formats accepted:
    <N>m   — minutes  (e.g. 30m, 90m)
    <N>h   — hours    (e.g. 1h, 6h)
    <N>d   — days     (e.g. 1d, 2d)
    (no arg) — defaults to last 50 messages (user + bot combined)

Limits:
    - Max lookback: 24 hours
    - Max messages scanned from Discord history: 500
    - Only the invoking user's messages and Jarvis's replies are summarised
"""

import re
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone, timedelta

from cogs.http_session import safe_reply
from cogs.state import is_ai_rate_limited, increment_ai_usage

# ── Config ────────────────────────────────────────────────────────────────────

MAX_SCAN         = 500    # max messages fetched from Discord history
DEFAULT_SCAN     = 200    # messages to scan when no time window given
MAX_LOOKBACK_H   = 24     # maximum time window (hours)
SUMMARY_TIMEOUT  = 20     # seconds to wait for AI provider

# Regex: integer + unit m/h/d
_WINDOW_RE = re.compile(r"^(\d+)\s*([mhd])$", re.IGNORECASE)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_window(arg: str) -> tuple[int, str] | None:
    """
    Parse '30m', '2h', '1d' → (total_seconds, human_label).
    Returns None if the format is unrecognised.
    """
    m = _WINDOW_RE.match(arg.strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit == "m":
        return n * 60,    f"{n} minute{'s' if n != 1 else ''}"
    if unit == "h":
        return n * 3600,  f"{n} hour{'s' if n != 1 else ''}"
    if unit == "d":
        return n * 86400, f"{n} day{'s' if n != 1 else ''}"
    return None


def _format_conversation(
    messages: list[discord.Message],
    user: discord.User | discord.Member,
    bot_id: int,
) -> str:
    """
    Build a plain-text transcript of only the user ↔ Jarvis exchange.
    Format: [HH:MM] You: ...   /   [HH:MM] Jarvis: ...
    """
    lines = []
    for msg in messages:
        content = msg.content.strip()
        if not content:
            continue
        ts = msg.created_at.strftime("%H:%M")
        if msg.author.id == user.id:
            lines.append(f"[{ts}] You: {content}")
        elif msg.author.id == bot_id:
            lines.append(f"[{ts}] Jarvis: {content}")
    return "\n".join(lines)


async def _fetch_by_time(channel, seconds: int) -> list[discord.Message]:
    after_dt = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    msgs = []
    async for m in channel.history(limit=MAX_SCAN, after=after_dt, oldest_first=True):
        msgs.append(m)
    return msgs


async def _fetch_default(channel) -> list[discord.Message]:
    msgs = []
    async for m in channel.history(limit=DEFAULT_SCAN, oldest_first=True):
        msgs.append(m)
    return msgs


# ── AI summariser ─────────────────────────────────────────────────────────────

async def _call_ai_summary(transcript: str, label: str) -> str:
    system = (
        "You are Jarvis, a helpful Discord assistant. "
        "You are given a transcript of a conversation between a user and yourself. "
        "Summarise what the user asked about, what you helped with, and any key outcomes or answers. "
        "Use bullet points for clarity. Be concise. "
        "If there is very little content, say so briefly."
    )
    prompt = (
        f"Summarise this conversation between me (Jarvis) and the user ({label}):\n\n"
        f"{transcript}\n\n"
        "Provide a clear, structured summary of what was discussed."
    )

    from cogs.ai import _try_groq, _try_gemini, GEMINI_MODEL_FLASH, GEMINI_API_KEY

    messages = [{"role": "user", "content": prompt}]

    try:
        result = await asyncio.wait_for(_try_groq(messages, system), timeout=SUMMARY_TIMEOUT)
        if result:
            return result
    except asyncio.TimeoutError:
        pass

    try:
        result = await asyncio.wait_for(
            _try_gemini(GEMINI_API_KEY, GEMINI_MODEL_FLASH, messages, system),
            timeout=SUMMARY_TIMEOUT,
        )
        if result:
            return result
    except asyncio.TimeoutError:
        pass

    return "⚠️ AI providers are currently unavailable. Please try again in a moment."


# ── Core logic ────────────────────────────────────────────────────────────────

async def _do_summary(
    bot: commands.Bot,
    channel,
    invoker: discord.User | discord.Member,
    reply_target,
    window_arg: str | None,
    is_slash: bool = False,
) -> None:

    # ── Parse window ──────────────────────────────────────────────────────────
    label   = "your recent conversation with me"
    seconds = None

    if window_arg:
        parsed = _parse_window(window_arg)
        if parsed is None:
            msg = (
                "❌ Invalid format. Use `!summary 30m`, `!summary 2h`, or `!summary 1d`.\n"
                "Supported units: `m` minutes · `h` hours · `d` days"
            )
            if is_slash:
                await reply_target.followup.send(msg, ephemeral=True)
            else:
                await safe_reply(reply_target, msg)
            return

        seconds, human = parsed
        if seconds > MAX_LOOKBACK_H * 3600:
            msg = f"⏳ Maximum lookback is **{MAX_LOOKBACK_H} hours**. Please use a shorter window."
            if is_slash:
                await reply_target.followup.send(msg, ephemeral=True)
            else:
                await safe_reply(reply_target, msg)
            return

        label = f"last {human}"

    # ── Rate limit ────────────────────────────────────────────────────────────
    if is_ai_rate_limited(invoker.id):
        msg = "⏳ You've hit your daily AI message limit. Try again tomorrow!"
        if is_slash:
            await reply_target.followup.send(msg, ephemeral=True)
        else:
            await safe_reply(reply_target, msg)
        return

    # ── Fetch channel history ─────────────────────────────────────────────────
    try:
        raw = await (_fetch_by_time(channel, seconds) if seconds else _fetch_default(channel))
    except discord.Forbidden:
        msg = "❌ I don't have permission to read message history in this channel."
        if is_slash:
            await reply_target.followup.send(msg, ephemeral=True)
        else:
            await safe_reply(reply_target, msg)
        return
    except Exception as e:
        print(f"[Summary] fetch error: {e}")
        msg = "❌ Something went wrong fetching messages. Please try again."
        if is_slash:
            await reply_target.followup.send(msg, ephemeral=True)
        else:
            await safe_reply(reply_target, msg)
        return

    # ── Filter: only user ↔ Jarvis messages ──────────────────────────────────
    convo = [m for m in raw if m.author.id in (invoker.id, bot.user.id)]

    if not convo:
        msg = f"📭 I couldn't find any messages between you and me for the **{label}**."
        if is_slash:
            await reply_target.followup.send(msg, ephemeral=True)
        else:
            await safe_reply(reply_target, msg)
        return

    user_msgs = [m for m in convo if m.author.id == invoker.id]
    if len(user_msgs) < 2:
        msg = f"📭 Only **{len(user_msgs)}** message(s) from you found — not enough to summarise."
        if is_slash:
            await reply_target.followup.send(msg, ephemeral=True)
        else:
            await safe_reply(reply_target, msg)
        return

    # ── Build transcript ──────────────────────────────────────────────────────
    transcript = _format_conversation(convo, invoker, bot.user.id)

    # Truncate if massive — keep last ~8000 chars
    if len(transcript) > 8000:
        transcript = "...[earlier messages omitted]...\n" + transcript[-8000:]

    # ── Call AI ───────────────────────────────────────────────────────────────
    increment_ai_usage(invoker.id)
    summary = await _call_ai_summary(transcript, label)

    # ── Build embed ───────────────────────────────────────────────────────────
    bot_msgs  = len(convo) - len(user_msgs)
    embed = discord.Embed(
        title=f"📋 Your Conversation Summary",
        description=summary,
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Window",       value=label.title(),       inline=True)
    embed.add_field(name="Your messages", value=str(len(user_msgs)), inline=True)
    embed.add_field(name="My replies",   value=str(bot_msgs),       inline=True)
    embed.set_footer(text=f"Requested by {invoker.display_name}")
    embed.set_thumbnail(url=invoker.display_avatar.url)

    # ── Send ──────────────────────────────────────────────────────────────────
    if is_slash:
        if len(summary) > 4000:
            await reply_target.followup.send(f"**📋 Your Conversation Summary** — {label}")
            for chunk in [summary[i:i+1900] for i in range(0, len(summary), 1900)]:
                await channel.send(chunk)
        else:
            await reply_target.followup.send(embed=embed)
    else:
        if len(summary) > 4000:
            await safe_reply(reply_target, f"**📋 Your Conversation Summary** — {label}")
            for chunk in [summary[i:i+1900] for i in range(0, len(summary), 1900)]:
                await channel.send(chunk)
        else:
            await safe_reply(reply_target, embed=embed)


# ── Cog ───────────────────────────────────────────────────────────────────────

class Summary(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="summary")
    async def prefix_summary(self, ctx: commands.Context, window: str = None):
        """
        Summarise your personal conversation with Jarvis.

        Examples:
          !summary       — your last ~50 exchanges
          !summary 30m   — last 30 minutes
          !summary 2h    — last 2 hours
          !summary 1d    — last 24 hours (max)
        """
        async with ctx.typing():
            await _do_summary(
                bot=self.bot,
                channel=ctx.channel,
                invoker=ctx.author,
                reply_target=ctx.message,
                window_arg=window,
                is_slash=False,
            )

    @app_commands.command(
        name="summary",
        description="Summarise your personal conversation with Jarvis (e.g. last 1h, 30m)",
    )
    @app_commands.describe(
        window="Time window: 30m, 1h, 2h, 1d. Leave blank for your last ~50 exchanges."
    )
    async def slash_summary(self, interaction: discord.Interaction, window: str = None):
        await interaction.response.defer(thinking=True)
        await _do_summary(
            bot=self.bot,
            channel=interaction.channel,
            invoker=interaction.user,
            reply_target=interaction,
            window_arg=window,
            is_slash=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Summary(bot))