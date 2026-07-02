"""
Summary cog — !summary [time_window]

Summarises the user's conversation history with Jarvis for a given time window.

Usage:
  !summary            → summarise ALL stored history (in-memory session)
  !summary 30m        → last 30 minutes
  !summary 1h         → last 1 hour
  !summary 2h         → last 2 hours
  !summary 6h         → last 6 hours
  !summary 12h        → last 12 hours
  !summary 24h        → last 24 hours  (or !summary 1d)
  !summary 7d         → last 7 days (uses DB history if available)

Slash command: /summary [window]

How it works
────────────
• Every time a user sends a message *and* gets a reply, we record it in
  _timestamped_log[user_id] as {"role": ..., "content": ..., "ts": float}.
• On !summary we filter that log by the requested window, format the
  transcript, and ask the AI to produce a concise summary.
• If no timestamped log exists yet (bot just restarted, or no messages
  sent this session) we fall back to private_history (no time filter).
"""

import re
import time
import asyncio
from collections import defaultdict

import discord
from discord.ext import commands
from discord import app_commands

from cogs.http_session import safe_reply
from cogs.message_splitter import send_long_message

# ── Timestamped log ──────────────────────────────────────────────────────────
# { user_id: [ {"role": "user"|"assistant", "content": str, "ts": float} ] }
# Kept in-memory; bounded to MAX_LOG entries per user.

_timestamped_log: dict[int, list[dict]] = defaultdict(list)
MAX_LOG = 200   # keep at most 200 turns per user in memory


def record_turn(user_id: int, user_msg: str, assistant_msg: str) -> None:
    """Called from ai.py after a successful AI response."""
    now = time.time()
    log = _timestamped_log[user_id]
    log.append({"role": "user",      "content": user_msg,      "ts": now})
    log.append({"role": "assistant", "content": assistant_msg, "ts": now})
    # Trim to MAX_LOG most-recent entries
    if len(log) > MAX_LOG:
        del log[:-MAX_LOG]


def get_log_for_window(user_id: int, seconds: float | None) -> list[dict]:
    """Return log entries within the last `seconds`, or all if seconds is None."""
    log = _timestamped_log[user_id]
    if not log:
        return []
    if seconds is None:
        return list(log)
    cutoff = time.time() - seconds
    return [m for m in log if m["ts"] >= cutoff]


# ── Time-window parser ────────────────────────────────────────────────────────

_WINDOW_RE = re.compile(
    r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$",
    re.IGNORECASE,
)

def parse_window(raw: str | None) -> tuple[float | None, str]:
    """
    Parse a time-window string like '1h', '30m', '2h30m', '1d'.
    Returns (seconds: float | None, label: str).
    Returns (None, 'all') when raw is None/empty.
    Returns (None, '') on parse failure (caller should show error).
    """
    if not raw:
        return None, "all"

    raw = raw.strip().lower()
    m = _WINDOW_RE.match(raw)
    if not m or not any(m.groups()):
        return None, ""   # invalid

    days  = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    mins  = int(m.group(3) or 0)
    total = days * 86400 + hours * 3600 + mins * 60

    if total <= 0:
        return None, ""

    # Build human label
    parts = []
    if days:  parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if mins:  parts.append(f"{mins}m")
    return float(total), " ".join(parts)


# ── AI summary call ───────────────────────────────────────────────────────────

async def _call_ai_summary(transcript: str, window_label: str) -> str:
    """
    Ask the AI to summarise the transcript.
    Tries Groq first, falls back to Gemini — mirrors the pattern in ai.py
    without importing the whole cog (avoids circular imports).
    """
    # Import lazily to avoid circular-import issues at module load time
    from cogs.ai import _try_groq, _try_gemini, GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_MODEL_FLASH

    system_prompt = (
        "You are a helpful assistant summarising a conversation between a user and "
        "Jarvis (an AI Discord bot). Produce a clear, concise summary of the key "
        "topics discussed, questions asked, and answers given. Use bullet points "
        "for distinct topics. Keep it brief but informative. Do not add any "
        "commentary about the summarisation task itself."
    )

    user_msg = (
        f"Please summarise the following conversation"
        + (f" (last {window_label})" if window_label != "all" else "")
        + ":\n\n"
        + transcript
    )

    messages = [{"role": "user", "content": user_msg}]

    result = await _try_groq(messages, system_prompt)
    if result:
        return result

    for gemini_key in (GEMINI_API_KEY, GEMINI_API_KEY_2):
        if not gemini_key:
            continue
        result = await _try_gemini(gemini_key, GEMINI_MODEL_FLASH, messages, system_prompt)
        if result:
            return result

    return "⚠️ AI providers are still working on your request. Please wait a moment for a response."


# ── Transcript formatter ──────────────────────────────────────────────────────

def _format_transcript(entries: list[dict]) -> str:
    lines = []
    for e in entries:
        role = "You" if e["role"] == "user" else "Jarvis"
        lines.append(f"{role}: {e['content']}")
    return "\n".join(lines)


# ── Core handler ──────────────────────────────────────────────────────────────

async def _handle_summary(
    respond,          # coroutine: respond(text) or respond(embed=...)
    user_id: int,
    raw_window: str | None,
    username: str,
) -> None:
    seconds, label = parse_window(raw_window)

    if label == "":
        await respond(
            "⚠️ **Invalid time window.**\n"
            "Valid formats: `30m`, `1h`, `2h30m`, `1d`, `7d`\n"
            "Example: `!summary 1h`"
        )
        return

    entries = get_log_for_window(user_id, seconds)

    # Fallback: pull from private_history if no timestamped data yet
    if not entries and seconds is None:
        from cogs.ai import private_history
        ph = private_history.get(user_id, [])
        if ph:
            entries = [{"role": m["role"], "content": m["content"], "ts": 0.0} for m in ph]

    if not entries:
        window_str = f"the last {label}" if label != "all" else "your session"
        await respond(
            f"📭 No conversation found for {window_str}.\n"
            "Chat with Jarvis first, then use `!summary` to get a summary."
        )
        return

    transcript = _format_transcript(entries)

    # Truncate transcript if very long (keep last ~3000 chars to stay within token budget)
    MAX_TRANSCRIPT = 3000
    if len(transcript) > MAX_TRANSCRIPT:
        transcript = "…[earlier messages omitted]…\n" + transcript[-MAX_TRANSCRIPT:]

    # Thinking indicator embed
    thinking_embed = discord.Embed(
        description="⏳ Summarising your conversation…",
        color=discord.Color.blurple(),
    )
    msg_ref = await respond(embed=thinking_embed, _return=True)

    summary_text = await _call_ai_summary(transcript, label)

    # Build result embed
    title = "📋 Conversation Summary"
    if label != "all":
        title += f" — last {label}"

    embed = discord.Embed(
        title=title,
        description=summary_text,
        color=discord.Color.blurple(),
    )
    embed.set_footer(
        text=f"{len(entries) // 2} exchange(s) summarised  •  {username}"
    )

    if msg_ref and hasattr(msg_ref, "edit"):
        try:
            await msg_ref.edit(embed=embed, content=None)
            return
        except Exception:
            pass

    await respond(embed=embed)


# ── Cog ───────────────────────────────────────────────────────────────────────

class Summary(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── !summary ──────────────────────────────────────────────────────────────

    @commands.command(name="summary")
    async def prefix_summary(self, ctx: commands.Context, window: str = None):
        """
        Summarise your chat with Jarvis.
        Usage: !summary [window]
        Examples: !summary  |  !summary 1h  |  !summary 30m  |  !summary 2h  |  !summary 1d
        """
        sent: list = []

        async def respond(text=None, embed=None, _return=False):
            if text:
                m = await safe_reply(ctx.message, text)
            else:
                m = await safe_reply(ctx.message, embed=embed)
            sent.append(m)
            if _return:
                return m

        await _handle_summary(respond, ctx.author.id, window, str(ctx.author))

    # ── /summary ──────────────────────────────────────────────────────────────

    @app_commands.command(name="summary", description="Summarise your conversation with Jarvis")
    @app_commands.describe(window="Time window: 30m, 1h, 2h, 6h, 12h, 24h, 1d, 7d — leave blank for full session")
    async def slash_summary(self, interaction: discord.Interaction, window: str = None):
        await interaction.response.defer(thinking=True)

        result_embed: list = []

        async def respond(text=None, embed=None, _return=False):
            if embed:
                result_embed.append(embed)
            if _return:
                return None  # can't return the deferred message easily

        await _handle_summary(respond, interaction.user.id, window, str(interaction.user))

        if result_embed:
            await interaction.followup.send(embed=result_embed[-1])
        else:
            await interaction.followup.send("Something went wrong generating the summary.")

    # ── !clearsummary ─────────────────────────────────────────────────────────

    @commands.command(name="clearsummary")
    async def prefix_clearsummary(self, ctx: commands.Context):
        """Clear your summary log (the timestamped conversation history used by !summary)."""
        _timestamped_log.pop(ctx.author.id, None)
        await safe_reply(ctx.message, "🧹 Your summary log has been cleared!")

    # ── /clearsummary ─────────────────────────────────────────────────────────

    @app_commands.command(name="clearsummary", description="Clear your summary conversation log")
    async def slash_clearsummary(self, interaction: discord.Interaction):
        _timestamped_log.pop(interaction.user.id, None)
        await interaction.response.send_message("🧹 Your summary log has been cleared!", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Summary(bot))