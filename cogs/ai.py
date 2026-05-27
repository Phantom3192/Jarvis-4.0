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
)
from cogs.message_splitter import send_long_message, edit_or_send_long_message
from cogs.http_session import get_session

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

GROQ_MODEL_TEXT    = "llama-3.3-70b-versatile"
GROQ_MODEL_VISION  = "meta-llama/llama-4-scout-17b-16e-instruct"
GEMINI_MODEL_FLASH = "gemini-2.0-flash"
GEMINI_MODEL_LITE  = "gemini-2.0-flash-lite"

HISTORY_LIMIT = 5   # Reduced for ultra-fast API responses
MAX_TOKENS    = 400 # Snappier responses — still ~2 solid paragraphs
PROVIDER_TIMEOUT = 15  # seconds per provider call — increased from 8s to handle Groq slowness
GROQ_RETRIES  = 2   # how many times to retry Groq on timeout before giving up

# ── Available models for /setmodel ───────────────────────────────────────────
# key        → internal identifier stored per user
# "label"    → shown in Discord UI
# "desc"     → shown in the info embed
# "supports_vision" → whether this model can handle image attachments

MODELS: dict[str, dict] = {
    "auto": {
        "label":           "🔄 Auto (default)",
        "desc":            "Tries Groq Llama first, falls back to Gemini automatically.",
        "supports_vision": True,
    },
    "groq": {
        "label":           "⚡ Groq — Llama 3.3 70B",
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
    "Keep responses concise and helpful. Do not start sentences with someone's name. Avoid unnecessary filler."
)

SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES       = 20 * 1024 * 1024
AI_UNAVAILABLE_MSG    = "⚠️ AI providers are currently unavailable. Please try again in a few minutes."

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
    print(f"✅ Groq pool: {len(_groq_clients)} key(s) loaded")
else:
    print("⚠️  No GROQ_API_KEY* found — Groq disabled")

def _groq_is_backed_off(api_key: str) -> bool:
    return time.time() < _groq_backoff_until.get(api_key, 0)

def _groq_set_backoff(api_key: str) -> None:
    _groq_backoff_until[api_key] = time.time() + _GROQ_BACKOFF
    print(f"[Groq] Key …{api_key[-6:]} backed off for {_GROQ_BACKOFF}s")

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
    print("⚠️ Warning: No supported Google GenAI package available. Gemini will be disabled.")
else:
    print(f"✅ Using Google GenAI package: {_GENAI_PACKAGE}")

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
_system_prompt_cache: dict[tuple, str] = {}  # Cache system prompts to avoid reconstruction
_response_cache: dict[tuple, tuple[str, float]] = {}  # Cache responses for 60s (message_hash, timestamp, response)


def _is_in_group(user_id: int, channel_id: int) -> bool:
    return channel_id in active_groups and user_id in active_groups[channel_id]

def _get_history(user_id: int, channel_id: int) -> list[dict]:
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

def clear_history(user_id: int, channel_id: int | None = None) -> None:
    if channel_id and _is_in_group(user_id, channel_id):
        group_history[channel_id].clear()
    else:
        private_history[user_id].clear()

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
                else:
                    await asyncio.sleep(1)
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
    deadline = asyncio.get_event_loop().time() + 20  # overall timeout

    while remaining:
        time_left = deadline - asyncio.get_event_loop().time()
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


# ── Core response ─────────────────────────────────────────────────────────────

async def generate_ai_response(
    user_id: int,
    user_message: str,
    channel_id: int,
    guild_id: int | None = None,
    image_b64: str | None = None,
    media_type: str | None = None,
    user: discord.User | discord.Member | None = None,
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

    # Quick cache check — if same message asked within 60s, return cached response
    if not image_b64:  # Only cache text responses
        cache_key = (user_id, channel_id, user_message)
        if cache_key in _response_cache:
            cached_response, cache_time = _response_cache[cache_key]
            if time.time() - cache_time < 60:  # Cache for 60 seconds
                return cached_response

    base_prompt = get_guild_prompt(guild_id) or DEFAULT_SYSTEM_PROMPT
    in_group    = _is_in_group(user_id, channel_id)

    if user:
        name     = user.display_name if user.display_name != user.name else user.name
        username = user.name
        if in_group:
            n = len(active_groups[channel_id])
            cache_key = (user_id, "group", n)
            if cache_key not in _system_prompt_cache:
                _system_prompt_cache[cache_key] = (
                    base_prompt +
                    f"\n\nThis is a shared group conversation between {n} people. "
                    f"Messages are prefixed with the sender's name so you know who said what. "
                    f"The person who just sent this message is {name} (username: {username}). "
                    f"Only mention their name/username if they directly ask about their own identity."
                )
            system_prompt = _system_prompt_cache[cache_key]
        else:
            cache_key = (user_id, "private")
            if cache_key not in _system_prompt_cache:
                _system_prompt_cache[cache_key] = (
                    base_prompt +
                    f"\n\nThe person messaging you is {name} (username: {username}). "
                    f"Only reveal this if they directly ask who they are. "
                    f"Never volunteer their name or username unprompted."
                )
            system_prompt = _system_prompt_cache[cache_key]
    else:
        system_prompt = base_prompt

    history  = _get_history(user_id, channel_id)
    has_image = bool(image_b64 and media_type)

    stored = (
        f"[{user.display_name}]: {user_message or '(sent an image)'}"
        if (in_group and user)
        else (user_message or "(sent an image)")
    )
    history.append({"role": "user", "content": stored})
    _trim(history)

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
        # "groq" or "auto" — Groq first (fastest), Gemini only as fallback
        if has_image:
            reply = await _try_groq_vision(history, system_prompt, image_b64, media_type, user_message)
        else:
            reply = await _try_groq(history, system_prompt)

        # Groq failed — try Gemini Flash + Lite together across both keys
        if not reply and genai is not None:
            tasks = []
            for key in filter(None, [GEMINI_API_KEY, GEMINI_API_KEY_2]):
                if not _gemini_is_backed_off(key):
                    tasks.append(asyncio.create_task(_try_gemini(key, GEMINI_MODEL_FLASH, history, system_prompt, image_b64, media_type)))
                    tasks.append(asyncio.create_task(_try_gemini(key, GEMINI_MODEL_LITE,  history, system_prompt, image_b64, media_type)))
            if tasks:
                reply = await _race_providers(*tasks)

    if not reply:
        history.pop()
        print(f"[AI Response] All providers failed for user {user_id} in channel {channel_id}")
        return AI_UNAVAILABLE_MSG

    history.append({"role": "assistant", "content": reply})
    
    # Only trim if necessary (avoid unnecessary list operations)
    if len(history) > HISTORY_LIMIT:
        _trim(history)
    
    # Fire-and-forget: record to DB in background, don't block response
    asyncio.create_task(asyncio.to_thread(_background_record, user_id, user_message, reply))

    new_count = increment_ai_usage(user_id)
    if new_count == WARN_AT:
        limit = get_ai_limit()
        remaining = limit - new_count
        reply += f"\n\n{WARN_LIMIT_MSG.format(count=new_count, limit=limit, remaining=remaining)}"

    # Cache text responses for 60 seconds (skip if has image)
    if not image_b64:
        cache_key = (user_id, channel_id, user_message)
        _response_cache[cache_key] = (reply, time.time())

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


# ── Cog ───────────────────────────────────────────────────────────────────────

class AI(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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

        if not (mentioned or replied_to_me or named):
            return

        # Guild-level ban check — on_message bypasses @bot.check so we must do this manually
        if message.guild:
            try:
                from cogs.admin import _guild_bans
                if message.guild.id in _guild_bans:
                    await message.reply("🚫 This server has been banned from using Jarvis.")
                    return
            except Exception:
                pass

        if is_bot_banned(message.author.id):
            await message.reply("🚫 You've been banned from Jarvis. Contact the bot owner if you think this is a mistake.")
            return

        if not await self.bot.is_owner(message.author):
            allowed, t = check_burst_and_maybe_timeout(message.author.id)
            if not allowed:
                await message.reply(
                    f"⏱️ You have been temporarily blocked from using Jarvis for {int(t)} seconds due to flooding."
                )
                return

            if not check_cooldown(message.author.id):
                await message.add_reaction("⏳")
                return

        # ── Group start ────────────────────────────────────────────────────
        if re.search(r"\b(group|public)\s+conversation\b", lower):
            participants = [u for u in message.mentions if u.id != self.bot.user.id and not u.bot]
            all_ids      = list({message.author.id} | {u.id for u in participants})
            if len(all_ids) < 2:
                await message.reply(
                    "⚠️ Mention at least one other person to start a group conversation.\n"
                    "**Example:** `Jarvis group conversation @friend`"
                )
                return
            start_group(message.channel.id, all_ids)
            names = " & ".join(
                (message.guild.get_member(uid) or message.author).display_name
                for uid in all_ids
            )
            await message.reply(
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
                await message.reply("⚠️ There's no active group conversation. Start one first with `Jarvis group conversation @user`.")
                return
            to_add = [u for u in message.mentions if u.id != self.bot.user.id and not u.bot and u.id not in members]
            if not to_add:
                await message.reply("⚠️ Those users are already in the group, or no valid users were mentioned.")
                return
            active_groups[message.channel.id].update(u.id for u in to_add)
            names = ", ".join(u.display_name for u in to_add)
            total = len(active_groups[message.channel.id])
            await message.reply(f"➕ **{names}** joined the group conversation! ({total} participants total)")
            return

        # ── Remove from group ──────────────────────────────────────────────
        if re.search(r"\bremove\b.+\bfrom\s+group\b", lower) or re.search(r"\bkick\b.+\bgroup\b", lower):
            members = get_group_members(message.channel.id)
            if not members:
                await message.reply("⚠️ There's no active group conversation in this channel.")
                return
            to_remove = [u for u in message.mentions if u.id != self.bot.user.id and not u.bot and u.id in members]
            if not to_remove:
                await message.reply("⚠️ Those users aren't in the group, or no valid users were mentioned.")
                return
            for u in to_remove:
                active_groups[message.channel.id].discard(u.id)
            names = ", ".join(u.display_name for u in to_remove)
            total = len(active_groups[message.channel.id])
            if total < 2:
                end_group(message.channel.id)
                await message.reply(f"➖ **{names}** left the group. Not enough participants — group ended. Everyone is back to private mode.")
            else:
                await message.reply(f"➖ **{names}** removed from the group conversation. ({total} participants remaining)")
            return

        # ── End group ──────────────────────────────────────────────────────
        if re.search(r"\b(end|stop)\s+group\b", lower):
            if get_group_members(message.channel.id):
                end_group(message.channel.id)
                await message.reply("🔒 Group conversation ended. Everyone is back to private mode.")
            else:
                await message.reply("ℹ️ There's no active group conversation in this channel.")
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
            await message.reply("Yes? What can I help you with?")
            return

        if is_new_user(message.author.id):
            mark_seen(message.author.id)
            await _log_new_user(message.author)

        guild_id = message.guild.id if message.guild else None
        try:
            async with message.channel.typing():
                reply = await generate_ai_response(
                    message.author.id, user_text, message.channel.id,
                    guild_id, image_b64, media_type, user=message.author,
                )
        except Exception as e:
            # Network error (DNS failure, connection timeout, etc.) — proceed without typing indicator
            print(f"[Warning] Failed to show typing indicator: {type(e).__name__}: {e}")
            reply = await generate_ai_response(
                message.author.id, user_text, message.channel.id,
                guild_id, image_b64, media_type, user=message.author,
            )
        await send_long_message(message, reply, ephemeral=False)

    # ── /mylimit & !mylimit ───────────────────────────────────────────────────

    @app_commands.command(name="mylimit", description="Check how many AI messages you have left today")
    async def slash_mylimit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=_build_mylimit_embed(interaction.user.id), ephemeral=True
        )

    @commands.command(name="mylimit")
    async def prefix_mylimit(self, ctx: commands.Context):
        """Check how many AI messages you have left today."""
        await ctx.reply(embed=_build_mylimit_embed(ctx.author.id))

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
        await ctx.reply(f"🧹 {'Group conversation' if in_group else 'Your'} history has been cleared!")

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
            await ctx.reply(
                f"**Usage:** `!setmodel <model>`\n"
                f"**Available:** {keys}\n\n"
                + "\n".join(f"**`{k}`** — {v['label']}: {v['desc']}" for k, v in MODELS.items())
            )
            return
        set_user_model(ctx.author.id, model)
        await ctx.reply(embed=_build_model_embed(ctx.author.id))

    # ── /mymodel & !mymodel ───────────────────────────────────────────────────

    @app_commands.command(name="mymodel", description="Check which AI model you're currently using")
    async def slash_mymodel(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=_build_model_embed(interaction.user.id), ephemeral=True
        )

    @commands.command(name="mymodel")
    async def prefix_mymodel(self, ctx: commands.Context):
        """Check your current AI model preference."""
        await ctx.reply(embed=_build_model_embed(ctx.author.id))


async def setup(bot: commands.Bot):
    await bot.add_cog(AI(bot))