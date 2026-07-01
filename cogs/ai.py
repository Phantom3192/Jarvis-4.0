"""
AI cog — handles all chat interactions with Jarvis.

CHANGES vs previous version:
- Per-user model preference stored in _user_model (in-memory dict).
  Users pick via /setmodel or !setmodel. Preference is reset on bot restart
  (intentionally lightweight — no DB needed for a UI preference).
- generate_ai_response now reads the user's preferred model and routes
  to that provider first, falling back to others if it fails.
- MODELS dict is the single source of truth for available choices.
- !ping / !uptime moved to system.py — not duplicated here.
"""
import discord
from discord.ext import commands
from discord import app_commands
import os
import re
import asyncio
import time
from collections import defaultdict
import groq
import importlib
import base64
from cogs.state import (
    is_bot_banned, is_new_user, mark_seen, record_message, get_guild_prompt,
    is_ai_rate_limited, increment_ai_usage, get_ai_usage, get_ai_limit,
    DAILY_AI_LIMIT, WARN_AT, check_burst_and_maybe_timeout, check_cooldown, get_setting,
    get_preferred_name, set_preferred_name, clear_preferred_name,
    get_reminders, add_reminder, delete_reminder, pop_due_reminders,
    set_dnd, is_dnd,
    get_credits, reset_ai_usage, earn_chat_credits, claim_daily_credits,
    grant_onboarding_bonus,
)
from cogs.economy import (
    SpendCreditsView, AI_LIMIT_RESET_COST, JC_EMOJI, JC_NAME,
    DAILY_CHECKIN_REWARD, ONBOARDING_BONUS, AI_CHAT_REWARD, AI_CHAT_REWARD_DAILY_CAP,
)
from cogs.message_splitter import send_long_message, edit_or_send_long_message
from cogs.http_session import get_session, safe_reply, safe_send
from cogs.history import add_message as _db_add_message, get_history as _db_get_history, clear_history as _db_clear_history
from cogs.memory import extract_facts, save_facts, get_facts, build_memory_prompt, forget_facts, get_facts_count
from cogs import music as _music_module

# ── API keys & models ─────────────────────────────────────────────────────────

# Groq — dynamic multi-key pool.
# Add keys to .env as GROQ_API_KEY, GROQ_API_KEY_1, GROQ_API_KEY_2, GROQ_API_KEY_3 ...
# The bot picks them all up automatically — no code changes needed.
def _load_groq_keys() -> list[str]:
    keys = []
    # Accept bare GROQ_API_KEY for backward compat
    base = os.getenv("GROQ_API_KEY", "").strip()
    if base:
        keys.append(base)
    # Scan GROQ_API_KEY_1, GROQ_API_KEY_2, ... up to 20
    for i in range(1, 21):
        k = os.getenv(f"GROQ_API_KEY_{i}", "").strip()
        if k and k not in keys:
            keys.append(k)
    return keys

GROQ_API_KEYS: list[str] = _load_groq_keys()

GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY")
GEMINI_API_KEY_2 = os.getenv("GEMINI_API_KEY_2")

GROQ_MODEL_TEXT    = "openai/gpt-oss-120b"
GROQ_MODEL_VISION  = "meta-llama/llama-4-scout-17b-16e-instruct"
GEMINI_MODEL_FLASH = "gemini-2.0-flash"
GEMINI_MODEL_LITE  = "gemini-2.0-flash-lite"

HISTORY_LIMIT    = 2    # Keep the prompt very short for faster responses
MAX_TOKENS       = 180  # Shorter responses are much faster to generate
PROVIDER_TIMEOUT = 2.0  # Fast failover keeps the user experience snappy
GROQ_RETRIES     = 1    # One quick retry helps avoid false timeouts

# ── Available models for /setmodel ───────────────────────────────────────────
# key        → internal identifier stored per user
# "label"    → shown in Discord UI
# "desc"     → shown in the info embed
# "supports_vision" → whether this model can handle image attachments

MODELS: dict[str, dict] = {
    "auto": {
        "label":           "🔄 Auto (default)",
        "desc":            "Tries Groq GPT-OSS first, falls back to Gemini automatically.",
        "supports_vision": True,
    },
    "groq": {
        "label":           "⚡ Groq — GPT-OSS 120B",
        "desc":            "Fast. Best for quick questions and conversation.",
        "supports_vision": False,
    },
    "gemini-flash": {
        "label":           "✨ Gemini 2.0 Flash",
        "desc":            "Google's fast multimodal model. Supports images.",
        "supports_vision": True,
    },
    "gemini-lite": {
        "label":           "🪶 Gemini 2.0 Flash-Lite",
        "desc":            "Lightest model. Best for simple tasks when speed matters.",
        "supports_vision": True,
    },
}

MODEL_CHOICES = [
    app_commands.Choice(name=v["label"], value=k)
    for k, v in MODELS.items()
]

# ── Per-user model preference (in-memory) ─────────────────────────────────────
# Resets on restart — intentional, no DB overhead needed for a UI preference.
_user_model: dict[int, str] = {}

def get_user_model(user_id: int) -> str:
    return _user_model.get(user_id, "auto")

def set_user_model(user_id: int, model_key: str) -> None:
    _user_model[user_id] = model_key

# ── System prompt ─────────────────────────────────────────────────────────────

DEFAULT_SYSTEM_PROMPT = (
    "You are Jarvis, a sharp, efficient, and slightly witty AI assistant built for Discord by Phantom. "
    "If someone asks who made you or who your creator is, say Phantom — but never volunteer or repeat this unprompted. "
    "Keep responses concise and helpful. Do not start sentences with someone's name. Avoid unnecessary filler. "
    "CRITICAL: Never fabricate or invent real-time bot data. "
    "If someone asks for your ping/latency, uptime, usage stats, or any live bot metric, "
    "say you can't retrieve that right now and suggest they use the relevant command (e.g. !ping, !uptime). "
    "Never make up a number for ping or any other live value."
)

SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES       = 20 * 1024 * 1024
AI_UNAVAILABLE_MSG    = "⏳ The AI provider is taking longer than expected. Please try again in a moment."

DAILY_LIMIT_MSG = (
    "⏳ **You've reached your daily Jarvis AI limit!**\n\n"
    "You've used all **{limit}** of your free AI messages for today. "
    "Your limit resets at **midnight UTC** — come back then and I'll be ready.\n\n"
    "In the meantime, here's what you can still do:\n"
    "🎮 **`!trivia`** or **`/trivia`** — test your knowledge with a trivia question\n"
    "🪢 **`!hangman`** or **`/hangman`** — play a round of hangman\n"
    "🤔 **`/wyr`** — Would You Rather? fun for the whole server\n"
    "🫣 **`/truth`** or **`/dare`** — classic truth or dare\n"
    "🤯 **`/funfact`** — get a random fun fact\n"
    "📊 **`/stats`** — check your Jarvis usage stats\n"
    "💡 **`!feedback`** — share ideas or suggestions with the developer\n\n"
    "See you tomorrow! 👋"
)

WARN_LIMIT_MSG = (
    "⚠️ **Heads up** — you've used **{count}/{limit}** AI messages today. "
    "You have **{remaining}** left before your limit resets at midnight UTC."
)

# ── Compiled regex ────────────────────────────────────────────────────────────

_MENTION_RE = re.compile(r"<@!?\d+>")
_JARVIS_RE  = re.compile(r"\bjarvis\b", re.IGNORECASE)

# ── Groq client pool ──────────────────────────────────────────────────────────
# One AsyncGroq client per key. Keys are rotated round-robin and backed off
# individually on rate limit / repeated timeout — same pattern as Gemini.

_groq_clients: list[tuple[str, groq.AsyncGroq]] = [
    (key, groq.AsyncGroq(api_key=key)) for key in GROQ_API_KEYS
]

_GROQ_BACKOFF   = 30  # seconds a Groq key is skipped after rate-limit / repeated timeout
_groq_backoff_until: dict[str, float] = {}
_groq_rr_index  = 0   # round-robin cursor

if _groq_clients:
    print(f"Groq pool: {len(_groq_clients)} key(s) loaded")
else:
    print("No GROQ_API_KEY* found - Groq disabled")

def _groq_is_backed_off(api_key: str) -> bool:
    return time.time() < _groq_backoff_until.get(api_key, 0)

def _groq_set_backoff(api_key: str) -> None:
    _groq_backoff_until[api_key] = time.time() + _GROQ_BACKOFF
    print(f"[Groq] Key ...{api_key[-6:]} backed off for {_GROQ_BACKOFF}s")

def _next_groq_client() -> tuple[str, groq.AsyncGroq] | None:
    """Return the next available (non-backed-off) Groq client, round-robin."""
    global _groq_rr_index
    if not _groq_clients:
        return None
    n = len(_groq_clients)
    for _ in range(n):
        key, client = _groq_clients[_groq_rr_index % n]
        _groq_rr_index = (_groq_rr_index + 1) % n
        if not _groq_is_backed_off(key):
            return key, client
    return None  # all keys backed off

# ── Gemini package detection ───────────────────────────────────────────────
_GENAI_PACKAGE: str | None = None
genai = None
for _name in ("google.genai", "google.generativeai"):
    try:
        module = importlib.import_module(_name)
        if hasattr(module, "GenerativeModel"):
            genai = module
            _GENAI_PACKAGE = _name
            break
    except ImportError:
        continue

if genai is None:
    print("Warning: No supported Google GenAI package available. Gemini will be disabled.")
else:
    print(f"Using Google GenAI package: {_GENAI_PACKAGE}")

_gemini_clients: dict[str, object] = {}

# ── Gemini circuit breaker ─────────────────────────────────────────────────────
# When a key hits 429, skip it for _GEMINI_BACKOFF seconds so Groq responds fast.
_GEMINI_BACKOFF = 20  # seconds — reduced from 60s; keys recover faster, Lite model is separate quota
_gemini_backoff_until: dict[str, float] = {}

def _gemini_is_backed_off(api_key: str) -> bool:
    return time.time() < _gemini_backoff_until.get(api_key, 0)

def _gemini_set_backoff(api_key: str) -> None:
    _gemini_backoff_until[api_key] = time.time() + _GEMINI_BACKOFF

# ── History stores ────────────────────────────────────────────────────────────

private_history: dict[int, list[dict]] = defaultdict(list)
group_history:   dict[int, list[dict]] = defaultdict(list)
active_groups:   dict[int, set[int]]   = {}
merge_history:   dict[int, list[dict]] = defaultdict(list)  # keyed by root message ID
_reply_thread_root: dict[int, int]     = {}  # message_id → root_message_id
_system_prompt_cache: dict[tuple, str] = {}  # Cache system prompts to avoid reconstruction
_response_cache: dict[tuple, tuple[str, float]] = {}  # Cache responses for 60s (message_hash, timestamp, response)
_response_cache_writes: list[int] = [0]  # mutable counter for sweep throttling


def _is_in_group(user_id: int, channel_id: int) -> bool:
    return channel_id in active_groups and user_id in active_groups[channel_id]

def _get_history(user_id: int, channel_id: int, thread_root_id: int | None = None) -> list[dict]:
    if thread_root_id is not None:
        return merge_history[thread_root_id]
    return group_history[channel_id] if _is_in_group(user_id, channel_id) else private_history[user_id]

def _trim(history: list[dict]) -> None:
    if len(history) > HISTORY_LIMIT:
        del history[:-HISTORY_LIMIT]

def _background_record(user_id: int, user_message: str, reply: str) -> None:
    """Fire-and-forget task to record message to DB without blocking response."""
    try:
        record_message(user_id, user_message, reply)
    except Exception as e:
        print(f"[Background Task] DB record error: {e}")
    # Also log the turn with a timestamp so !summary can filter by time window
    try:
        from cogs.summary import record_turn
        record_turn(user_id, user_message, reply)
    except Exception as e:
        print(f"[Background Task] summary record_turn error: {e}")

def clear_history(user_id: int, channel_id: int | None = None) -> None:
    if channel_id and _is_in_group(user_id, channel_id):
        group_history[channel_id].clear()
    else:
        private_history[user_id].clear()
        # Also wipe the persisted DB history so it doesn't reload on next restart
        asyncio.create_task(_db_clear_history(user_id))

_MAX_REPLY_ROOT_CACHE = 10_000

# Users currently being processed — prevents double-processing while bot is responding
_processing_users: set[int] = set()

def register_reply_root(message_id: int, root_id: int) -> None:
    """Map a message ID to the root of its reply chain for merge history lookup."""
    if len(_reply_thread_root) >= _MAX_REPLY_ROOT_CACHE:
        # Evict oldest ~10% of entries to keep memory bounded
        evict = list(_reply_thread_root.keys())[:_MAX_REPLY_ROOT_CACHE // 10]
        for k in evict:
            _reply_thread_root.pop(k, None)
    _reply_thread_root[message_id] = root_id

def get_reply_root(message_id: int) -> int | None:
    return _reply_thread_root.get(message_id)

def start_group(channel_id: int, user_ids: list[int]) -> None:
    active_groups[channel_id] = set(user_ids)
    group_history[channel_id].clear()

def end_group(channel_id: int) -> None:
    active_groups.pop(channel_id, None)
    group_history[channel_id].clear()

def get_group_members(channel_id: int) -> set[int] | None:
    return active_groups.get(channel_id)



# ── Webhook logger ────────────────────────────────────────────────────────────

async def _log_new_user(user: discord.User | discord.Member) -> None:
    webhook_url = os.getenv("LOG_WEBHOOK_URL", "")
    if not webhook_url:
        return
    try:
        session = get_session()
        webhook = discord.Webhook.from_url(webhook_url, session=session)
        embed   = discord.Embed(
            title="✨ New Jarvis User",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Username",    value=str(user),      inline=True)
        embed.add_field(name="User ID",     value=f"`{user.id}`", inline=True)
        embed.add_field(name="Account Age", value=discord.utils.format_dt(user.created_at, style="R"), inline=True)
        embed.add_field(
            name="Quick Actions",
            value=f"`!global-ban {user.id} reason`\n`!global-unban {user.id}`",
            inline=False,
        )
        embed.set_footer(text="First ever interaction with Jarvis")
        await webhook.send(embed=embed, username="Jarvis Logs")
    except Exception as e:
        print(f"❌ Webhook error _log_new_user: {e}")


async def _log_guild(guild: discord.Guild, *, joined: bool, backfill: bool = False, index: int = 0, total: int = 0) -> None:
    """Send a guild join/leave embed to SERVER_LOG_WEBHOOK_URL."""
    webhook_url = os.getenv("SERVER_LOG_WEBHOOK_URL", "")
    if not webhook_url:
        return
    try:
        if backfill:
            title = "📋 Existing Server (backfill)"
            color = discord.Color.blurple()
        elif joined:
            title = "➕ Bot Added to Server"
            color = discord.Color.green()
        else:
            title = "➖ Bot Removed from Server"
            color = discord.Color.red()

        embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
        embed.add_field(name="Server Name",    value=guild.name,              inline=True)
        embed.add_field(name="Server ID",      value=f"`{guild.id}`",         inline=True)
        embed.add_field(name="Member Count",   value=str(guild.member_count), inline=True)
        embed.add_field(name="Owner",          value=f"<@{guild.owner_id}>" if guild.owner_id else "Unknown", inline=True)
        created_ts = int(guild.created_at.timestamp())
        embed.add_field(name="Server Created", value=f"<t:{created_ts}:R>",   inline=True)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        footer = f"Backfill {index}/{total}" if backfill else ("Bot now in servers" if joined else "Bot removed")
        embed.set_footer(text=footer)

        session = get_session()
        webhook = discord.Webhook.from_url(webhook_url, session=session)
        await webhook.send(embed=embed, username="Jarvis Server Logs")
    except Exception as e:
        print(f"❌ Webhook error _log_guild: {e}")


async def _log_member(member: discord.Member, *, joined: bool) -> None:
    """Send a member join/leave embed to SERVER_LOG_WEBHOOK_URL."""
    webhook_url = os.getenv("SERVER_LOG_WEBHOOK_URL", "")
    if not webhook_url:
        return
    try:
        title = "📥 New Member Joined" if joined else "📤 Member Left"
        color = discord.Color.green() if joined else discord.Color.red()

        embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="User",           value=f"{member} (`{member.id}`)", inline=False)
        embed.add_field(name="Server",         value=member.guild.name,           inline=True)
        embed.add_field(name="Server ID",      value=f"`{member.guild.id}`",      inline=True)
        embed.add_field(name="Members Now",    value=str(member.guild.member_count), inline=True)
        created_ts = int(member.created_at.timestamp())
        embed.add_field(name="Account Age",    value=f"<t:{created_ts}:R>",       inline=True)
        if joined and member.joined_at:
            joined_ts = int(member.joined_at.timestamp())
            embed.add_field(name="Joined At",  value=f"<t:{joined_ts}:R>",        inline=True)
        embed.set_footer(text=f"Server: {member.guild.name}")

        session = get_session()
        webhook = discord.Webhook.from_url(webhook_url, session=session)
        await webhook.send(embed=embed, username="Jarvis Server Logs")
    except Exception as e:
        print(f"❌ Webhook error _log_member: {e}")


# ── Image fetch ───────────────────────────────────────────────────────────────

async def _fetch_image(attachment: discord.Attachment) -> tuple[str, str] | None:
    content_type = (attachment.content_type or "").split(";")[0].strip().lower()
    if content_type not in SUPPORTED_IMAGE_TYPES or attachment.size > MAX_IMAGE_BYTES:
        return None
    try:
        session = get_session()
        async with session.get(attachment.url) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
        return base64.b64encode(data).decode("utf-8"), content_type
    except Exception:
        return None


# ── AI providers ──────────────────────────────────────────────────────────────

async def _try_groq(messages: list[dict], system_prompt: str) -> str | None:
    """Try all available Groq keys round-robin until one succeeds or all fail."""
    if not _groq_clients:
        return None
    tried: set[str] = set()
    while True:
        pick = _next_groq_client()
        if pick is None or pick[0] in tried:
            break  # all keys exhausted or backed off
        api_key, client = pick
        tried.add(api_key)
        for attempt in range(1, GROQ_RETRIES + 1):
            try:
                resp = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=GROQ_MODEL_TEXT,
                        messages=[{"role": "system", "content": system_prompt}] + messages,
                        max_tokens=MAX_TOKENS,
                    ),
                    timeout=PROVIDER_TIMEOUT,
                )
                return resp.choices[0].message.content.strip()
            except asyncio.TimeoutError:
                print(f"[Groq …{api_key[-6:]}] Timeout after {PROVIDER_TIMEOUT}s (attempt {attempt}/{GROQ_RETRIES})")
                if attempt == GROQ_RETRIES:
                    _groq_set_backoff(api_key)  # repeated timeout → back off this key
            except Exception as e:
                error_msg = str(e)[:120]
                if "429" in error_msg or "rate" in error_msg.lower():
                    print(f"[Groq …{api_key[-6:]}] Rate limited — backing off: {error_msg}")
                    _groq_set_backoff(api_key)
                else:
                    print(f"[Groq …{api_key[-6:]}] {type(e).__name__}: {error_msg}")
                break  # move to next key
    return None


async def _try_groq_vision(
    messages: list[dict],
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
) -> str | None:
    """Try vision model across all available Groq keys."""
    if not _groq_clients:
        return None
    tried: set[str] = set()
    while True:
        pick = _next_groq_client()
        if pick is None or pick[0] in tried:
            break
        api_key, client = pick
        tried.add(api_key)
        try:
            history = [{"role": "system", "content": system_prompt}] + messages[:-1]
            content: list[dict] = []
            if user_text:
                content.append({"type": "text", "text": user_text})
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{image_b64}"},
            })
            history.append({"role": "user", "content": content})
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=GROQ_MODEL_VISION,
                    messages=history,
                    max_tokens=MAX_TOKENS,
                ),
                timeout=PROVIDER_TIMEOUT,
            )
            return resp.choices[0].message.content.strip()
        except asyncio.TimeoutError:
            print(f"[Groq Vision …{api_key[-6:]}] Timeout after {PROVIDER_TIMEOUT}s — trying next key")
            _groq_set_backoff(api_key)
        except Exception as e:
            error_msg = str(e)[:120]
            if "429" in error_msg or "rate" in error_msg.lower():
                print(f"[Groq Vision …{api_key[-6:]}] Rate limited — backing off")
                _groq_set_backoff(api_key)
            else:
                print(f"[Groq Vision …{api_key[-6:]}] {type(e).__name__}: {error_msg}")
            break
    return None


async def _try_gemini(
    api_key: str | None,
    model_name: str,
    messages: list[dict],
    system_prompt: str,
    image_b64: str | None = None,
    media_type: str | None = None,
) -> str | None:
    if not api_key or not genai:
        return None
    # Skip immediately if this key is in backoff (recently 429'd)
    if _gemini_is_backed_off(api_key):
        return None
    try:
        # Configure API key before creating the model (google.generativeai requires this)
        genai.configure(api_key=api_key)
        model   = genai.GenerativeModel(model_name=model_name, system_instruction=system_prompt)
        history = [
            {"role": "user" if m["role"] == "user" else "model", "parts": [m["content"]]}
            for m in messages[:-1]
        ]
        chat = model.start_chat(history=history)
        last = messages[-1]["content"]
        if image_b64 and media_type:
            parts = (([last] if last else []) +
                     [{"mime_type": media_type, "data": base64.b64decode(image_b64)}])
            resp = await asyncio.wait_for(chat.send_message_async(parts), timeout=PROVIDER_TIMEOUT)
        else:
            resp = await asyncio.wait_for(chat.send_message_async(last), timeout=PROVIDER_TIMEOUT)
        return resp.text.strip()
    except asyncio.TimeoutError:
        print(f"[Gemini {model_name}] Timeout after {PROVIDER_TIMEOUT}s")
        return None
    except Exception as e:
        error_msg = str(e)
        # 429 quota exceeded — back off silently, no spam
        if "429" in error_msg or "ResourceExhausted" in type(e).__name__ or "ResourceExhausted" in error_msg:
            _gemini_set_backoff(api_key)
            print(f"[Gemini {model_name}] 429 quota hit — pausing this key for {_GEMINI_BACKOFF}s, falling back to Groq.")
            return None
        print(f"[Gemini {model_name} Error] {type(e).__name__}: {error_msg[:100]}")
        return None


async def _race_providers(*tasks: asyncio.Task) -> str | None:
    """
    Race multiple API calls. Returns the first SUCCESSFUL (non-None) response.
    Keeps waiting for remaining tasks if early completions return None (e.g. 429s).
    Cancels remaining tasks only after a valid result is found.
    """
    if not tasks:
        return None

    remaining = set(tasks)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 20  # overall timeout

    while remaining:
        time_left = deadline - loop.time()
        if time_left <= 0:
            break
        try:
            done, remaining = await asyncio.wait(
                remaining,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=time_left,
            )
        except Exception:
            break

        if not done:
            break  # timeout hit with nothing completing

        for task in done:
            try:
                result = task.result()
                if result:
                    # Got a valid response — cancel everything still running
                    for t in remaining:
                        t.cancel()
                    return result
            except Exception:
                continue
        # All completed tasks returned None — loop and wait for the rest

    # Nothing succeeded — cancel any stragglers
    for t in remaining:
        t.cancel()
    return None



# ── Intent interceptor ────────────────────────────────────────────────────────
# Maps natural-language chat messages to real bot command outputs.
# Returns True if the message was handled (so caller skips the AI API),
# False if it should proceed to the AI normally.
#
# ── DESIGN RULE ──────────────────────────────────────────────────────────────
# Every pattern here must require EXPLICIT, UNAMBIGUOUS bot-directed phrasing.
# A lone keyword like "skip", "play", "ping", or "pause" is NOT enough —
# the surrounding phrase must clearly express intent to invoke that feature.
# This prevents conversational messages like "let's play a game", "why do dogs
# skip their chew toy", or "can you ping this user" from misfiring.
# ─────────────────────────────────────────────────────────────────────────────

_INTENT_PING = re.compile(
    # Must ask about *Jarvis/bot* latency — not "ping a user" or "ping @someone"
    # Excluded: "ping <username>", "ping @user", "ping this user", "ping them"
    r"\b(?:what\'?s?\s+(?:your\s+)?(?:ping|latency)"
    r"|check\s+(?:your\s+)?(?:ping|latency)"
    r"|how\s+(?:fast|slow)\s+are\s+you"
    r"|your\s+response\s+time"
    r"|bot\s+latency"
    r"|are\s+you\s+lagging"
    r"|why\s+are\s+you\s+(?:slow|lagging)"
    # "show ping", "show me ping", "what is your ping", "show your latency"
    r"|show\s+(?:me\s+)?(?:your\s+)?(?:ping|latency)"
    r"|(?:your\s+)?ping\s*\?"         # "your ping?" or just "ping?"
    r"|(?:^|\s)ping\s*$"              # lone "ping" at end of message
    r")\b",
    re.IGNORECASE,
)
_INTENT_UPTIME = re.compile(
    r"\b(uptime|how\s+long\s+have\s+you\s+been\s+(online|running|up|alive)|"
    r"how\s+long\s+(you\'?ve|you\s+have)\s+been\s+(online|running|up)|"
    r"when\s+did\s+you\s+(come\s+online|start)|been\s+running\s+for)\b",
    re.IGNORECASE,
)
_INTENT_USAGE = re.compile(
    r"\b(show\s+(me\s+)?(system|server|host)\s+(usage|stats?|resources?)|"
    r"(system|server|host)\s+(usage|stats?|resources?)\s*(please|\?)?$|"
    r"how\s+much\s+(ram|cpu|memory|disk)\s+(are\s+you\s+using|is\s+being\s+used))\b",
    re.IGNORECASE,
)
_INTENT_STATS = re.compile(
    r"\b(show\s+(me\s+)?my\s+stats?|what\s+are\s+my\s+stats?|"
    r"my\s+usage\s+stats?|how\s+many\s+(ai\s+)?(messages?|tokens)\s+(have\s+i\s+sent|do\s+i\s+have)|"
    r"my\s+daily\s+(ai\s+)?limit)\b",
    re.IGNORECASE,
)
_INTENT_HELP = re.compile(
    r"\b(show\s+(me\s+)?(the\s+)?(help|commands?(\s+list)?|command\s+list)|"
    r"what\s+commands?\s+(do\s+you\s+have|can\s+i\s+use|are\s+available)|"
    r"list\s+(all\s+)?commands?|available\s+commands?|help\s+menu|open\s+help)\b",
    re.IGNORECASE,
)
_INTENT_MEMORY = re.compile(
    r"\b(what\s+do\s+you\s+(know|remember)\s+about\s+me|"
    r"what\s+have\s+you\s+(stored|saved|remembered)\s+(about\s+me)?|"
    r"show\s+(me\s+)?my\s+memor(y|ies)|clear\s+my\s+memor(y|ies))\b",
    re.IGNORECASE,
)
_INTENT_MODEL = re.compile(
    r"\b(what\s+(model|ai)\s+are\s+you(\s+using|\s+running\s+on)?|"
    r"which\s+(ai\s+)?model\s+(are\s+you|is\s+(active|running))|"
    r"what\s+ai\s+are\s+you|my\s+current\s+model|change\s+my\s+model)\b",
    re.IGNORECASE,
)
_INTENT_LIMIT = re.compile(
    r"\b(how\s+many\s+(ai\s+)?(messages?|requests?)\s+(do\s+i\s+have\s+left|remaining)|"
    r"what\'?s?\s+my\s+(daily\s+)?(ai\s+)?limit|ai\s+(messages?\s+)?left)\b",
    re.IGNORECASE,
)

# ── Music intents ─────────────────────────────────────────────────────────────
# NOTE: "play" alone is too broad — must have explicit music context.
# "let's play a game" must NOT trigger this.
#
# Matching rules:
#  1. Generic music: "play music", "play some songs", etc.
#  2. Explicit queue actions: "put on X", "queue up X"
#  3. Vague play: "play me something", "play anything"
#  4. Song reference: "play the song", "play this track"
#  5. "X by Y" pattern: "play Someone You Loved by Lewis Capaldi"
#  6. Bare song title: "play <2+ words>" — catches "play Someone You Loved"
#     BUT we exclude game-like contexts: "play a game", "play [game name]",
#     "let's play", "wanna play" etc. to avoid misfires.
_INTENT_PLAY = re.compile(
    r"(?:"
    # 1. Generic music keywords
    r"play\s+(?:some\s+)?(?:music|songs?|tracks?|tunes?|a\s+song|a\s+track|some\s+(?:\w+\s+)?music)\b"
    # 2. Explicit queue/put-on actions with a target
    r"|(?:put\s+on|start\s+playing|queue\s+up|add\s+to\s+(?:the\s+)?(?:music\s+)?queue)\s+\S"
    # 3. Vague play requests
    r"|play\s+(?:me\s+)?(?:something|anything)\b"
    # 4. Explicit song/track reference
    r"|(?:can\s+you\s+)?play\s+(?:the\s+song|the\s+track|this\s+song|that\s+song)\b"
    # 5. "X by Y" pattern — "play Someone You Loved by Lewis Capaldi"
    r"|play\s+\S+(?:\s+\S+)*?\s+by\s+\S"
    # 6. Bare song title with 2+ words — catches "play Someone You Loved",
    #    "play Blinding Lights", etc.
    #    Must appear at start of (cleaned) message or after a non-game-intent prefix.
    #    Excluded targets: game keywords, and single-word-only targets.
    #    Game-intent phrases ("let's play", "wanna play") are handled upstream
    #    by checking the full user_text in _try_intent_intercept before this fires.
    r"|(?:^|\.\s+|\?\s+)play\s+(?!a\s+game\b)(?!me\b)(?!some\b)(?!any\b)(?!trivia\b)(?!hangman\b)(?!chess\b)(?!uno\b)(?!poker\b)(?!minecraft\b)(?!roulette\b)(?!\w+\s*$)\w[\w\s']{2,}"
    r")",
    re.IGNORECASE,
)
_INTENT_SKIP = re.compile(
    # Must clearly be about skipping a song/track
    r"\b(?:skip\s+(?:this\s+)?(?:song|track|one)|next\s+(?:song|track)|"
    r"(?:can\s+you\s+)?skip\s+(?:it|this)\b|skip\s+to\s+(?:the\s+)?next)\b",
    re.IGNORECASE,
)
_INTENT_STOP_MUSIC = re.compile(
    r"\b(?:stop\s+(?:the\s+)?(?:music|song|playback)|stop\s+playing\s+(?:music|the\s+song)|"
    r"(?:leave|disconnect\s+from)\s+(?:the\s+)?(?:vc|voice\s+channel))\b",
    re.IGNORECASE,
)
_INTENT_PAUSE_MUSIC = re.compile(
    # "pause" alone is too vague; require music context
    r"\b(?:pause\s+(?:the\s+)?(?:music|song|playback)|resume\s+(?:the\s+)?(?:music|song|playback)|"
    r"unpause\s+(?:the\s+)?(?:music|song)?|(?:can\s+you\s+)?(?:pause|resume)\s+(?:it|the\s+music))\b",
    re.IGNORECASE,
)
_INTENT_QUEUE = re.compile(
    r"\b(?:show\s+(?:me\s+)?(?:the\s+)?(?:music\s+|song\s+)?queue|"
    r"what\'?s?\s+in\s+(?:the\s+)?(?:music\s+|song\s+)?queue|"
    r"(?:music|song)\s+queue(?:\s+list)?|queue\s+list)\b",
    re.IGNORECASE,
)
_INTENT_NOWPLAYING = re.compile(
    r"\b(?:what\'?s?\s+(?:currently\s+)?playing(?:\s+right\s+now)?|"
    r"now\s+playing|what\s+(?:song|track)\s+is\s+(?:this|playing)|"
    r"current\s+(?:song|track))\b",
    re.IGNORECASE,
)

# ── Image search intent ───────────────────────────────────────────────────────
# Requires "send/show/find me an image/photo/picture OF something" — explicit request
_INTENT_IMAGE = re.compile(
    r"\b(?:(?:send|show|find|get|search(?:\s+for)?)\s+(?:me\s+)?(?:an?\s+)?(?:image|photo|picture|pic)\s+(?:of|for)\s+\S"
    r"|(?:can\s+you\s+)?(?:send|show)\s+(?:me\s+)?(?:a|an)\s+(?:image|photo|picture|pic)\s+of\s+\S)\b",
    re.IGNORECASE,
)

# ── Music controls intent ─────────────────────────────────────────────────────
_INTENT_CONTROLS = re.compile(
    r"\b(?:(?:open|show|bring\s+up|pull\s+up|give\s+me)\s+(?:the\s+)?(?:music\s+)?controls?|"
    r"music\s+controls?\s+(?:panel|menu)|player\s+controls?\s+(?:panel|menu))\b",
    re.IGNORECASE,
)

# ── YouTube intents ───────────────────────────────────────────────────────────
_INTENT_YT_SEARCH = re.compile(
    r"\b(?:search\s+(?:youtube|yt)\s+for\s+\S|youtube\s+search\s+\S|"
    r"find\s+(?:on\s+)?(?:youtube|yt)\s+\S|look\s+up\s+(?:on\s+)?(?:youtube|yt)\s+\S|"
    r"(?:youtube|yt)\s+(?:video|videos?)\s+(?:of|for|about)\s+\S)\b",
    re.IGNORECASE,
)
_INTENT_REEL_SEARCH = re.compile(
    r"\b(?:(?:send|show|find|get|search(?:\s+for)?)\s+(?:me\s+)?(?:an?\s+)?(?:reel|short|shorts)\s+(?:of|for|about)\s+\S"
    r"|(?:can\s+you\s+)?(?:send|show)\s+(?:me\s+)?(?:a|an)\s+(?:reel|short)\s+(?:of|about)\s+\S"
    r"|search\s+(?:youtube\s+)?shorts\s+(?:for\s+)?\S)\b",
    re.IGNORECASE,
)
_INTENT_YT_TREND = re.compile(
    r"\b(?:what\'?s?\s+trending\s+on\s+(?:youtube|yt)|(?:youtube|yt)\s+trending|"
    r"show\s+(?:me\s+)?(?:youtube|yt)\s+trends?|top\s+(?:youtube|yt)\s+videos?\s+(?:today|now|right\s+now))\b",
    re.IGNORECASE,
)
_INTENT_YT_INFO = re.compile(
    r"\b(?:youtube\s+info|ytinfo|video\s+info\s+for\s+\S|"
    r"get\s+info\s+(?:for|about|on)\s+https?://\S*(?:youtube|youtu\.be)\S*)\b",
    re.IGNORECASE,
)


def _parse_relative_time(text: str) -> float | None:
    """Parse a relative time duration like '10m', '1h 30m', or '2h' into seconds."""
    text = text.strip().lower()
    if text.startswith("in "):
        text = text[3:]
    if not text:
        return None
    total = 0.0
    for part in re.findall(r"(\d+(?:\.\d+)?)([smhd])", text):
        value = float(part[0])
        unit = part[1]
        if unit == "s":
            total += value
        elif unit == "m":
            total += value * 60
        elif unit == "h":
            total += value * 3600
        elif unit == "d":
            total += value * 86400
    return total if total > 0 else None


async def _try_intent_intercept(
    message: discord.Message,
    user_text: str,
    bot: commands.Bot,
) -> bool:
    """
    Check if user_text maps to a known bot command intent.
    If it does, send the real embed/response and return True.
    Return False to let the message fall through to the AI.
    """
    from cogs.admin import is_admin as _is_admin

    # ── ping ──────────────────────────────────────────────────────────────
    if _INTENT_PING.search(user_text):
        import time as _time
        from cogs.system import _ping_colour
        ws_ms  = round(bot.latency * 1000)
        before = _time.monotonic()
        msg    = await safe_reply(message, "🏓 Pinging…")
        api_ms = round((_time.monotonic() - before) * 1000)
        embed  = discord.Embed(title="🏓 Pong!", color=_ping_colour(ws_ms))
        embed.add_field(name="WebSocket",      value=f"`{ws_ms} ms`",  inline=True)
        embed.add_field(name="API Round-trip", value=f"`{api_ms} ms`", inline=True)
        await msg.edit(content=None, embed=embed)
        return True

    # ── uptime ────────────────────────────────────────────────────────────
    if _INTENT_UPTIME.search(user_text):
        from cogs.system import _START_TIME, _fmt_uptime
        import time as _time
        uptime = _time.monotonic() - _START_TIME
        embed  = discord.Embed(
            title="⏱️ Jarvis Uptime",
            description=f"Online for **{_fmt_uptime(uptime)}**",
            color=discord.Color.green(),
        )
        embed.set_footer(text="Jarvis")
        await safe_reply(message, embed=embed)
        return True

    # ── system usage (admin only) ─────────────────────────────────────────
    if _INTENT_USAGE.search(user_text):
        if not _is_admin(message.author):
            await safe_reply(message, "🚫 System usage stats are admin-only.", mention_author=False)
            return True
        from cogs.system import _build_usage_embed
        await safe_reply(message, embed=_build_usage_embed(bot))
        return True

    # ── personal stats ────────────────────────────────────────────────────
    if _INTENT_STATS.search(user_text):
        from cogs.state import get_stats as _get_stats
        from cogs.stats import _format_stats, _no_stats_embed
        data = _get_stats(message.author.id)
        if not data:
            await safe_reply(message, embed=_no_stats_embed(message.author, True))
        else:
            # Get rank and memory count
            try:
                from cogs.stats import Stats as _StatsCog
                cog = bot.cogs.get("Stats")
                rank = cog._user_rank(message.author.id) if cog else None
                mem  = await cog._memory_count(message.author.id) if cog else 0
            except Exception:
                rank, mem = None, 0
            await safe_reply(message, embed=_format_stats(message.author, data, rank=rank, memory_count=mem))
        return True

    # ── daily AI limit ────────────────────────────────────────────────────
    if _INTENT_LIMIT.search(user_text):
        await safe_reply(message, embed=_build_mylimit_embed(message.author.id))
        return True

    # ── help / command list ───────────────────────────────────────────────
    if _INTENT_HELP.search(user_text):
        from cogs.help import _build_overview_embed, HelpView
        view = HelpView(author_id=message.author.id)
        await safe_reply(message, embed=_build_overview_embed(), view=view)
        return True

    # ── memory ────────────────────────────────────────────────────────────
    if _INTENT_MEMORY.search(user_text):
        from cogs.memory import get_facts as _get_facts
        facts = await _get_facts(message.author.id)
        await safe_reply(message, embed=_build_memory_embed(message.author, facts))
        return True

    # ── current model ─────────────────────────────────────────────────────
    if _INTENT_MODEL.search(user_text):
        await safe_reply(message, embed=_build_model_embed(message.author.id))
        return True

    # ── music: play ───────────────────────────────────────────────────────────
    # Guard: reject game-intent phrases before checking the play regex.
    # "let's play", "wanna play", "want to play" should never trigger music.
    _GAME_INTENT = re.compile(
        r"\b(?:let'?s\s+play|wanna\s+play|want\s+to\s+play|can\s+we\s+play|"
        r"play\s+(?:a\s+)?(?:game|round|match)|i\s+want\s+to\s+play\s+a)\b",
        re.IGNORECASE,
    )
    if _INTENT_PLAY.search(user_text) and message.guild and not _GAME_INTENT.search(user_text):
        music_cog = bot.cogs.get("Music")
        if music_cog:
            if _music_module.MUSIC_FEATURE_DOWN:
                await safe_reply(message, embed=_music_module.MUSIC_DOWN_EMBED)
                return True
            # Strip common prefixes to extract the actual song query
            query = re.sub(
                r"^\s*(?:can\s+you\s+)?(?:please\s+)?"
                r"(?:play|put\s+on|start\s+playing|queue\s+up|add\s+to\s+(?:the\s+)?(?:music\s+)?queue)"
                r"(?:\s+(?:me\s+)?(?:the\s+song\s+)?(?:called\s+)?)?",
                "", user_text, flags=re.IGNORECASE
            ).strip(" ,:-")
            if not query:
                await safe_reply(message, "\U0001f3b5 What song would you like me to play?")
                return True
            send_fn = lambda *a, **kw: safe_reply(message, *a, **kw)
            await music_cog._do_play(message.guild, message.author, query, send_fn)
            return True

    # ── music: skip ───────────────────────────────────────────────────────────
    if _INTENT_SKIP.search(user_text) and message.guild:
        music_cog = bot.cogs.get("Music")
        if music_cog:
            if _music_module.MUSIC_FEATURE_DOWN:
                await safe_reply(message, embed=_music_module.MUSIC_DOWN_EMBED)
                return True
            send_fn = lambda *a, **kw: safe_reply(message, *a, **kw)
            await music_cog._do_skip(message.guild, send_fn)
            return True

    # ── music: stop ───────────────────────────────────────────────────────────
    if _INTENT_STOP_MUSIC.search(user_text) and message.guild:
        music_cog = bot.cogs.get("Music")
        if music_cog:
            if _music_module.MUSIC_FEATURE_DOWN:
                await safe_reply(message, embed=_music_module.MUSIC_DOWN_EMBED)
                return True
            send_fn = lambda *a, **kw: safe_reply(message, *a, **kw)
            await music_cog._do_stop(message.guild, send_fn)
            return True

    # ── music: pause / resume ─────────────────────────────────────────────────
    if _INTENT_PAUSE_MUSIC.search(user_text) and message.guild:
        music_cog = bot.cogs.get("Music")
        if music_cog:
            if _music_module.MUSIC_FEATURE_DOWN:
                await safe_reply(message, embed=_music_module.MUSIC_DOWN_EMBED)
                return True
            send_fn = lambda *a, **kw: safe_reply(message, *a, **kw)
            await music_cog._do_pause(message.guild, send_fn)
            return True

    # ── music: queue ──────────────────────────────────────────────────────────
    if _INTENT_QUEUE.search(user_text) and message.guild:
        music_cog = bot.cogs.get("Music")
        if music_cog:
            if _music_module.MUSIC_FEATURE_DOWN:
                await safe_reply(message, embed=_music_module.MUSIC_DOWN_EMBED)
                return True
            send_fn = lambda *a, **kw: safe_reply(message, *a, **kw)
            await music_cog._do_queue(message.guild, send_fn)
            return True

    # ── music: now playing ────────────────────────────────────────────────────
    if _INTENT_NOWPLAYING.search(user_text) and message.guild:
        music_cog = bot.cogs.get("Music")
        if music_cog:
            if _music_module.MUSIC_FEATURE_DOWN:
                await safe_reply(message, embed=_music_module.MUSIC_DOWN_EMBED)
                return True
            send_fn = lambda *a, **kw: safe_reply(message, *a, **kw)
            await music_cog._do_np(message.guild, send_fn)
            return True

    # ── image search ──────────────────────────────────────────────────────────
    if _INTENT_IMAGE.search(user_text):
        image_cog = bot.cogs.get("ImageSearch")
        if image_cog:
            query = re.sub(
                r".*?\b(image|photo|picture|pic)\s+(of|for)\s*",
                "", user_text, count=1, flags=re.IGNORECASE
            ).strip(" ,:-")
            if not query:
                query = re.sub(
                    r"^\s*(send\s+(me\s+)?(an?\s+)?(image|photo|picture|pic)|show\s+(me\s+)?(an?\s+)?(image|photo|picture|pic)|(find|get|search(\s+for)?)\s+(an?\s+)?(image|photo|picture|pic))\s*",
                    "", user_text, flags=re.IGNORECASE
                ).strip(" ,:-")
            if not query:
                await safe_reply(message, "\U0001f50d What would you like me to search an image for?")
                return True
            await image_cog._send_image(message, query)
            return True

    # ── music controls panel ──────────────────────────────────────────────────
    if _INTENT_CONTROLS.search(user_text) and message.guild:
        music_cog = bot.cogs.get("Music")
        if music_cog:
            if _music_module.MUSIC_FEATURE_DOWN:
                await safe_reply(message, embed=_music_module.MUSIC_DOWN_EMBED)
                return True
            send_fn = lambda *a, **kw: safe_reply(message, *a, **kw)
            await music_cog._send_controls(message.guild, send_fn)
            return True

    # ── reel/shorts search ────────────────────────────────────────────────────
    if _INTENT_REEL_SEARCH.search(user_text):
        yt_cog = bot.cogs.get("YouTube")
        if yt_cog:
            query = re.sub(
                r".*?\b(reel|short|shorts)\s+(of|for|about)\s*",
                "", user_text, count=1, flags=re.IGNORECASE
            ).strip(" ,:-")
            if not query:
                query = re.sub(
                    r"^\s*(send\s+(me\s+)?(an?\s+)?(reel|short|shorts)|show\s+(me\s+)?(an?\s+)?(reel|short|shorts)|search\s+(youtube\s+)?shorts)\s*(for)?\s*",
                    "", user_text, flags=re.IGNORECASE
                ).strip(" ,:-")
            if not query:
                await safe_reply(message, "\U0001f4f1 What would you like me to search a reel for?")
                return True
            await yt_cog._do_search_shorts(
                query        = query,
                user         = message.author,
                send_fn      = lambda **kw: safe_reply(message, **kw),
                ephemeral_fn = lambda **kw: safe_reply(message, **kw),
            )
            return True

    # ── youtube: search ───────────────────────────────────────────────────────
    if _INTENT_YT_SEARCH.search(user_text):
        yt_cog = bot.cogs.get("YouTube")
        if yt_cog:
            query = re.sub(
                r"\b(search\s+(youtube|yt)\s+(for)?|youtube\s+search|find\s+(on\s+)?(youtube|yt)|look\s+up\s+(on\s+)?(youtube|yt)|(youtube|yt)\s+(video|videos?)\s+(of|for|about))\s*",
                "", user_text, flags=re.IGNORECASE
            ).strip(" ,:-")
            if not query:
                await safe_reply(message, "\U0001f3ac What would you like me to search on YouTube?")
                return True
            await yt_cog._do_search(
                query        = query,
                user         = message.author,
                send_fn      = lambda **kw: safe_reply(message, **kw),
                ephemeral_fn = lambda **kw: safe_reply(message, **kw),
            )
            return True

    # ── youtube: trending ─────────────────────────────────────────────────────
    if _INTENT_YT_TREND.search(user_text):
        yt_cog = bot.cogs.get("YouTube")
        if yt_cog:
            import discord as _discord
            embed = _discord.Embed(
                title       = "\U0001f525 YouTube Trending",
                description = "Pick a category from the dropdown to see what\'s trending.",
                color       = 0xFF0000,
            )
            from cogs.youtube import TrendingView
            view = TrendingView(message.author)
            await safe_reply(message, embed=embed, view=view)
            return True

    # ── youtube: video info ───────────────────────────────────────────────────
    if _INTENT_YT_INFO.search(user_text):
        yt_cog = bot.cogs.get("YouTube")
        if yt_cog:
            # Extract URL or video ID from the message
            url_match = re.search(r"https?://\S+", user_text)
            vid_match = re.search(r"\b([A-Za-z0-9_-]{11})\b", user_text)
            target = (url_match.group(0) if url_match else
                      vid_match.group(1) if vid_match else "")
            if not target:
                await safe_reply(message, "\U0001f4cb Please include a YouTube URL or video ID.")
                return True
            await yt_cog._do_info(
                target       = target,
                user         = message.author,
                send_fn      = lambda **kw: safe_reply(message, **kw),
                ephemeral_fn = lambda **kw: safe_reply(message, **kw),
            )
            return True

    return False


async def _offer_ai_limit_reset(send_fn, user_id: int) -> bool:
    """
    Called once a user has hit their daily AI limit. If they have enough JC,
    show a Yes/No prompt offering to reset their daily counter for the cost
    of AI_LIMIT_RESET_COST JC. Otherwise just send the normal limit message.

    `send_fn` is an async callable matching `message.reply` / `interaction.followup.send`
    signatures — i.e. accepts content=/embed=/view= kwargs and returns the sent message.

    Always returns True: the current request isn't fulfilled this turn either way
    (the user must resend their message after a reset).
    """
    limit = get_ai_limit()

    if get_credits(user_id) < AI_LIMIT_RESET_COST:
        await send_fn(content=DAILY_LIMIT_MSG.format(limit=limit))
        return True

    async def on_confirm(interaction: discord.Interaction, view: SpendCreditsView):
        reset_ai_usage(user_id)
        embed = discord.Embed(
            title="✅ Daily Limit Reset!",
            description=(
                f"{JC_EMOJI} **{AI_LIMIT_RESET_COST} {JC_NAME}** spent.\n"
                f"Your daily AI usage has been reset to **0/{limit}** — send your message again!"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(content=None, embed=embed, view=view)

    async def on_decline(interaction: discord.Interaction, view: SpendCreditsView, reason: str):
        text = DAILY_LIMIT_MSG.format(limit=limit)
        if reason == "insufficient":
            text = f"❌ Not enough {JC_EMOJI} {JC_NAME}s (need **{AI_LIMIT_RESET_COST}**).\n\n" + text
        await interaction.response.edit_message(content=text, embed=None, view=view)

    view = SpendCreditsView(user_id, AI_LIMIT_RESET_COST, on_confirm, on_decline, timeout=30)
    embed = discord.Embed(
        title="📊 Daily AI Limit Reached",
        description=(
            f"You've used all **{limit}** of your free AI messages for today.\n\n"
            f"Spend **{AI_LIMIT_RESET_COST}** {JC_EMOJI} {JC_NAME} to reset your daily counter "
            f"and keep chatting?"
        ),
        color=discord.Color.orange(),
    )
    msg = await send_fn(embed=embed, view=view)
    view.message = msg
    return True


# ── Core response ─────────────────────────────────────────────────────────────

async def generate_ai_response(
    user_id: int,
    user_message: str,
    channel_id: int,
    guild_id: int | None = None,
    image_b64: str | None = None,
    media_type: str | None = None,
    user: discord.User | discord.Member | None = None,
    reply_context: list[dict] | None = None,
    thread_root_id: int | None = None,
) -> str:
    if is_ai_rate_limited(user_id):
        limit = get_ai_limit()
        return DAILY_LIMIT_MSG.format(limit=limit)

    # Prevent abusive repeat/spam requests that ask Jarvis to output a phrase
    # many times (e.g. "say Hi 100 times"). Detect common patterns and
    # refuse if the requested repeat count exceeds a sane threshold.
    def _requested_repeat_count(text: str) -> int | None:
        # Patterns like "... 100 times", "repeat 100 times", "say hi 100x"
        m = re.search(r"(?:repeat|say|spam|print)\s+['\"]?.{0,50}?['\"]?\s*(?:for\s*)?(?P<n>\d{1,5})\s*(?:x|times)?\b", text, re.IGNORECASE)
        if m:
            try:
                return int(m.group("n"))
            except Exception:
                return None
        m2 = re.search(r"\b(?P<n>\d{1,5})\s*(?:x|times)\b", text, re.IGNORECASE)
        if m2:
            try:
                return int(m2.group("n"))
            except Exception:
                return None
        return None

    rep = _requested_repeat_count(user_message or "")
    if rep and rep > int(get_setting("max_repeat_requests", 10)):
        return (
            f"🚫 I won't repeat something {rep} times — that's abusive. "
            f"If you really need a repeated output, ask for at most {get_setting('max_repeat_requests', 10)} repetitions."
        )

    # Quick cache check — if same message asked within 30s, return cached response
    if not image_b64:  # Only cache text responses
        cache_key = (user_id, channel_id, user_message)
        if cache_key in _response_cache:
            cached_response, cache_time = _response_cache[cache_key]
            if time.time() - cache_time < 30:  # Cache for 30 seconds
                # Still extract memory even on cache hits so "remember to call me X"
                # is never silently dropped just because the message was repeated.
                if user_message and thread_root_id is None:
                    async def _extract_memory_cached():
                        try:
                            facts = extract_facts(user_message)
                            if facts:
                                await save_facts(user_id, facts)
                        except Exception as e:
                            print(f"[Memory] extract error (cached): {e}")
                    asyncio.create_task(_extract_memory_cached())
                return cached_response

    base_prompt = get_guild_prompt(guild_id) or DEFAULT_SYSTEM_PROMPT
    in_group    = _is_in_group(user_id, channel_id)
    in_merge    = thread_root_id is not None

    # ── Long-term memory injection ────────────────────────────────────────────
    # Start memory fetch immediately — it runs concurrently while we build the
    # system prompt and history. We await it only right before we need it.
    memory_task = None
    if not in_group and not in_merge:
        memory_task = asyncio.create_task(get_facts(user_id))

    # Build history and prompt while memory is fetching in background
    history  = _get_history(user_id, channel_id, thread_root_id)
    has_image = bool(image_b64 and media_type)

    # Await memory result now — by this point it's likely already done
    memory_suffix = ""
    if memory_task is not None:
        try:
            facts = await asyncio.wait_for(memory_task, timeout=0.25)
            memory_suffix = build_memory_prompt(facts)
        except Exception:
            memory_suffix = ""
    effective_base = base_prompt + memory_suffix

    if user:
        name     = get_preferred_name(user.id) or (user.display_name if user.display_name != user.name else user.name)
        username = user.name
        if in_merge:
            cache_key = (thread_root_id, "merge")
            if cache_key not in _system_prompt_cache:
                _system_prompt_cache[cache_key] = (
                    effective_base +
                    "\n\nThis is a shared conversation between multiple people replying in the same thread. "
                    "Messages are prefixed with each person's name so you know who said what. "
                    "Maintain a single coherent conversation thread across all participants. "
                    f"The person who just sent this message is {name} (username: {username}). "
                    "Only reveal their name/username if they directly ask."
                )
            system_prompt = _system_prompt_cache[cache_key]
        elif in_group:
            n = len(active_groups[channel_id])
            cache_key = (user_id, "group", n)
            if cache_key not in _system_prompt_cache:
                _system_prompt_cache[cache_key] = (
                    effective_base +
                    f"\n\nThis is a shared group conversation between {n} people. "
                    f"Messages are prefixed with the sender's name so you know who said what. "
                    f"The person who just sent this message is {name} (username: {username}). "
                    f"Only mention their name/username if they directly ask about their own identity."
                )
            system_prompt = _system_prompt_cache[cache_key]
        else:
            # Private — don't cache when memory_suffix is present, since facts change
            if not memory_suffix:
                cache_key = (user_id, "private")
                if cache_key not in _system_prompt_cache:
                    _system_prompt_cache[cache_key] = (
                        effective_base +
                        f"\n\nThe person messaging you is {name} (username: {username}). "
                        f"Only reveal this if they directly ask who they are. "
                        f"Never volunteer their name or username unprompted."
                    )
                system_prompt = _system_prompt_cache[cache_key]
            else:
                # Memory-enriched prompt — build fresh each time (facts may have just been added)
                system_prompt = (
                    effective_base +
                    f"\n\nThe person messaging you is {name} (username: {username}). "
                    f"Only reveal this if they directly ask who they are. "
                    f"Never volunteer their name or username unprompted."
                )
    else:
        system_prompt = effective_base

    # In merge threads reply_context is not needed — history IS the shared thread.
    # For non-merge replies, apply the old read-only context injection.
    if reply_context and not in_merge:
        combined = list(reply_context)
        for entry in history:
            if entry not in combined:
                combined.append(entry)
        history = combined

    # Always prefix with speaker name in merge threads (and group mode)
    stored = (
        f"[{user.display_name}]: {user_message or '(sent an image)'}"
        if ((in_group or in_merge) and user)
        else (user_message or "(sent an image)")
    )

    # Write to the correct history bucket
    real_history = _get_history(user_id, channel_id, thread_root_id)
    real_history.append({"role": "user", "content": stored})
    _trim(real_history)

    # Build the working history for this request
    if reply_context and not in_merge:
        history.append({"role": "user", "content": stored})
        _trim(history)
    else:
        history = real_history

    # ── Route based on user's preferred model ─────────────────────────────────
    # Strategy: try the preferred/primary provider first with a short timeout.
    # Only call Gemini if Groq fails, and only if Gemini keys aren't backed off.
    # This keeps responses fast (Groq ~1s) and avoids wasting time on 429s.
    preferred = get_user_model(user_id)
    reply = None

    gemini_keys_available = (
        genai is not None and
        (
            (GEMINI_API_KEY   and not _gemini_is_backed_off(GEMINI_API_KEY)) or
            (GEMINI_API_KEY_2 and not _gemini_is_backed_off(GEMINI_API_KEY_2))
        )
    )

    if preferred == "gemini-flash":
        # Try Gemini first, fall back to Groq
        tasks = []
        if GEMINI_API_KEY   and not _gemini_is_backed_off(GEMINI_API_KEY):
            tasks.append(asyncio.create_task(_try_gemini(GEMINI_API_KEY,  GEMINI_MODEL_FLASH, history, system_prompt, image_b64, media_type)))
        if GEMINI_API_KEY_2 and not _gemini_is_backed_off(GEMINI_API_KEY_2):
            tasks.append(asyncio.create_task(_try_gemini(GEMINI_API_KEY_2, GEMINI_MODEL_FLASH, history, system_prompt, image_b64, media_type)))
        if tasks:
            reply = await _race_providers(*tasks)
        if not reply:
            reply = await _try_groq(history, system_prompt)

    elif preferred == "gemini-lite":
        # Try Gemini lite first, fall back to flash, then Groq
        tasks = []
        if GEMINI_API_KEY   and not _gemini_is_backed_off(GEMINI_API_KEY):
            tasks.append(asyncio.create_task(_try_gemini(GEMINI_API_KEY,  GEMINI_MODEL_LITE, history, system_prompt, image_b64, media_type)))
        if GEMINI_API_KEY_2 and not _gemini_is_backed_off(GEMINI_API_KEY_2):
            tasks.append(asyncio.create_task(_try_gemini(GEMINI_API_KEY_2, GEMINI_MODEL_LITE, history, system_prompt, image_b64, media_type)))
        if tasks:
            reply = await _race_providers(*tasks)
        if not reply and gemini_keys_available:
            flash_tasks = []
            if GEMINI_API_KEY   and not _gemini_is_backed_off(GEMINI_API_KEY):
                flash_tasks.append(asyncio.create_task(_try_gemini(GEMINI_API_KEY,  GEMINI_MODEL_FLASH, history, system_prompt, image_b64, media_type)))
            if GEMINI_API_KEY_2 and not _gemini_is_backed_off(GEMINI_API_KEY_2):
                flash_tasks.append(asyncio.create_task(_try_gemini(GEMINI_API_KEY_2, GEMINI_MODEL_FLASH, history, system_prompt, image_b64, media_type)))
            if flash_tasks:
                reply = await _race_providers(*flash_tasks)
        if not reply:
            reply = await _try_groq(history, system_prompt)

    else:
        # Ultra-fast path for auto/groq: use Groq directly and avoid the extra
        # Gemini fallback work entirely. This is the quickest practical route
        # for normal chat replies and keeps latency low.
        if has_image:
            reply = await _try_groq_vision(history, system_prompt, image_b64, media_type, user_message)
        else:
            reply = await _try_groq(history, system_prompt)

    if not reply:
        history.pop()
        print(f"[AI Response] All providers failed for user {user_id} in channel {channel_id}")
        return AI_UNAVAILABLE_MSG

    history.append({"role": "assistant", "content": reply})
    
    # Only trim if necessary (avoid unnecessary list operations)
    if len(history) > HISTORY_LIMIT:
        _trim(history)
    
    # ── Persist session history to DB (fire-and-forget) ───────────────────────
    # Only for private conversations (not merge threads or group chats — those
    # are ephemeral by design)
    if thread_root_id is None and not in_group:
        async def _persist_session():
            try:
                await _db_add_message(user_id, "user", stored)
                await _db_add_message(user_id, "assistant", reply)
            except Exception as e:
                print(f"[History] persist error: {e}")
        asyncio.create_task(_persist_session())

    # ── Extract and save long-term memories (fire-and-forget) ─────────────────
    if user_message and thread_root_id is None and not in_group:
        async def _extract_memory():
            try:
                facts = extract_facts(user_message)
                if facts:
                    await save_facts(user_id, facts)
            except Exception as e:
                print(f"[Memory] extract error: {e}")
        asyncio.create_task(_extract_memory())

    # Fire-and-forget: record stats in background, don't block response
    asyncio.create_task(asyncio.to_thread(_background_record, user_id, user_message, reply))

    new_count = increment_ai_usage(user_id)
    earn_chat_credits(user_id, AI_CHAT_REWARD, AI_CHAT_REWARD_DAILY_CAP)
    if new_count == WARN_AT:
        limit = get_ai_limit()
        remaining = limit - new_count
        reply += f"\n\n{WARN_LIMIT_MSG.format(count=new_count, limit=limit, remaining=remaining)}"

    # Cache text responses for 30 seconds (skip if has image)
    # Sweep stale entries every 100 writes to avoid unbounded growth without
    # paying a full scan cost on every single message.
    if not image_b64:
        cache_key = (user_id, channel_id, user_message)
        now = time.time()
        _response_cache[cache_key] = (reply, now)
        _response_cache_writes[0] += 1
        if _response_cache_writes[0] >= 100:
            _response_cache_writes[0] = 0
            stale = [k for k, (_, ts) in _response_cache.items() if now - ts > 30]
            for k in stale:
                _response_cache.pop(k, None)

    return reply


# ── Shared embed builders ─────────────────────────────────────────────────────

def _build_mylimit_embed(user_id: int) -> discord.Embed:
    count, day = get_ai_usage(user_id)
    limit = get_ai_limit()
    remaining  = max(0, limit - count)
    pct        = count / limit if limit else 1

    if remaining == 0:
        colour, status = discord.Color.red(),    "❌ Limit reached"
    elif pct >= 0.75:
        colour, status = discord.Color.orange(), "⚠️ Running low"
    else:
        colour, status = discord.Color.green(),  "✅ Good to go"

    bar_filled = int(pct * 10)
    bar        = "█" * bar_filled + "░" * (10 - bar_filled)

    embed = discord.Embed(
        title="📊 Your Daily AI Limit",
        color=colour,
        description=(
            f"`{bar}` {count}/{limit}\n"
            f"**Status:** {status}\n"
            f"**Remaining:** {remaining} message(s)\n"
            f"**Resets:** midnight UTC (daily)"
        ),
    )
    embed.set_footer(text=f"Limit resets every day at 00:00 UTC  •  {day}")
    return embed


def _build_model_embed(user_id: int) -> discord.Embed:
    current = get_user_model(user_id)
    info    = MODELS[current]
    embed   = discord.Embed(
        title="🤖 Your AI Model",
        color=discord.Color.blurple(),
        description=f"**Current:** {info['label']}\n{info['desc']}",
    )
    embed.add_field(
        name="Available Models",
        value="\n".join(
            f"{'▶ ' if k == current else '  '}`{k}` — {v['label']}"
            for k, v in MODELS.items()
        ),
        inline=False,
    )
    if not MODELS[current]["supports_vision"] and current != "auto":
        embed.set_footer(text="⚠️ This model doesn't support images — image messages will fall back to Gemini.")
    else:
        embed.set_footer(text="Use /setmodel or !setmodel to change  •  Resets on bot restart")
    return embed


# ── Reply-chain context helper ───────────────────────────────────────────────

async def _extract_reply_context(message: discord.Message) -> list[dict]:
    """
    Walk up the Discord reply chain (up to 3 hops) and collect the prior
    exchange as a mini-history list so Jarvis understands the thread.

    Returns a list of {"role": ..., "content": ...} dicts, oldest first,
    ready to be prepended before the current user's message.

    Example:
        UserA: "Jarvis who is Naruto?"
        Jarvis: "Naruto is a fictional ninja..."
        UserB replies to Jarvis: "Which anime?"
        → returns [
            {"role": "user",      "content": "[UserA]: who is Naruto?"},
            {"role": "assistant", "content": "Naruto is a fictional ninja..."},
          ]
    """
    context: list[dict] = []
    MAX_HOPS = 3
    current = message

    for _ in range(MAX_HOPS):
        ref = getattr(current, "reference", None)
        if not ref:
            break
        parent = ref.resolved
        if not isinstance(parent, discord.Message):
            # Not cached — try to fetch it
            try:
                parent = await current.channel.fetch_message(ref.message_id)
            except Exception:
                break

        if parent.author.bot:
            # This is a Jarvis reply — add as assistant turn
            context.insert(0, {"role": "assistant", "content": parent.content})
        else:
            # This is a human message — add as user turn, labelled with their name
            text = parent.content.strip()
            # Strip bot mentions and "jarvis" keyword so it reads cleanly
            text = _MENTION_RE.sub("", text)
            text = _JARVIS_RE.sub("", text).strip(" ,:-")
            label = getattr(parent.author, "display_name", str(parent.author))
            context.insert(0, {"role": "user", "content": f"[{label}]: {text}"})

        current = parent

    return context


# ── DND Check ────────────────────────────────────────────────────────────────
def _dnd_check(ctx: commands.Context) -> bool:
    """Allow commands if DND is off, or if command is whitelisted when DND is on."""
    if not is_dnd(ctx.author.id):
        return True  # DND off, allow all commands
    
    # DND is on — only allow these commands
    allowed = {'settings', 'config', 'dnd'}
    if ctx.command.name in allowed or (ctx.command.aliases and any(a in allowed for a in ctx.command.aliases)):
        return True
    return False


# ── Cog ───────────────────────────────────────────────────────────────────────

class AI(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.reminder_task = asyncio.create_task(self._reminder_loop())

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Apply DND check to all commands in this cog."""
        if not _dnd_check(ctx):
            await ctx.reply("🔕 Do Not Disturb mode is ON. Only `!dnd off`, `!settings`, and `!config` are allowed.")
            return False
        return True

    async def _reminder_loop(self) -> None:
        while True:
            try:
                due = pop_due_reminders()
                now = discord.utils.utcnow().timestamp()
                for user_id, reminder in due:
                    try:
                        user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                        if not user:
                            continue
                        when = reminder.get("when")
                        content = reminder.get("content", "(No content)")
                        embed = discord.Embed(
                            title="⏰ Reminder",
                            description=content,
                            color=discord.Color.blurple(),
                        )
                        if when:
                            embed.add_field(
                                name="Scheduled for",
                                value=f"<t:{int(when)}:R>",
                                inline=False,
                            )
                        await user.send(embed=embed)
                    except discord.Forbidden:
                        pass
                    except Exception as e:
                        print(f"[Reminder] Failed to deliver reminder for {user_id}: {e}")
            except Exception as e:
                print(f"[Reminder] Background reminder loop error: {e}")
            await asyncio.sleep(20)

    # ── /chat ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="chat", description="Chat with Jarvis")
    @app_commands.describe(message="Your message to Jarvis", image="Optional image for Jarvis to analyse")
    async def chat(
        self,
        interaction: discord.Interaction,
        message: str,
        image: discord.Attachment | None = None,
    ):
        await interaction.response.defer(thinking=True)
        if is_new_user(interaction.user.id):
            mark_seen(interaction.user.id)
            await _log_new_user(interaction.user)
            grant_onboarding_bonus(interaction.user.id, ONBOARDING_BONUS)

        if is_ai_rate_limited(interaction.user.id):
            await _offer_ai_limit_reset(interaction.followup.send, interaction.user.id)
            return

        image_b64, media_type = None, None
        if image:
            result = await _fetch_image(image)
            if result:
                image_b64, media_type = result
            else:
                await interaction.followup.send(
                    "⚠️ Unsupported file type or too large (max 20 MB, JPEG/PNG/GIF/WebP).",
                    ephemeral=True,
                )
                return

        reply = await generate_ai_response(
            interaction.user.id, message, interaction.channel_id,
            interaction.guild_id, image_b64, media_type, user=interaction.user,
        )
        await edit_or_send_long_message(interaction, reply, ephemeral=False)

    # ── on_message ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # Skip all chat features if user has DND on
        if is_dnd(message.author.id):
            return

        content = message.content.strip()
        lower   = content.lower()

        mentioned     = self.bot.user in message.mentions
        replied_to_me = (
            message.reference is not None
            and message.reference.resolved is not None
            and isinstance(message.reference.resolved, discord.Message)
            and message.reference.resolved.author == self.bot.user
        )
        named = "jarvis" in lower

        if content.startswith("!"):
            return

        auto_respond = False
        restricted = False
        if message.guild is not None:
            auto_respond = bool(get_setting(f"auto_respond_channel_{message.channel.id}", False))
            restricted = bool(get_setting(f"restrict_channel_{message.channel.id}", False))

        if restricted:
            return

        if not (mentioned or replied_to_me or named or auto_respond):
            return

        # Guild-level ban check — on_message bypasses @bot.check so we must do this manually
        if message.guild:
            try:
                from cogs.admin import _guild_bans
                if message.guild.id in _guild_bans:
                    await safe_reply(message, "🚫 This server has been banned from using Jarvis.")
                    return
            except Exception:
                pass

        if is_bot_banned(message.author.id):
            await safe_reply(message, "🚫 You've been banned from Jarvis. Contact the bot owner if you think this is a mistake.")
            return

        if not await self.bot.is_owner(message.author):
            allowed, t = check_burst_and_maybe_timeout(message.author.id)
            if not allowed:
                await safe_reply(message, 
                    f"⏱️ You have been temporarily blocked from using Jarvis for {int(t)} seconds due to flooding."
                )
                return

            if not check_cooldown(message.author.id):
                await message.add_reaction("⏳")
                return

        if is_new_user(message.author.id):
            grant_onboarding_bonus(message.author.id, ONBOARDING_BONUS)

        claimed, _ = claim_daily_credits(message.author.id, DAILY_CHECKIN_REWARD)
        if claimed:
            # Fire-and-forget — don't block the AI reply pipeline on this send.
            asyncio.create_task(message.channel.send(
                f"{JC_EMOJI} **{message.author.display_name}** claimed their daily check-in bonus: "
                f"**+{DAILY_CHECKIN_REWARD} {JC_NAME}**!",
                delete_after=10,
            ))

        if is_ai_rate_limited(message.author.id):
            await _offer_ai_limit_reset(message.reply, message.author.id)
            return

        # ── Group start ────────────────────────────────────────────────────
        if re.search(r"\b(group|public)\s+conversation\b", lower):
            participants = [u for u in message.mentions if u.id != self.bot.user.id and not u.bot]
            all_ids      = list({message.author.id} | {u.id for u in participants})
            if len(all_ids) < 2:
                await safe_reply(message, 
                    "⚠️ Mention at least one other person to start a group conversation.\n"
                    "**Example:** `Jarvis group conversation @friend`"
                )
                return
            start_group(message.channel.id, all_ids)
            names = " & ".join(
                (message.guild.get_member(uid) or message.author).display_name
                for uid in all_ids
            )
            await safe_reply(message, 
                f"👥 **Group conversation started!**\n"
                f"**Participants:** {names}\n"
                "Your messages to Jarvis in this channel are now shared between you.\n"
                "Say `Jarvis end group` when you're done to return to private mode."
            )
            return

        # ── Add to group ───────────────────────────────────────────────────
        if re.search(r"\badd\b.+\bto\s+group\b", lower) or re.search(r"\badd\b.+\bgroup\b", lower):
            members = get_group_members(message.channel.id)
            if not members:
                await safe_reply(message, "⚠️ There's no active group conversation. Start one first with `Jarvis group conversation @user`.")
                return
            to_add = [u for u in message.mentions if u.id != self.bot.user.id and not u.bot and u.id not in members]
            if not to_add:
                await safe_reply(message, "⚠️ Those users are already in the group, or no valid users were mentioned.")
                return
            active_groups[message.channel.id].update(u.id for u in to_add)
            names = ", ".join(u.display_name for u in to_add)
            total = len(active_groups[message.channel.id])
            await safe_reply(message, f"➕ **{names}** joined the group conversation! ({total} participants total)")
            return

        # ── Remove from group ──────────────────────────────────────────────
        if re.search(r"\bremove\b.+\bfrom\s+group\b", lower) or re.search(r"\bkick\b.+\bgroup\b", lower):
            members = get_group_members(message.channel.id)
            if not members:
                await safe_reply(message, "⚠️ There's no active group conversation in this channel.")
                return
            to_remove = [u for u in message.mentions if u.id != self.bot.user.id and not u.bot and u.id in members]
            if not to_remove:
                await safe_reply(message, "⚠️ Those users aren't in the group, or no valid users were mentioned.")
                return
            for u in to_remove:
                active_groups[message.channel.id].discard(u.id)
            names = ", ".join(u.display_name for u in to_remove)
            total = len(active_groups[message.channel.id])
            if total < 2:
                end_group(message.channel.id)
                await safe_reply(message, f"➖ **{names}** left the group. Not enough participants — group ended. Everyone is back to private mode.")
            else:
                await safe_reply(message, f"➖ **{names}** removed from the group conversation. ({total} participants remaining)")
            return

        # ── End group ──────────────────────────────────────────────────────
        if re.search(r"\b(end|stop)\s+group\b", lower):
            if get_group_members(message.channel.id):
                end_group(message.channel.id)
                await safe_reply(message, "🔒 Group conversation ended. Everyone is back to private mode.")
            else:
                await safe_reply(message, "ℹ️ There's no active group conversation in this channel.")
            return

        # ── Who is this / who is @user ───────────────────────────────────────
        if re.search(r"\bwho\s+is\b|\bwho's\b", lower):
            target = None
            for user in message.mentions:
                if user.id != self.bot.user.id and not user.bot:
                    target = user
                    break
            if target is None and message.reference is not None:
                ref = message.reference.resolved
                if isinstance(ref, discord.Message) and not ref.author.bot:
                    target = ref.author
            if target is not None:
                name = get_preferred_name(target.id) or getattr(target, "display_name", str(target))
                await safe_reply(message, f"That is {name}.")
                return

        # ── Normal message ─────────────────────────────────────────────────
        user_text = _MENTION_RE.sub("", content)
        user_text = _JARVIS_RE.sub("", user_text).strip(" ,:-")

        image_b64, media_type = None, None
        for attachment in message.attachments:
            result = await _fetch_image(attachment)
            if result:
                image_b64, media_type = result
                break

        if not user_text and not image_b64:
            await safe_reply(message, "Yes? What can I help you with?")
            return

        # ── Intent intercept — run real command logic, skip the AI API ────
        # Detect natural-language requests that map to a concrete bot command
        # and respond with the actual embed/data instead of letting the API
        # write a made-up text description of it.
        if not image_b64:
            intercepted = await _try_intent_intercept(message, user_text, self.bot)
            if intercepted:
                return

        if is_new_user(message.author.id):
            mark_seen(message.author.id)
            asyncio.create_task(_log_new_user(message.author))  # fire-and-forget

        # ── Reply-chain merge ──────────────────────────────────────────────
        # When any user replies into an existing Jarvis thread, automatically
        # route all reads/writes to a shared merge_history keyed by the root
        # message of that reply chain. This means User B replying to a
        # User A ↔ Jarvis exchange picks up the full shared context, and both
        # users' subsequent replies continue the same thread seamlessly.
        reply_context: list[dict] = []
        thread_root_id: int | None = None

        if replied_to_me:
            # Walk up the reply chain to find or create the root message ID
            root_id: int | None = None

            # Check if we already know the root for the message being replied to
            ref_msg_id = message.reference.message_id if message.reference else None
            if ref_msg_id:
                root_id = get_reply_root(ref_msg_id)

            if root_id is None:
                # Not cached — walk the chain to find the original human message
                # that started this thread (the one Jarvis first replied to)
                cur = message.reference.resolved if message.reference else None
                hops = 0
                while isinstance(cur, discord.Message) and hops < 10:
                    parent_ref = getattr(cur, "reference", None)
                    if not parent_ref:
                        # cur has no parent — it's a root-level message
                        if not cur.author.bot:
                            root_id = cur.id
                        break
                    parent = parent_ref.resolved
                    if not isinstance(parent, discord.Message):
                        try:
                            parent = await message.channel.fetch_message(parent_ref.message_id)
                        except Exception:
                            break
                    if not parent.author.bot:
                        # parent is human — it could be the root, keep going up
                        root_id = parent.id
                    cur = parent
                    hops += 1

                # Fallback: if we couldn't walk up, use the Jarvis message being
                # replied to as the anchor — still creates a shared history
                if root_id is None and ref_msg_id:
                    root_id = ref_msg_id

            if root_id is not None:
                thread_root_id = root_id
                # Register current message → root so future replies resolve instantly
                register_reply_root(message.id, root_id)

                # Seed the merge history from the reply chain if it's brand new
                if not merge_history[root_id]:
                    seed = await _extract_reply_context(message)
                    if seed:
                        merge_history[root_id].extend(seed)
                        _trim(merge_history[root_id])

        guild_id = message.guild.id if message.guild else None
        _processing_users.add(message.author.id)
        try:
            try:
                async with message.channel.typing():
                    reply = await generate_ai_response(
                        message.author.id, user_text, message.channel.id,
                        guild_id, image_b64, media_type, user=message.author,
                        reply_context=reply_context,
                        thread_root_id=thread_root_id,
                    )
            except Exception as e:
                # Network error (DNS failure, connection timeout, etc.) — proceed without typing indicator
                print(f"[Warning] Failed to show typing indicator: {type(e).__name__}: {e}")
                reply = await generate_ai_response(
                    message.author.id, user_text, message.channel.id,
                    guild_id, image_b64, media_type, user=message.author,
                    reply_context=reply_context,
                    thread_root_id=thread_root_id,
                )
            bot_reply_msgs = await send_long_message(message, reply, ephemeral=False)
        finally:
            _processing_users.discard(message.author.id)
        # Register both the human message and Jarvis's reply → root so that
        # whoever replies next immediately resolves to the correct merge history.
        # For a fresh (non-reply) message, the message itself is the root.
        effective_root = thread_root_id if thread_root_id is not None else message.id
        register_reply_root(message.id, effective_root)
        if bot_reply_msgs:
            register_reply_root(bot_reply_msgs[0].id, effective_root)

    # ── /mylimit & !mylimit ───────────────────────────────────────────────────

    @app_commands.command(name="mylimit", description="Check how many AI messages you have left today")
    async def slash_mylimit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=_build_mylimit_embed(interaction.user.id), ephemeral=True
        )

    @commands.command(name="mylimit")
    async def prefix_mylimit(self, ctx: commands.Context):
        """Check how many AI messages you have left today."""
        await safe_reply(ctx.message, embed=_build_mylimit_embed(ctx.author.id))

    # ── /clearhistory & !clearhistory ─────────────────────────────────────────

    @app_commands.command(name="clearhistory", description="Clear your Jarvis conversation history")
    async def slash_clearhistory(self, interaction: discord.Interaction):
        clear_history(interaction.user.id, interaction.channel_id)
        in_group = bool(get_group_members(interaction.channel_id))
        await interaction.response.send_message(
            f"🧹 {'Group conversation' if in_group else 'Your'} history has been cleared!",
            ephemeral=True,
        )

    @commands.command(name="clearhistory")
    async def prefix_clearhistory(self, ctx: commands.Context):
        """Clear your conversation history with Jarvis."""
        clear_history(ctx.author.id, ctx.channel.id)
        in_group = bool(get_group_members(ctx.channel.id))
        await safe_reply(ctx.message, f"🧹 {'Group conversation' if in_group else 'Your'} history has been cleared!")

    # ── /setmodel & !setmodel ─────────────────────────────────────────────────

    @app_commands.command(name="setmodel", description="Choose which AI model Jarvis uses for your messages")
    @app_commands.describe(model="The model to use for your conversations")
    @app_commands.choices(model=MODEL_CHOICES)
    async def slash_setmodel(self, interaction: discord.Interaction, model: app_commands.Choice[str]):
        set_user_model(interaction.user.id, model.value)
        info = MODELS[model.value]
        await interaction.response.send_message(
            embed=_build_model_embed(interaction.user.id), ephemeral=True
        )

    @commands.command(name="setmodel")
    async def prefix_setmodel(self, ctx: commands.Context, model: str = None):
        """Choose your AI model. Usage: !setmodel <auto|groq|gemini-flash|gemini-lite>"""
        if not model or model not in MODELS:
            keys = ", ".join(f"`{k}`" for k in MODELS)
            await safe_reply(ctx.message, 
                f"**Usage:** `!setmodel <model>`\n"
                f"**Available:** {keys}\n\n"
                + "\n".join(f"**`{k}`** — {v['label']}: {v['desc']}" for k, v in MODELS.items())
            )
            return
        set_user_model(ctx.author.id, model)
        await safe_reply(ctx.message, embed=_build_model_embed(ctx.author.id))

    # ── /mymodel & !mymodel ───────────────────────────────────────────────────

    @app_commands.command(name="mymodel", description="Check which AI model you're currently using")
    async def slash_mymodel(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=_build_model_embed(interaction.user.id), ephemeral=True
        )

    @commands.command(name="mymodel")
    async def prefix_mymodel(self, ctx: commands.Context):
        """Check your current AI model preference."""
        await safe_reply(ctx.message, embed=_build_model_embed(ctx.author.id))

    # ── /mymemory & !mymemory ─────────────────────────────────────────────────

    @app_commands.command(name="mymemory", description="See what Jarvis remembers about you")
    async def slash_mymemory(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        facts = await get_facts(interaction.user.id)
        embed = _build_memory_embed(interaction.user, facts)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @commands.command(name="mymemory")
    async def prefix_mymemory(self, ctx: commands.Context):
        """See what Jarvis remembers about you."""
        facts = await get_facts(ctx.author.id)
        embed = _build_memory_embed(ctx.author, facts)
        await safe_reply(ctx.message, embed=embed)

    # ── /forgetme & !forgetme ─────────────────────────────────────────────────

    @app_commands.command(name="forgetme", description="Make Jarvis forget everything it remembers about you")
    async def slash_forgetme(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        count = await forget_facts(interaction.user.id)
        if count:
            await interaction.followup.send(
                f"🧹 Done — I've forgotten **{count}** thing(s) about you. Fresh start!",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "I don't have anything stored about you yet.", ephemeral=True
            )

    @commands.command(name="forgetme")
    async def prefix_forgetme(self, ctx: commands.Context):
        """Make Jarvis forget everything it remembers about you."""
        count = await forget_facts(ctx.author.id)
        if count:
            await safe_reply(ctx.message, f"🧹 Done — I've forgotten **{count}** thing(s) about you. Fresh start!")
        else:
            await safe_reply(ctx.message, "I don't have anything stored about you yet.")

    # ── /nickname & !nickname ─────────────────────────────────────────────────

    @app_commands.command(name="nickname", description="Tell Jarvis what to call you")
    @app_commands.describe(name="The name Jarvis should use for you")
    async def slash_nickname(self, interaction: discord.Interaction, name: str):
        set_preferred_name(interaction.user.id, name)
        await save_facts(interaction.user.id, [(f"User's name is {name}", "identity")])
        await interaction.response.send_message(f"✅ Got it — I'll call you **{name}**.", ephemeral=True)

    @commands.command(name="nickname")
    async def prefix_nickname(self, ctx: commands.Context, *, name: str = None):
        """Tell Jarvis what to call you."""
        if not name:
            await safe_reply(ctx.message, "**Usage:** `!nickname <name>`")
            return
        set_preferred_name(ctx.author.id, name)
        await save_facts(ctx.author.id, [(f"User's name is {name}", "identity")])
        await safe_reply(ctx.message, f"✅ Got it — I'll call you **{name}**.")

    # ── /dnd & !dnd (Do Not Disturb) ──────────────────────────────────────────

    @app_commands.command(name="dnd", description="Toggle Do Not Disturb mode (blocks most commands)")
    @app_commands.describe(mode="on or off")
    async def slash_dnd(self, interaction: discord.Interaction, mode: str):
        if mode.lower() not in {"on", "off"}:
            await interaction.response.send_message("❌ Use `on` or `off`.", ephemeral=True)
            return
        is_on = mode.lower() == "on"
        set_dnd(interaction.user.id, is_on)
        status = "🔕 **ON**" if is_on else "🔔 **OFF**"
        msg = (
            f"{status}\n"
            f"When DND is **ON**, only these commands work:\n"
            f"`!dnd off` — turn DND off\n"
            f"`!settings` / `!config` — view channel settings"
        ) if is_on else f"{status}\n All commands are now available again."
        await interaction.response.send_message(msg, ephemeral=True)

    @commands.command(name="dnd")
    async def prefix_dnd(self, ctx: commands.Context, mode: str = None):
        """Toggle Do Not Disturb mode. Usage: !dnd on/off"""
        if mode is None:
            # Show current status
            is_on = is_dnd(ctx.author.id)
            status = "🔕 **ON**" if is_on else "🔔 **OFF**"
            await safe_reply(ctx.message, f"Do Not Disturb: {status}")
            return
        
        if mode.lower() not in {"on", "off"}:
            await safe_reply(ctx.message, "❌ Use `on` or `off`.")
            return
        
        is_on = mode.lower() == "on"
        set_dnd(ctx.author.id, is_on)
        status = "🔕 **ON**" if is_on else "🔔 **OFF**"
        msg = (
            f"{status}\n"
            f"When DND is **ON**, only these commands work:\n"
            f"`!dnd off` — turn DND off\n"
            f"`!settings` / `!config` — view channel settings"
        ) if is_on else f"{status}\nAll commands are now available again."
        await safe_reply(ctx.message, msg)

    # ── /remindme & !remindme ────────────────────────────────────────────────

    @app_commands.command(name="remindme", description="Set a DM reminder for later")
    @app_commands.describe(duration="When to remind me (e.g. 10m, 1h, 2d)", message="What should I remind you about")
    async def slash_remindme(self, interaction: discord.Interaction, duration: str, message: str):
        seconds = _parse_relative_time(duration)
        if seconds is None:
            await interaction.response.send_message(
                "❌ Invalid time format. Use something like `10m`, `1h`, or `2h 30m`.",
                ephemeral=True,
            )
            return
        when = time.time() + seconds
        reminder_id = add_reminder(interaction.user.id, when, message)
        await interaction.response.send_message(
            f"✅ Reminder set for <t:{int(when)}:R> (ID **{reminder_id}**).",
            ephemeral=True,
        )

    @commands.command(name="remindme")
    async def prefix_remindme(self, ctx: commands.Context, duration: str = None, *, message: str = None):
        """Set a DM reminder. Usage: !remindme 10m Take a break"""
        if not duration or not message:
            await safe_reply(ctx.message, "**Usage:** `!remindme <duration> <message>`\nExample: `!remindme 15m Take a break`")
            return
        seconds = _parse_relative_time(duration)
        if seconds is None:
            await safe_reply(ctx.message, "❌ Invalid time format. Use something like `10m`, `1h`, or `2h 30m`.")
            return
        when = time.time() + seconds
        reminder_id = add_reminder(ctx.author.id, when, message)
        await safe_reply(ctx.message, f"✅ Reminder set for <t:{int(when)}:R> (ID **{reminder_id}**).")

    # ── /myreminders & !myreminders ───────────────────────────────────────────

    @app_commands.command(name="myreminders", description="View your active reminders")
    async def slash_myreminders(self, interaction: discord.Interaction):
        reminders = get_reminders(interaction.user.id)
        embed = _build_reminders_embed(interaction.user, reminders)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.command(name="myreminders")
    async def prefix_myreminders(self, ctx: commands.Context):
        """View your active reminders."""
        reminders = get_reminders(ctx.author.id)
        embed = _build_reminders_embed(ctx.author, reminders)
        await safe_reply(ctx.message, embed=embed)

    # ── /cancelreminder & !cancelreminder ────────────────────────────────────

    @app_commands.command(name="cancelreminder", description="Cancel one of your reminders")
    @app_commands.describe(reminder_id="The ID of the reminder to cancel")
    async def slash_cancelreminder(self, interaction: discord.Interaction, reminder_id: int):
        if delete_reminder(interaction.user.id, reminder_id):
            await interaction.response.send_message(f"✅ Reminder **{reminder_id}** cancelled.", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"❌ I couldn't find reminder **{reminder_id}**.", ephemeral=True,
            )

    @commands.command(name="cancelreminder")
    async def prefix_cancelreminder(self, ctx: commands.Context, reminder_id: int = None):
        """Cancel one of your reminders. Usage: !cancelreminder <id>"""
        if reminder_id is None:
            await safe_reply(ctx.message, "**Usage:** `!cancelreminder <id>`")
            return
        if delete_reminder(ctx.author.id, reminder_id):
            await safe_reply(ctx.message, f"✅ Reminder **{reminder_id}** cancelled.")
        else:
            await safe_reply(ctx.message, f"❌ I couldn't find reminder **{reminder_id}**.")

    # ── Server / member event listeners ──────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await _log_guild(guild, joined=True)

    # ── Backfill command (owner-only) ─────────────────────────────────────────

    @commands.command(name="backfill_servers")
    @commands.is_owner()
    async def backfill_servers(self, ctx: commands.Context) -> None:
        """Send one log embed for every server the bot is already in."""
        if not os.getenv("SERVER_LOG_WEBHOOK_URL", ""):
            await ctx.reply("❌ `SERVER_LOG_WEBHOOK_URL` is not set in your `.env`.")
            return
        guilds = list(self.bot.guilds)
        total  = len(guilds)
        status = await ctx.reply(f"📋 Backfilling **{total}** server(s)…")
        for i, guild in enumerate(guilds, 1):
            await _log_guild(guild, joined=True, index=i, total=total)
            await asyncio.sleep(2.0)   # stay under webhook rate limit
        await status.edit(content=f"✅ Done — sent **{total}** server log(s) to the webhook.")


def _build_memory_embed(user: discord.User | discord.Member, facts: list[str]) -> discord.Embed:
    if not facts:
        embed = discord.Embed(
            title="🧠 My memory about you",
            description="I don't remember anything about you yet — just chat and I'll pick up important things naturally.",
            color=discord.Color.blurple(),
        )
    else:
        lines = "\n".join(f"• {f}" for f in facts)
        embed = discord.Embed(
            title="🧠 My memory about you",
            description=lines,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{len(facts)} thing(s) stored • Use /forgetme to clear all")
    embed.set_thumbnail(url=user.display_avatar.url)
    return embed


def _build_reminders_embed(user: discord.User | discord.Member, reminders: list[dict[str, object]]) -> discord.Embed:
    if not reminders:
        embed = discord.Embed(
            title="⏰ Your reminders",
            description="You don't have any reminders yet. Use `/remindme` or `!remindme` to set one.",
            color=discord.Color.blurple(),
        )
    else:
        lines = []
        for reminder in sorted(reminders, key=lambda r: r.get("when", 0)):
            when = reminder.get("when")
            when_str = f"<t:{int(when)}:R>" if when else "Unknown time"
            content = reminder.get("content", "(No content)")
            lines.append(f"**{reminder.get('id')}** — {when_str}\n{content}")
        embed = discord.Embed(
            title="⏰ Your reminders",
            description="\n\n".join(lines),
            color=discord.Color.blurple(),
        )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text="Use /cancelreminder <id> or !cancelreminder <id> to remove one.")
    return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(AI(bot))