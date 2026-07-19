"""
Image Search cog — powered by Serper.dev Google Image Search API.

Usage : !image <query> [--index <1-10>]
Slash : /image query:[...] index:[1-10]

• Replies to the user who triggered the command.
• Supports any number of Serper API keys in .env using the pattern:
    SERPER_API_KEY=key1
    SERPER_API_KEY2=key2
    SERPER_API_KEY3=key3
    SERPER_API_KEY4=key4
    ... and so on — just keep adding, the bot picks them all up automatically.
• Keys are round-robin rotated across requests.
• Shows a rich embed with the image, source link, and search metadata.
"""

from __future__ import annotations

import itertools
import os
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from cogs.http_session import get_session
from cogs.economy import SpendCreditsView, JC_EMOJI, JC_NAME, get_credits
from cogs.state import record_image_search

load_dotenv()

# ── API key pool ──────────────────────────────────────────────────────────────
# Add keys in your .env like this — no limit on how many:
#
#   SERPER_API_KEY=abc123
#   SERPER_API_KEY2=def456
#   SERPER_API_KEY3=ghi789
#   SERPER_API_KEY4=...
#
# The loader grabs SERPER_API_KEY first, then SERPER_API_KEY2, SERPER_API_KEY3,
# and keeps going until it finds a gap. Just keep appending new ones and restart.

def _load_keys() -> list[str]:
    keys: list[str] = []

    # Always check bare SERPER_API_KEY first
    first = os.getenv("SERPER_API_KEY", "").strip()
    if first:
        keys.append(first)

    # Then SERPER_API_KEY2, SERPER_API_KEY3, ... until a gap
    n = 2
    while True:
        val = os.getenv(f"SERPER_API_KEY{n}", "").strip()
        if not val:
            break
        keys.append(val)
        n += 1

    if keys:
        print(f"[Serper] Loaded {len(keys)} API key(s).")
    else:
        print("[Serper] ⚠️  No SERPER_API_KEY found in .env — !image will not work.")

    return keys

_KEY_POOL: list[str] = _load_keys()

# Infinite round-robin iterator over the key pool
_key_cycle = itertools.cycle(_KEY_POOL) if _KEY_POOL else None

_SERPER_STATS: dict[str, object] = {
    "enabled": bool(_KEY_POOL),
    "configured": len(_KEY_POOL),
    "active": len(_KEY_POOL),
    "requests": 0,
    "successes": 0,
    "failures": 0,
    "total_tokens": 0,
    "total_latency_ms": 0.0,
    "keys": [],
}


def _record_serper_result(*, success: bool, latency_ms: float, tokens: int = 0, error: str | None = None) -> None:
    _SERPER_STATS["requests"] = int(_SERPER_STATS.get("requests", 0)) + 1
    if success:
        _SERPER_STATS["successes"] = int(_SERPER_STATS.get("successes", 0)) + 1
    else:
        _SERPER_STATS["failures"] = int(_SERPER_STATS.get("failures", 0)) + 1
    _SERPER_STATS["total_latency_ms"] = float(_SERPER_STATS.get("total_latency_ms", 0.0)) + latency_ms
    _SERPER_STATS["total_tokens"] = int(_SERPER_STATS.get("total_tokens", 0)) + tokens
    _SERPER_STATS["active"] = len(_KEY_POOL)
    if error:
        _SERPER_STATS["last_error"] = error[:120]


def get_serper_status_snapshot() -> dict:
    return {
        key: value for key, value in _SERPER_STATS.items()
    }


def _next_key() -> str | None:
    """Return the next API key from the pool (round-robin), or None if unconfigured."""
    if _key_cycle is None:
        return None
    return next(_key_cycle)


# ── Serper API ────────────────────────────────────────────────────────────────

SERPER_URL     = "https://google.serper.dev/images"
SERPER_TIMEOUT = aiohttp.ClientTimeout(total=10)
MAX_RESULTS    = 10   # Serper returns up to 10 image results by default

IMAGE_SEARCH_COOLDOWN = 10 * 60  # 10 minutes in seconds
IMAGE_BYPASS_COST     = 50       # JC cost to skip the cooldown

# ── Channels exempt from the cooldown ─────────────────────────────────────────
# Set in .env like:
#   IMAGE_COOLDOWN_EXEMPT_CHANNELS=123456789012345678,987654321098765432
# Any channel ID listed here will have NO cooldown on !image / /image.

def _load_exempt_channels() -> set[int]:
    raw = os.getenv("IMAGE_COOLDOWN_EXEMPT_CHANNELS", "").strip()
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    if ids:
        print(f"[ImageSearch] Cooldown exempt channels: {ids}")
    return ids

IMAGE_COOLDOWN_EXEMPT_CHANNELS: set[int] = _load_exempt_channels()

def _is_cooldown_exempt(channel) -> bool:
    channel_id = getattr(channel, "id", None)
    return channel_id is not None and channel_id in IMAGE_COOLDOWN_EXEMPT_CHANNELS

# ── Per-user rate limit ───────────────────────────────────────────────────────

_last_searched: dict[int, float] = {}

def _check_cooldown(user_id: int) -> float:
    """Returns 0 if user may search now, or seconds remaining if on cooldown."""
    import time
    elapsed = time.time() - _last_searched.get(user_id, 0)
    return max(0.0, IMAGE_SEARCH_COOLDOWN - elapsed)

def _mark_searched(user_id: int) -> None:
    import time
    _last_searched[user_id] = time.time()
    record_image_search(user_id)

def _fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s" if m else f"{s}s"

def _cooldown_embed(remaining: float, user: discord.User | discord.Member) -> discord.Embed:
    balance = get_credits(user.id)
    can_bypass = balance >= IMAGE_BYPASS_COST
    bypass_hint = (
        f"\n\n{JC_EMOJI} **Bypass for {IMAGE_BYPASS_COST} {JC_NAME}s?**\n"
        f"Your balance: **{balance}** {JC_NAME}s — press **Yes** below!"
        if can_bypass else
        f"\n\n{JC_EMOJI} Need **{IMAGE_BYPASS_COST} {JC_NAME}s** to bypass. You have **{balance}**."
    )
    embed = discord.Embed(
        title="⏳ Cooldown Active",
        description=(
            f"You can only search images **once every 10 minutes**.\n\n"
            f"**Time remaining:** `{_fmt_time(remaining)}`"
            + bypass_hint
        ),
        color=discord.Color.orange(),
    )
    embed.set_footer(
        text=f"Requested by {user.display_name}",
        icon_url=user.display_avatar.url,
    )
    return embed


def _is_nsfw_channel(channel) -> bool:
    """Return True if the channel is marked as NSFW (or is a DM)."""
    if isinstance(channel, discord.DMChannel):
        return False  # DMs are not NSFW by default
    return getattr(channel, "nsfw", False)


async def _search_images(query: str, safe: bool = True) -> list[dict] | None:
    """
    Call Serper.dev image search and return a list of result dicts, or None on failure.
    Each result dict has at least: title, imageUrl, link, source.
    Pass safe=True to enable safe search (for non-NSFW channels).
    """
    key = _next_key()
    if not key:
        return None

    session = get_session()
    headers = {
        "X-API-KEY":    key,
        "Content-Type": "application/json",
    }
    payload = {"q": query, "num": MAX_RESULTS, "safe": "active" if safe else "off"}

    start = time.perf_counter()
    try:
        async with session.post(
            SERPER_URL,
            json=payload,
            headers=headers,
            timeout=SERPER_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                latency_ms = (time.perf_counter() - start) * 1000
                _record_serper_result(success=False, latency_ms=latency_ms, error=f"http-{resp.status}")
                print(f"[Serper] HTTP {resp.status} for query: {query!r}")
                return None
            data = await resp.json()
            latency_ms = (time.perf_counter() - start) * 1000
            results = data.get("images") or []
            _record_serper_result(success=True, latency_ms=latency_ms, tokens=max(1, len(query) // 4 + len(str(results)) // 4))
            return results
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        _record_serper_result(success=False, latency_ms=latency_ms, error=str(exc)[:120])
        print(f"[Serper] Error: {exc}")
        return None


# ── Embed builder ─────────────────────────────────────────────────────────────

def _build_embed(
    query:   str,
    result:  dict,
    index:   int,
    total:   int,
    user:    discord.User | discord.Member,
) -> discord.Embed:
    title     = result.get("title") or "Image Result"
    image_url = result.get("imageUrl", "")
    page_link = result.get("link", "")
    source    = result.get("source", "")

    embed = discord.Embed(
        title       = f"🔍 {title[:200]}",
        url         = page_link or None,
        color       = discord.Color.blurple(),
        description = f"**Source:** {source}" if source else "",
    )
    embed.set_image(url=image_url)
    embed.set_footer(
        text=(
            f"Result {index} of {total}  •  Query: {query[:80]}"
            f"  •  Requested by {user.display_name}"
            f"  •  Powered by Serper.dev"
        ),
        icon_url=user.display_avatar.url,
    )
    return embed


def _no_results_embed(query: str, user: discord.User | discord.Member) -> discord.Embed:
    return discord.Embed(
        title       = "🔍 No Results Found",
        description = f"No images found for **{query[:200]}**.",
        color       = discord.Color.red(),
    ).set_footer(
        text     = f"Requested by {user.display_name}",
        icon_url = user.display_avatar.url,
    )


def _error_embed(reason: str, user: discord.User | discord.Member) -> discord.Embed:
    return discord.Embed(
        title       = "❌ Image Search Failed",
        description = reason,
        color       = discord.Color.red(),
    ).set_footer(
        text     = f"Requested by {user.display_name}",
        icon_url = user.display_avatar.url,
    )


# ── Cog ───────────────────────────────────────────────────────────────────────

class ImageSearch(commands.Cog):
    """Search Google Images via Serper.dev and reply with the result."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Shared logic ──────────────────────────────────────────────────────────

    async def _handle(
        self,
        query:    str,
        index:    int,
        user:     discord.User | discord.Member,
        reply_fn,
        error_fn,
        channel=None,
    ) -> None:
        # Determine if safe search should be applied
        nsfw = _is_nsfw_channel(channel) if channel is not None else False
        safe_search = not nsfw

        if not _KEY_POOL:
            await error_fn(
                embed=_error_embed(
                    "No Serper API key configured.\n"
                    "Add `SERPER_API_KEY=your_key` to your `.env` file.",
                    user,
                )
            )
            return

        remaining = 0.0 if _is_cooldown_exempt(channel) else _check_cooldown(user.id)
        if remaining > 0:
            # Only offer bypass button if user can afford it
            if get_credits(user.id) >= IMAGE_BYPASS_COST:
                bypass_msg: discord.Message | None = None

                async def on_confirm(interaction: discord.Interaction, view: SpendCreditsView):
                    # JC already deducted by SpendCreditsView — run the search now
                    await interaction.response.edit_message(
                        content="🔍 Searching…", embed=None, view=None
                    )
                    results = await _search_images(query, safe=safe_search)
                    if not results:
                        await interaction.edit_original_response(
                            content=None,
                            embed=_no_results_embed(query, user),
                        )
                        return
                    idx    = max(1, min(index, len(results)))
                    embed  = _build_embed(query, results[idx - 1], idx, len(results), user)
                    try:
                        from cogs.system_breach import bump_daemon_quest
                        bump_daemon_quest(user.id, "image_seeker", 1)
                    except Exception:
                        pass
                    _mark_searched(user.id)
                    await interaction.edit_original_response(content=None, embed=embed)

                async def on_decline(interaction: discord.Interaction, view: SpendCreditsView, reason: str):
                    msg = "❌ Bypass cancelled." if reason == "declined" else f"❌ Not enough {JC_NAME}s."
                    await interaction.response.edit_message(content=msg, embed=None, view=None)

                async def on_timeout(view: SpendCreditsView):
                    if bypass_msg:
                        try:
                            await bypass_msg.edit(content="⏰ Bypass prompt expired.", embed=None, view=None)
                        except discord.HTTPException:
                            pass

                view = SpendCreditsView(
                    user_id=user.id,
                    cost=IMAGE_BYPASS_COST,
                    on_confirm=on_confirm,
                    on_decline=on_decline,
                    on_timeout_action=on_timeout,
                    timeout=30,
                )
                bypass_msg = await error_fn(embed=_cooldown_embed(remaining, user), view=view)
                if bypass_msg:
                    view.message = bypass_msg
            else:
                await error_fn(embed=_cooldown_embed(remaining, user))
            return

        results = await _search_images(query, safe=safe_search)
        
        if results is None:
            await error_fn(
                embed=_error_embed("Serper.dev didn't respond. Please try again later.", user)
            )
            return

        if not results:
            await error_fn(embed=_no_results_embed(query, user))
            return

        try:
            from cogs.system_breach import bump_daemon_quest
            bump_daemon_quest(user.id, "image_seeker", 1)
        except Exception:
            pass
                
        index  = max(1, min(index, len(results)))
        result = results[index - 1]
        embed  = _build_embed(query, result, index, len(results), user)
        if not _is_cooldown_exempt(channel):
            _mark_searched(user.id)
        else:
            record_image_search(user.id)  # still log the search for stats, just skip the cooldown timer
        await reply_fn(embed=embed)

    async def _send_image(self, message: discord.Message, query: str, index: int = 1) -> None:
        """Called from ai.py intent intercept — searches and replies to a discord.Message."""
        await self._handle(
            query    = query,
            index    = index,
            user     = message.author,
            reply_fn = lambda **kw: message.reply(**kw),
            error_fn = lambda **kw: message.reply(**kw),
            channel  = message.channel,
        )

    # ── Prefix command: !image <query> [--index <n>] ──────────────────────────

    @commands.command(name="image", aliases=["img", "imgsearch"])
    async def prefix_image(self, ctx: commands.Context, *, args: str = ""):
        """
        Search Google Images and reply with a result.

        Usage:
          !image <query>
          !image <query> --index <1-10>

        Examples:
          !image golden retriever puppy
          !image Mount Everest --index 3
        """
        if not args.strip():
            await ctx.reply(
                "**Usage:** `!image <query> [--index <1-10>]`\n"
                "**Example:** `!image sunset over mountains --index 2`"
            )
            return

        index = 1
        if "--index" in args:
            try:
                idx_pos = args.index("--index")
                rest    = args[idx_pos + len("--index"):].strip()
                token   = rest.split()[0]
                index   = int(token)
                args    = args[:idx_pos].strip()
            except (ValueError, IndexError):
                await ctx.reply("❌ `--index` must be a number between 1 and 10.")
                return

        query = args.strip()
        if not query:
            await ctx.reply("❌ Please provide a search query.")
            return

        async with ctx.typing():
            await self._handle(
                query    = query,
                index    = index,
                user     = ctx.author,
                reply_fn = lambda **kw: ctx.reply(**kw),
                error_fn = lambda **kw: ctx.reply(**kw),
                channel  = ctx.channel,
            )

    # ── Slash command: /image ─────────────────────────────────────────────────

    @app_commands.command(name="image", description="Search Google Images and get a result 🔍")
    @app_commands.describe(
        query = "What do you want to search for?",
        index = "Which result to show (1–10, default: 1)",
    )
    async def slash_image(
        self,
        interaction: discord.Interaction,
        query: str,
        index: app_commands.Range[int, 1, 10] = 1,
    ) -> None:
        await interaction.response.defer(thinking=True)
        await self._handle(
            query    = query,
            index    = index,
            user     = interaction.user,
            reply_fn = lambda **kw: interaction.followup.send(**kw),
            error_fn = lambda **kw: interaction.followup.send(**kw),
            channel  = interaction.channel,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ImageSearch(bot))