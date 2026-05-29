"""
Shared aiohttp ClientSession for the entire bot.

Creating a new ClientSession per request (as the original code did) is expensive:
  - Each session opens/closes a TCP connection pool
  - Each session allocates SSL contexts, connection limits, etc.
  - At scale this wastes memory and adds latency

This module owns one session that is created at startup and closed on shutdown.
All cogs import `get_session()` instead of constructing their own.
"""
import aiohttp
import discord

_session: aiohttp.ClientSession | None = None


def get_session() -> aiohttp.ClientSession:
    """Return the shared session. Must call create_session() first."""
    if _session is None or _session.closed:
        raise RuntimeError("HTTP session not initialised. Call http_session.create_session() at startup.")
    return _session


async def create_session():
    """Create the shared session. Call once from main() before starting the bot."""
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(
            limit=20,           # max simultaneous connections across the whole bot
            limit_per_host=5,   # max per individual host (Discord, Groq, Gemini…)
            ttl_dns_cache=300,  # cache DNS for 5 min — avoids repeated lookups
            ssl=True,
        )
        _session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=15),
        )


async def close_session():
    """Gracefully close the shared session. Call on bot shutdown."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None

async def safe_reply(message: discord.Message, *args, **kwargs) -> discord.Message | None:
    """Reply to a message, silently ignoring Forbidden (public bot — not our problem)."""
    try:
        return await message.reply(*args, **kwargs)
    except discord.Forbidden:
        return None


async def safe_send(channel, *args, **kwargs) -> discord.Message | None:
    """Send to a channel, silently ignoring Forbidden."""
    try:
        return await channel.send(*args, **kwargs)
    except discord.Forbidden:
        return None