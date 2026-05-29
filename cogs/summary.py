"""
Summary cog — summarises recent channel messages using AI.

Usage:
    !summary            → summarise the last 50 messages
    !summary 30m        → summarise messages from the last 30 minutes
    !summary 1h         → summarise messages from the last 1 hour
    !summary 2h         → summarise messages from the last 2 hours
    !summary 6h         → summarise messages from the last 6 hours
    !summary 24h / 1d   → summarise messages from the last 24 hours

    /summary [window]   → slash command equivalent

Time window formats accepted:
    <N>m   — minutes  (e.g. 30m, 90m)
    <N>h   — hours    (e.g. 1h, 6h)
    <N>d   — days     (e.g. 1d, 2d)
    (no arg) — defaults to last 50 messages regardless of time

Limits:
    - Max lookback: 24 hours (to avoid enormous fetches)
    - Max messages scanned: 500
    - Bot messages are excluded from the summary content
    - Requires the bot to have read history permission in the channel
"""

import re
import time
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone, timedelta

from cogs.http_session import safe_reply
from cogs.message_splitter import send_long_message
from cogs.state import is_ai_rate_limited, increment_ai_usage

# ── Config ────────────────────────────────────────────────────────────────────

MAX_MESSAGES     = 500          # hard cap on messages fetched from Discord
DEFAULT_MESSAGES = 50           # used when no time window is specified
MAX_LOOKBACK_H   = 24           # maximum time window allowed (hours)
SUMMARY_TIMEOUT  = 20           # seconds to wait for AI response
MAX_TOKENS       = 600          # summary can be a bit longer than chat replies

# Regex: optional integer + unit (m/h/d), case-insensitive
_WINDOW_RE = re.compile(r"^(\d+)\s*([mhd])$", re.IGNORECASE)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_window(arg: str) -> tuple[int, str] | None:
    """
    Parse a time window string like '30m', '2h', '1d'.
    Returns (seconds, human_label) or None if invalid.
    """
    m = _WINDOW_RE.match(arg.strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit == "m":
        return n * 60, f"{n} minute{'s' if n != 1 else ''}"
    if unit == "h":
        return n * 3600, f"{n} hour{'s' if n != 1 else ''}"
    if unit == "d":
        return n * 86400, f"{n} day{'s' if n != 1 else ''}"
    return None


def _format_messages(messages: list[discord.Message]) -> str:
    """
    Turn a list of Discord messages into a plain-text transcript for the AI.
    Format: [HH:MM] Username: content
    """
    lines = []
    for msg in messages:
        # Skip empty messages (e.g. pure embeds / attachments)
        content = msg.content.strip()
        if not content:
            continue
        ts = msg.created_at.strftime("%H:%M")
        name = msg.author.display_name
        lines.append(f"[{ts}] {name}: {content}")
    return "\n".join(lines)


async def _fetch_messages_by_time(
    channel: discord.TextChannel | discord.DMChannel,
    seconds: int,
) -> list[discord.Message]:
    """Fetch up to MAX_MESSAGES messages from the last `seconds` seconds."""
    after_dt = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    messages = []
    async for msg in channel.history(limit=MAX_MESSAGES, after=after_dt, oldest_first=True):
        messages.append(msg)
    return messages


async def _fetch_messages_default(
    channel: discord.TextChannel | discord.DMChannel,
) -> list[discord.Message]:
    """Fetch the last DEFAULT_MESSAGES messages (no time filter)."""
    messages = []
    async for msg in channel.history(limit=DEFAULT_MESSAGES, oldest_first=True):
        messages.append(msg)
    return messages


# ── AI call ───────────────────────────────────────────────────────────────────

async def _call_ai_summary(transcript: str, label: str) -> str:
    """
    Call Groq (preferred) or Gemini to summarise the transcript.
    Falls back gracefully if providers fail.
    """
    system = (
        "You are Jarvis, a helpful Discord assistant. "
        "Your task is to summarise the conversation provided. "
        "Be concise but thorough. Use bullet points for key topics. "
        "Mention notable moments, decisions, or conclusions if any. "
        "Do not repeat raw messages — synthesise them into a clear summary. "
        "If the conversation is trivial or too short, say so briefly."
    )
    prompt = (
        f"Please summarise the following Discord conversation ({label}):\n\n"
        f"{transcript}\n\n"
        "Provide a clear, structured summary."
    )

    # Lazy import to avoid circular deps (ai.py imports state, etc.)
    from cogs.ai import _try_groq, _try_gemini, GEMINI_MODEL_FLASH, GEMINI_API_KEY

    messages = [{"role": "user", "content": prompt}]

    # Try Groq first
    try:
        result = await asyncio.wait_for(_try_groq(messages, system), timeout=SUMMARY_TIMEOUT)
        if result:
            return result
    except asyncio.TimeoutError:
        pass

    # Fallback to Gemini Flash
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
    channel: discord.TextChannel | discord.DMChannel,
    invoker: discord.User | discord.Member,
    reply_target,          # discord.Message (prefix) or discord.Interaction (slash)
    window_arg: str | None,
    is_slash: bool = False,
) -> None:
    """Shared implementation for both prefix and slash commands."""

    # ── Parse time window ──
    label = f"last {DEFAULT_MESSAGES} messages"
    seconds = None

    if window_arg:
        parsed = _parse_window(window_arg)
        if parsed is None:
            msg = (
                "❌ Invalid time format. Use `!summary 30m`, `!summary 2h`, or `!summary 1d`.\n"
                "Supported units: `m` (minutes), `h` (hours), `d` (days)."
            )
            if is_slash:
                await reply_target.followup.send(msg, ephemeral=True)
            else:
                await safe_reply(reply_target, msg)
            return

        seconds, label = parsed
        max_seconds = MAX_LOOKBACK_H * 3600

        if seconds > max_seconds:
            msg = f"⏳ Maximum lookback is **{MAX_LOOKBACK_H} hours**. Please use a shorter window."
            if is_slash:
                await reply_target.followup.send(msg, ephemeral=True)
            else:
                await safe_reply(reply_target, msg)
            return

        label = f"last {label}"

    # ── Check AI rate limit ──
    if is_ai_rate_limited(invoker.id):
        msg = "⏳ You've hit your daily AI message limit. Try again tomorrow!"
        if is_slash:
            await reply_target.followup.send(msg, ephemeral=True)
        else:
            await safe_reply(reply_target, msg)
        return

    # ── Fetch messages ──
    try:
        if seconds is not None:
            raw_messages = await _fetch_messages_by_time(channel, seconds)
        else:
            raw_messages = await _fetch_messages_default(channel)
    except discord.Forbidden:
        msg = "❌ I don't have permission to read message history in this channel."
        if is_slash:
            await reply_target.followup.send(msg, ephemeral=True)
        else:
            await safe_reply(reply_target, msg)
        return
    except Exception as e:
        print(f"[Summary] fetch error: {e}")
        msg = "❌ Something went wrong while fetching messages. Please try again."
        if is_slash:
            await reply_target.followup.send(msg, ephemeral=True)
        else:
            await safe_reply(reply_target, msg)
        return

    # Filter out bot messages from transcript (keep human convo only)
    human_messages = [m for m in raw_messages if not m.author.bot]

    if not human_messages:
        msg = f"📭 No messages found for the **{label}**."
        if is_slash:
            await reply_target.followup.send(msg, ephemeral=True)
        else:
            await safe_reply(reply_target, msg)
        return

    if len(human_messages) < 3:
        msg = f"📭 Only **{len(human_messages)}** message(s) found — not enough to summarise."
        if is_slash:
            await reply_target.followup.send(msg, ephemeral=True)
        else:
            await safe_reply(reply_target, msg)
        return

    # ── Build transcript ──
    transcript = _format_messages(human_messages)

    # Truncate if transcript is huge (keep last ~8000 chars to stay within token limits)
    if len(transcript) > 8000:
        transcript = "...[earlier messages truncated]...\n" + transcript[-8000:]

    # ── Call AI ──
    increment_ai_usage(invoker.id)
    summary = await _call_ai_summary(transcript, label)

    # ── Build embed ──
    embed = discord.Embed(
        title=f"📋 Chat Summary — {label.title()}",
        description=summary,
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(
        text=f"Based on {len(human_messages)} message(s) • Requested by {invoker.display_name}"
    )

    # ── Send ──
    if is_slash:
        # Slash: followup (we deferred earlier)
        if len(summary) > 4000:
            # Embed description limit — send as plain text split across messages
            await reply_target.followup.send(
                f"**📋 Chat Summary — {label.title()}**\n*(based on {len(human_messages)} messages)*"
            )
            # send_long_message expects a discord.Message; use channel.send for slash overflow
            chunks = [summary[i:i+1900] for i in range(0, len(summary), 1900)]
            for chunk in chunks:
                await channel.send(chunk)
        else:
            await reply_target.followup.send(embed=embed)
    else:
        # Prefix: reply with embed
        if len(summary) > 4000:
            await safe_reply(
                reply_target,
                f"**📋 Chat Summary — {label.title()}**\n*(based on {len(human_messages)} messages)*",
            )
            chunks = [summary[i:i+1900] for i in range(0, len(summary), 1900)]
            for chunk in chunks:
                await channel.send(chunk)
        else:
            await safe_reply(reply_target, embed=embed)


# ── Cog ───────────────────────────────────────────────────────────────────────

class Summary(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Prefix command: !summary [window] ────────────────────────────────────

    @commands.command(name="summary")
    async def prefix_summary(self, ctx: commands.Context, window: str = None):
        """
        Summarise recent chat messages.

        Examples:
          !summary         — last 50 messages
          !summary 30m     — last 30 minutes
          !summary 2h      — last 2 hours
          !summary 1d      — last 24 hours (max)
        """
        async with ctx.typing():
            await _do_summary(
                channel=ctx.channel,
                invoker=ctx.author,
                reply_target=ctx.message,
                window_arg=window,
                is_slash=False,
            )

    # ── Slash command: /summary [window] ─────────────────────────────────────

    @app_commands.command(
        name="summary",
        description="Summarise recent chat messages (e.g. last 1h, 30m, or last 50 messages)",
    )
    @app_commands.describe(
        window="Time window to summarise (e.g. 30m, 1h, 2h, 1d). Leave blank for last 50 messages."
    )
    async def slash_summary(self, interaction: discord.Interaction, window: str = None):
        await interaction.response.defer(thinking=True)
        await _do_summary(
            channel=interaction.channel,
            invoker=interaction.user,
            reply_target=interaction,
            window_arg=window,
            is_slash=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Summary(bot))