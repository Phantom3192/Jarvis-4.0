"""
Shared mutable state across cogs — backed by Turso (LibSQL).
Stores bans, seen users, stats, guild prompts, and rate limits.
All data persists across restarts via Turso.
Bot runs fine in memory-only mode if TURSO_URL/TOKEN are not set.

OPTIMISATIONS vs original:
- _debounced_save: replaced if/elif chain with a lookup dict → O(1) dispatch
- get_ai_usage / increment_ai_usage: merged duplicate reset logic into one helper
- _today_utc: cached at module-level with a 1-second TTL to avoid repeated
  datetime calls on every message (cheap but adds up at scale)
- _BanProxy / _SeenProxy: added missing dunder methods (__repr__, update) so
  they behave more like the built-in types they proxy
- Type annotations tightened throughout (no bare `dict` / `set` on proxies)
- Removed unused `json` import alias (already imported at top)
"""
import os
import time
import asyncio
import json
from collections import deque
from datetime import datetime, timezone
from typing import Any

from cogs.turso_db import TursoConnection

_db: TursoConnection | None = None  # set in init_db()

# ── In-memory mirrors ─────────────────────────────────────────────────────────

_data: dict[str, Any] = {
    "bans":           {},    # str(user_id) → {"reason": str, "expires": float|None}
    "seen":           set(), # set of int user_ids
    "stats":          {},    # str(user_id) → {"messages", "tokens_est", "first_seen", "last_seen"}
    "prompts":        {},    # str(guild_id) → prompt string
    "rate_limits":    {},    # str(user_id)  → {"count": int, "day": "YYYY-MM-DD"}
    "settings":       {},    # arbitrary bot settings persisted (e.g. cooldowns)
    "preferred_names": {},   # str(user_id) → preferred display name
    "reminders":      {},    # str(user_id) → list of reminder objects
    "playlists":      {},    # str(user_id) -> {playlist_name: [track_info, ...]}
    "song_history":   {},    # str(user_id) -> [track_info, ...]
    "guild_bans":     {},    # str(guild_id) → {"reason": str, "banned_at": float}
    "credits":        {},    # str(user_id) → int balance of Jarvis Credits (JC)
    "credit_meta":    {},    # str(user_id) → {"last_daily": "YYYY-MM-DD", "chat_day": "YYYY-MM-DD", "chat_count": int, "streak": int, "last_streak_day": "YYYY-MM-DD", "streak_milestones": [int, ...]}
    "guild_logs":     {},    # str(guild_id) → {"name": str, "joined_at": float, "member_count": int, "owner_id": int}
    "referral_codes": {},    # str(user_id) → str code (each user's own stable invite code)
    "referred_by":    {},    # str(user_id) → referrer's user_id (int). Presence = "already redeemed a code".
    "dnd_users":      {},    # str(user_id) → True (presence = DND enabled)
    "game_stats":     {},    # str(user_id) → {"chess_wins","chess_losses","mafia_wins","mafia_losses","hangman_wins"}
    "songs_played":   {},    # str(user_id) → int, lifetime count of songs played (not trimmed like song_history)
    "badges":         {},    # str(user_id) → list[str] of unlocked achievement badge ids
    "titles":         {},    # str(user_id) → {"owned": [title_id,...], "equipped": title_id|None}
    "banners":        {},    # str(user_id) → {"owned": [banner_id,...], "equipped": banner_id|None}
    "banner_borders": {},    # str(user_id) → {"owned": [border_id,...], "equipped": border_id|None}
    "title_subscriptions": {}, # str(user_id) → {title_id: next_charge_epoch}
    "profile_hidden": {},    # str(user_id) → list[str] of /profile section keys the user has hidden
}

# Serialisers for each key (avoids if/elif chain in _debounced_save)
_SERIALISE: dict[str, Any] = {
    "bans":            lambda: _data["bans"],
    "seen":            lambda: [str(uid) for uid in _data["seen"]],
    "stats":           lambda: _data["stats"],
    "prompts":         lambda: _data["prompts"],
    "rate_limits":     lambda: _data["rate_limits"],
    "settings":        lambda: _data["settings"],
    "preferred_names": lambda: _data["preferred_names"],
    "reminders":       lambda: _data["reminders"],
    "playlists":       lambda: _data["playlists"],
    "song_history":    lambda: _data["song_history"],
    "guild_bans":      lambda: _data["guild_bans"],
    "credits":         lambda: _data["credits"],
    "credit_meta":     lambda: _data["credit_meta"],
    "guild_logs":      lambda: _data["guild_logs"],
    "referral_codes":  lambda: _data["referral_codes"],
    "referred_by":     lambda: _data["referred_by"],
    "dnd_users":       lambda: _data["dnd_users"],
    "game_stats":      lambda: _data["game_stats"],
    "songs_played":    lambda: _data["songs_played"],
    "badges":          lambda: _data["badges"],
    "titles":          lambda: _data["titles"],
    "banners":         lambda: _data["banners"],
    "banner_borders":  lambda: _data["banner_borders"],
    "title_subscriptions": lambda: _data["title_subscriptions"],
    "profile_hidden":  lambda: _data["profile_hidden"],
}


# ── DB init ───────────────────────────────────────────────────────────────────



async def init_db():
    """Call once at startup. Connects to Turso and loads all state into memory."""
    global _db

    turso_url   = os.getenv("TURSO_URL",   "").strip().lstrip("=").strip()
    turso_token = os.getenv("TURSO_TOKEN", "").strip().lstrip("=").strip()

    if not turso_url or not turso_token:
        print(
            "⚠️  TURSO_URL or TURSO_TOKEN not set.\n"
            "   Jarvis will run in memory-only mode — all data will be lost on restart.\n"
            "   Add TURSO_URL and TURSO_TOKEN to your .env to persist data."
        )
        return

    def _ensure_table():
        _db.conn.execute("""
            CREATE TABLE IF NOT EXISTS state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        _db.conn.commit()

    _db = TursoConnection("State", turso_url, turso_token, init_fn=_ensure_table)
    connected = await _db.connect_async()
    if not connected:
        print("❌ Turso state DB connection failed — Jarvis will run in memory-only mode.")
        _db = None
        return

    rows = await _db.run(
        lambda: _db.conn.execute("SELECT key, value FROM state").fetchall(),
        default=[],
    )
    db = {row[0]: json.loads(row[1]) for row in rows}

    if "bans"           in db: _data["bans"]           = db["bans"]
    if "seen"           in db: _data["seen"]           = set(int(uid) for uid in db["seen"])
    if "stats"          in db: _data["stats"]          = db["stats"]
    if "prompts"        in db: _data["prompts"]        = db["prompts"]
    if "settings"       in db: _data["settings"]       = db["settings"]
    if "preferred_names" in db: _data["preferred_names"] = db["preferred_names"]
    if "reminders"      in db: _data["reminders"]      = db["reminders"]
    if "playlists"      in db: _data["playlists"]      = db["playlists"]
    if "song_history"   in db: _data["song_history"]   = db["song_history"]
    if "rate_limits"    in db: _data["rate_limits"]    = db["rate_limits"]
    if "guild_bans"     in db: _data["guild_bans"]     = db["guild_bans"]
    if "credits"        in db: _data["credits"]        = db["credits"]
    if "credit_meta"    in db: _data["credit_meta"]    = db["credit_meta"]
    if "guild_logs"     in db: _data["guild_logs"]     = db["guild_logs"]
    if "referral_codes" in db: _data["referral_codes"] = db["referral_codes"]
    if "referred_by"    in db: _data["referred_by"]    = db["referred_by"]
    if "dnd_users"      in db: _data["dnd_users"]       = db["dnd_users"]
    if "game_stats"     in db: _data["game_stats"]      = db["game_stats"]
    if "songs_played"   in db: _data["songs_played"]    = db["songs_played"]
    if "badges"         in db: _data["badges"]          = db["badges"]
    if "titles"         in db: _data["titles"]          = db["titles"]
    if "banners"        in db: _data["banners"]         = db["banners"]
    if "banner_borders" in db: _data["banner_borders"]  = db["banner_borders"]
    if "title_subscriptions" in db: _data["title_subscriptions"] = db["title_subscriptions"]
    if "profile_hidden" in db: _data["profile_hidden"]  = db["profile_hidden"]

    print("✅ Turso state DB connected")
    asyncio.create_task(_db.keepalive_loop())


# ── Save helpers ──────────────────────────────────────────────────────────────

async def _save_key(key: str, value: Any) -> None:
    """Upsert a single key into the state table. Auto-reconnect, retry,
    and off-event-loop execution are all handled by TursoConnection.run() —
    this can never raise or stall the event loop."""
    if _db is None:
        return

    def _do_save():
        _db.conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value))
        )
        _db.conn.commit()

    await _db.run(_do_save)


# ── Debounced save ────────────────────────────────────────────────────────────

_save_tasks: dict[str, asyncio.Task] = {}

def _schedule_save(key: str) -> None:
    """Debounce saves — waits 2 s after last change before writing."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = _save_tasks.get(key)
    if task and not task.done():
        task.cancel()
    _save_tasks[key] = loop.create_task(_debounced_save(key))

async def _debounced_save(key: str, delay: float = 2.0) -> None:
    await asyncio.sleep(delay)
    serialiser = _SERIALISE.get(key)
    if serialiser:
        try:
            loop = asyncio.get_running_loop()
            if loop.is_closed() or not loop.is_running():
                return
            await _save_key(key, serialiser())
        except Exception as e:
            # Last-resort guard: a debounced save must never crash its Task
            # silently and must never raise into the event loop's task runner.
            print(f"❌ Unexpected error in debounced save ({key}): {e}")


async def flush_all_saves() -> None:
    """Immediately persist every key with a pending (not-yet-fired) debounced
    save, skipping the normal 2s wait. Call this from a SIGTERM/SIGINT
    handler before the process actually exits — otherwise any change made
    in the last ~2s before a restart/redeploy is silently lost, since
    platforms like Railway send SIGTERM and Python doesn't run pending
    asyncio tasks or `finally` blocks for that signal on its own.
    """
    if _db is None:
        return

    pending = [key for key, task in _save_tasks.items() if task and not task.done()]
    for key in pending:
        _save_tasks[key].cancel()

    saved = 0
    for key in pending:
        serialiser = _SERIALISE.get(key)
        if not serialiser:
            continue
        try:
            await _save_key(key, serialiser())
            saved += 1
        except Exception as e:
            print(f"❌ Error flushing '{key}' on shutdown: {e}")

    if saved:
        print(f"✅ Flushed {saved} pending state key(s) before shutdown.")


# ══════════════════════════════════════════════════════════════════════════════
# BAN STATE
# ══════════════════════════════════════════════════════════════════════════════

class _BanProxy(dict):
    """Proxy so existing cog code (bot_bans[x] = y, del bot_bans[x]) still works."""
    def __contains__(self, key):        return key in _data["bans"]
    def __getitem__(self, key):         return _data["bans"][key]
    def __setitem__(self, key, value):
        _data["bans"][key] = value
        _schedule_save("bans")
    def __delitem__(self, key):
        del _data["bans"][key]
        _schedule_save("bans")
    def __iter__(self):                 return iter(_data["bans"])
    def __len__(self):                  return len(_data["bans"])
    def __repr__(self):                 return repr(_data["bans"])
    def get(self, key, default=None):   return _data["bans"].get(key, default)
    def items(self):                    return _data["bans"].items()
    def keys(self):                     return _data["bans"].keys()
    def values(self):                   return _data["bans"].values()
    def update(self, other=(), **kw):
        _data["bans"].update(other, **kw)
        _schedule_save("bans")
    def pop(self, key, *args):
        val = _data["bans"].pop(key, *args)
        _schedule_save("bans")
        return val

bot_bans: dict = _BanProxy()

def save_bans() -> None:
    _schedule_save("bans")

def is_bot_banned(user_id: int) -> bool:
    uid  = str(user_id)
    ban  = _data["bans"].get(uid)
    if not ban:
        return False
    expires = ban.get("expires")
    if expires is not None and time.time() >= expires:
        # Temp ban expired — remove it automatically
        del _data["bans"][uid]
        _schedule_save("bans")
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
# SEEN USERS
# ══════════════════════════════════════════════════════════════════════════════

class _SeenProxy(set):
    def __contains__(self, item): return item in _data["seen"]
    def __iter__(self):           return iter(_data["seen"])
    def __len__(self):            return len(_data["seen"])
    def __repr__(self):           return repr(_data["seen"])
    def add(self, item):
        if item not in _data["seen"]:
            _data["seen"].add(item)
            _schedule_save("seen")

seen_users: set = _SeenProxy()

def mark_seen(user_id: int) -> None:
    if user_id not in _data["seen"]:
        _data["seen"].add(user_id)
        _schedule_save("seen")

def is_new_user(user_id: int) -> bool:
    return user_id not in _data["seen"]


# ══════════════════════════════════════════════════════════════════════════════
# STATS
# ══════════════════════════════════════════════════════════════════════════════

def record_message(user_id: int, user_text: str, reply_text: str) -> None:
    uid    = str(user_id)
    now    = time.time()
    tokens = (len(user_text) + len(reply_text)) // 4
    stats  = _data["stats"]
    if uid not in stats:
        stats[uid] = {
            "messages":   0,
            "tokens_est": 0,
            "first_seen": now,
            "last_seen":  now,
        }
    s = stats[uid]
    s["messages"]   += 1
    s["tokens_est"] += tokens
    s["last_seen"]   = now
    _schedule_save("stats")

def get_stats(user_id: int) -> dict | None:
    return _data["stats"].get(str(user_id))

def record_image_search(user_id: int) -> None:
    """Bump a user's cumulative !image usage count. Reuses the same 'stats'
    table/persistence as record_message() rather than adding a new table."""
    uid   = str(user_id)
    now   = time.time()
    stats = _data["stats"]
    if uid not in stats:
        stats[uid] = {
            "messages":   0,
            "tokens_est": 0,
            "first_seen": now,
            "last_seen":  now,
        }
    stats[uid]["image_searches"] = stats[uid].get("image_searches", 0) + 1
    _schedule_save("stats")

def get_image_search_count(user_id: int) -> int:
    data = _data["stats"].get(str(user_id))
    return data.get("image_searches", 0) if data else 0

def get_all_stats() -> dict[str, dict]:
    """Return a copy of all user stats, keyed by str(user_id)."""
    return dict(_data["stats"])

def get_all_bans() -> dict[str, dict]:
    """Return a copy of all active bans, keyed by str(user_id)."""
    return dict(_data["bans"])

def get_all_rate_limits() -> dict[str, dict]:
    """Return a copy of all rate limit entries, keyed by str(user_id)."""
    return dict(_data["rate_limits"])


# ══════════════════════════════════════════════════════════════════════════════
# GUILD PROMPTS
# ══════════════════════════════════════════════════════════════════════════════

def get_guild_prompt(guild_id: int | None) -> str | None:
    if guild_id is None:
        return None
    return _data["prompts"].get(str(guild_id))

def set_guild_prompt(guild_id: int, prompt: str) -> None:
    _data["prompts"][str(guild_id)] = prompt
    _schedule_save("prompts")

def reset_guild_prompt(guild_id: int) -> bool:
    uid = str(guild_id)
    if uid in _data["prompts"]:
        del _data["prompts"][uid]
        _schedule_save("prompts")
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# AI RATE LIMITING
# ══════════════════════════════════════════════════════════════════════════════

DAILY_AI_LIMIT = 100
WARN_AT        = 80

# Simple 1-second cache for today's UTC date string — avoids repeated datetime
# formatting on every single AI message.
_today_cache: tuple[float, str] = (0.0, "")

def _today_utc() -> str:
    global _today_cache
    ts = time.time()
    if ts - _today_cache[0] > 1.0:
        _today_cache = (ts, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    return _today_cache[1]


def _reset_entry(uid: str, today: str) -> dict:
    """Return a fresh rate-limit entry and persist it."""
    entry = {"count": 0, "day": today}
    _data["rate_limits"][uid] = entry
    _schedule_save("rate_limits")
    return entry


def get_ai_usage(user_id: int) -> tuple[int, str]:
    uid   = str(user_id)
    today = _today_utc()
    entry = _data["rate_limits"].get(uid)
    if not entry or entry.get("day") != today:
        entry = _reset_entry(uid, today)
    return entry["count"], today

def increment_ai_usage(user_id: int) -> int:
    uid   = str(user_id)
    today = _today_utc()
    entry = _data["rate_limits"].get(uid)
    if not entry or entry.get("day") != today:
        entry = _reset_entry(uid, today)
    entry["count"] += 1
    _schedule_save("rate_limits")
    return entry["count"]

def get_ai_limit() -> int:
    # Enforce the single source of truth for daily AI usage limits.
    return DAILY_AI_LIMIT

def is_ai_rate_limited(user_id: int) -> bool:
    count, _ = get_ai_usage(user_id)
    return count >= get_ai_limit()

def reset_ai_usage(user_id: int) -> None:
    uid = str(user_id)
    _data["rate_limits"][uid] = {"count": 0, "day": _today_utc()}
    _schedule_save("rate_limits")


# ── Generic settings storage ──────────────────────────────────────────────────

# In-memory cache for settings — avoids dict-in-dict lookup on every message.
# Invalidated on every set_setting() call so values are always fresh.
_settings_cache: dict[str, object] = {}

def get_setting(key: str, default=None):
    if key in _settings_cache:
        return _settings_cache[key]
    val = _data.get("settings", {}).get(key, default)
    _settings_cache[key] = val
    return val


def set_setting(key: str, value) -> None:
    if "settings" not in _data:
        _data["settings"] = {}
    _data["settings"][key] = value
    _settings_cache[key] = value  # update cache immediately
    _schedule_save("settings")


def get_preferred_name(user_id: int) -> str | None:
    return _data.get("preferred_names", {}).get(str(user_id))


def set_preferred_name(user_id: int, name: str) -> None:
    if "preferred_names" not in _data:
        _data["preferred_names"] = {}
    _data["preferred_names"][str(user_id)] = name
    _schedule_save("preferred_names")


def clear_preferred_name(user_id: int) -> bool:
    uid = str(user_id)
    if uid in _data.get("preferred_names", {}):
        del _data["preferred_names"][uid]
        _schedule_save("preferred_names")
        return True
    return False


def get_reminders(user_id: int) -> list[dict[str, object]]:
    return list(_data.get("reminders", {}).get(str(user_id), []))


def add_reminder(user_id: int, when: float, content: str) -> int:
    if "reminders" not in _data:
        _data["reminders"] = {}
    uid = str(user_id)
    if uid not in _data["reminders"]:
        _data["reminders"][uid] = []
    reminders = _data["reminders"][uid]
    new_id = max((reminder.get("id", 0) for reminder in reminders), default=0) + 1
    reminder = {"id": new_id, "when": when, "content": content}
    reminders.append(reminder)
    _schedule_save("reminders")
    return new_id


def delete_reminder(user_id: int, reminder_id: int) -> bool:
    uid = str(user_id)
    reminders = _data.get("reminders", {}).get(uid)
    if not reminders:
        return False
    new_reminders = [r for r in reminders if r.get("id") != reminder_id]
    if len(new_reminders) == len(reminders):
        return False
    _data["reminders"][uid] = new_reminders
    _schedule_save("reminders")
    return True


def pop_due_reminders(now: float | None = None) -> list[tuple[int, dict[str, object]]]:
    if now is None:
        now = time.time()
    due: list[tuple[int, dict[str, object]]] = []
    for uid_str, reminders in list(_data.get("reminders", {}).items()):
        remaining = []
        for reminder in reminders:
            if reminder.get("when", 0) <= now:
                try:
                    uid = int(uid_str)
                except ValueError:
                    continue
                due.append((uid, reminder))
            else:
                remaining.append(reminder)
        if remaining:
            _data["reminders"][uid_str] = remaining
        else:
            del _data["reminders"][uid_str]
    if due:
        _schedule_save("reminders")
    return due


def get_user_playlists(user_id: int) -> dict[str, list[dict[str, object]]]:
    return _data.get("playlists", {}).get(str(user_id), {})


def set_user_playlist(user_id: int, name: str, tracks: list[dict[str, object]]) -> None:
    if "playlists" not in _data:
        _data["playlists"] = {}
    uid = str(user_id)
    if uid not in _data["playlists"]:
        _data["playlists"][uid] = {}
    _data["playlists"][uid][name] = tracks
    _schedule_save("playlists")


def delete_user_playlist(user_id: int, name: str) -> bool:
    uid = str(user_id)
    if "playlists" not in _data or uid not in _data["playlists"]:
        return False
    if name not in _data["playlists"][uid]:
        return False
    del _data["playlists"][uid][name]
    _schedule_save("playlists")
    return True


def get_song_history(user_id: int, limit: int = 50) -> list[dict[str, object]]:
    history = _data.get("song_history", {}).get(str(user_id), [])
    return history[-limit:]


def set_dnd(user_id: int, enabled: bool) -> None:
    """Enable or disable Do Not Disturb mode for a user."""
    if "dnd_users" not in _data:
        _data["dnd_users"] = {}
    if enabled:
        _data["dnd_users"][str(user_id)] = True
    else:
        _data["dnd_users"].pop(str(user_id), None)
    _schedule_save("dnd_users")


def is_dnd(user_id: int) -> bool:
    """Check if user has DND mode enabled."""
    return str(user_id) in _data.get("dnd_users", {})


def append_song_history(user_id: int, track: dict[str, object], max_items: int = 50) -> None:
    if "song_history" not in _data:
        _data["song_history"] = {}
    uid = str(user_id)
    if uid not in _data["song_history"]:
        _data["song_history"][uid] = []
    history = _data["song_history"][uid]
    history.append(track)
    if len(history) > max_items:
        del history[:-max_items]
    _schedule_save("song_history")
    # Lifetime counter — kept separate from the capped history above so
    # achievement thresholds (e.g. "played 250 songs") stay accurate even
    # after old plays get trimmed off song_history.
    _data["songs_played"][uid] = _data["songs_played"].get(uid, 0) + 1
    _schedule_save("songs_played")


# ══════════════════════════════════════════════════════════════════════════════
# BURST PROTECTION
# ══════════════════════════════════════════════════════════════════════════════

_burst_records: dict[int, deque] = {}


def check_burst_and_maybe_timeout(user_id: int) -> tuple[bool, float | None]:
    now = time.monotonic()
    window = float(get_setting("burst_window_seconds", 60.0))
    limit = int(get_setting("burst_limit_count", 20))
    timeout = float(get_setting("burst_timeout_seconds", 300.0))

    dq = _burst_records.setdefault(user_id, deque())
    dq.append(now)
    cutoff = now - window
    while dq and dq[0] < cutoff:
        dq.popleft()



    if len(dq) >= limit:
        bot_bans[str(user_id)] = {
            "reason": f"Flooding commands ({len(dq)} in {int(window)}s)",
            "expires": time.time() + timeout,
        }
        save_bans()
        dq.clear()
        print(f"[burst] Timed out user {user_id}: {limit} hits (limit={limit}, window={window}s, timeout={timeout}s)")
        return False, timeout

    return True, None


def get_burst_status(user_id: int) -> dict:
    """Live snapshot of a user's current burst-window activity, for display
    purposes only (does not mutate state or trigger a timeout). Useful for
    spotting a user who's ramping up before they actually hit the limit."""
    now    = time.monotonic()
    window = float(get_setting("burst_window_seconds", 60.0))
    limit  = int(get_setting("burst_limit_count", 20))

    dq = _burst_records.get(user_id)
    if not dq:
        count = 0
    else:
        cutoff = now - window
        count = sum(1 for t in dq if t >= cutoff)

    return {
        "count":  count,
        "limit":  limit,
        "window": window,
        "pct":    round((count / limit) * 100) if limit else 0,
    }


# ── Mention spam protection ──────────────────────────────────────────────────

_mention_records: dict[tuple[int, int], deque] = {}

def record_mention(invoker_id: int, target_id: int) -> tuple[bool, float | None]:
    """Record that `invoker_id` caused the bot to mention `target_id`.

    Returns (allowed, timeout_seconds). If allowed is False the invoker has
    been temporarily bot-banned and the timeout value is returned.
    """
    now = time.monotonic()
    window = float(get_setting("mention_window_seconds", 60.0))
    limit = int(get_setting("mention_limit_count", 4))
    timeout = float(get_setting("mention_timeout_seconds", 600.0))

    key = (invoker_id, target_id)
    dq = _mention_records.setdefault(key, deque())
    dq.append(now)
    cutoff = now - window
    while dq and dq[0] < cutoff:
        dq.popleft()

    # Trigger only when more than the configured limit within the window.
    if len(dq) > limit:
        # Temp ban the invoker
        bot_bans[str(invoker_id)] = {
            "reason": f"Mention spamming user {target_id} ({len(dq)} in {int(window)}s)",
            "expires": time.time() + timeout,
        }
        save_bans()
        dq.clear()
        print(f"[mention] Timed out user {invoker_id} for mentioning {target_id}: {len(dq)} hits (limit={limit}, window={window}s, timeout={timeout}s)")
        return False, timeout

    return True, None


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND COOLDOWN
# ══════════════════════════════════════════════════════════════════════════════

_last_command_time: dict[int, float] = {}

def check_cooldown(user_id: int) -> bool:
    """Check if user has waited long enough since last command/message.
    Returns True if cooldown passed, False if still cooling down.
    """
    now = time.monotonic()
    last = _last_command_time.get(user_id)
    cooldown = float(get_setting("user_command_cooldown", 2.0))
    if last is None or (now - last) >= cooldown:
        _last_command_time[user_id] = now
        return True
    return False

# ══════════════════════════════════════════════════════════════════════════════
# JARVIS CREDITS (JC)
# ══════════════════════════════════════════════════════════════════════════════

# Streak length (consecutive days chatted) → JC bonus paid out once that
# length is reached. Kept here (not economy.py) so state.py's bump_streak()
# has no import dependency on the economy cog.
STREAK_MILESTONES: dict[int, int] = {
    7:  200,   # 🔥 7-day streak bonus
    30: 500,   # ⭐ Monthly loyal user
}

def get_all_credits() -> dict[str, int]:
    """Return a copy of the str(user_id) → JC balance mapping."""
    return dict(_data["credits"])


def get_credits(user_id: int) -> int:
    """Return the user's current JC balance."""
    return int(_data["credits"].get(str(user_id), 0))


def add_credits(user_id: int, amount: int) -> int:
    """Add (or subtract, if amount is negative) JC. Balance never goes below 0.
    Returns the new balance."""
    uid = str(user_id)
    bal = _data["credits"].get(uid, 0) + amount
    if bal < 0:
        bal = 0
    _data["credits"][uid] = bal
    _schedule_save("credits")
    return bal


def spend_credits(user_id: int, amount: int) -> bool:
    """Attempt to deduct `amount` JC. Returns False (no-op) if the balance
    is insufficient, True if the deduction succeeded."""
    uid = str(user_id)
    bal = _data["credits"].get(uid, 0)
    if bal < amount:
        return False
    _data["credits"][uid] = bal - amount
    _schedule_save("credits")
    return True


def _credit_meta(user_id: int) -> dict:
    uid = str(user_id)
    meta = _data["credit_meta"].get(uid)
    if not meta:
        meta = {
            "last_daily": "", "chat_day": "", "chat_count": 0,
            "streak": 0, "last_streak_day": "", "streak_milestones": [],
        }
        _data["credit_meta"][uid] = meta
    else:
        # Backfill defaults for users created before streaks/mystery-box existed.
        meta.setdefault("streak", 0)
        meta.setdefault("last_streak_day", "")
        meta.setdefault("streak_milestones", [])
    return meta


def claim_daily_credits(user_id: int, amount: int) -> tuple[bool, int]:
    """Grant the daily JC bonus if the user hasn't claimed it today.
    Returns (claimed, new_balance). claimed=False if already claimed today."""
    today = _today_utc()
    meta = _credit_meta(user_id)
    if meta.get("last_daily") == today:
        return False, get_credits(user_id)
    meta["last_daily"] = today
    _schedule_save("credit_meta")
    new_balance = add_credits(user_id, amount)
    return True, new_balance


def earn_chat_credits(user_id: int, amount: int, daily_cap: int) -> int:
    """Award JC for an AI chat message, up to `daily_cap` JC per day from
    this source. Returns the amount actually awarded (0 if cap reached)."""
    today = _today_utc()
    meta = _credit_meta(user_id)
    if meta.get("chat_day") != today:
        meta["chat_day"] = today
        meta["chat_count"] = 0
    if meta["chat_count"] >= daily_cap:
        return 0
    award = min(amount, daily_cap - meta["chat_count"])
    meta["chat_count"] += award
    _schedule_save("credit_meta")
    add_credits(user_id, award)
    return award


def get_streak(user_id: int) -> int:
    """Return the user's current consecutive daily-chat streak."""
    return int(_credit_meta(user_id).get("streak", 0))


def bump_streak(user_id: int) -> tuple[int, list[int]]:
    """
    Advance the user's daily streak. Call this once per UTC day, at the same
    point `claim_daily_credits` fires (first message of the day) — both rely
    on the same `chat_day`/date-rollover signal, so they always stay in sync.

    Streak rules:
      - Same day as last bump → no-op, streak unchanged.
      - Exactly the day after `last_streak_day` → streak += 1.
      - Any gap (missed a day, or brand new user) → streak resets to 1.

    Returns (new_streak, newly_hit_milestones) where newly_hit_milestones is
    a subset of STREAK_MILESTONES the user just reached *this call* (usually
    empty, sometimes one entry). Milestones only fire once per user ever —
    `streak_milestones` tracks which ones have already been paid out so a
    user who breaks and rebuilds a streak across the same milestone twice
    still gets paid both times (it's cleared on reset, see below).
    """
    today = _today_utc()
    meta = _credit_meta(user_id)

    if meta["last_streak_day"] == today:
        return meta["streak"], []  # already counted today

    if meta["last_streak_day"]:
        try:
            last = datetime.strptime(meta["last_streak_day"], "%Y-%m-%d").date()
            cur = datetime.strptime(today, "%Y-%m-%d").date()
            consecutive = (cur - last).days == 1
        except ValueError:
            consecutive = False
    else:
        consecutive = False

    if consecutive:
        meta["streak"] += 1
    else:
        meta["streak"] = 1
        meta["streak_milestones"] = []  # streak broke — milestones can be earned again

    meta["last_streak_day"] = today

    hit = [m for m in STREAK_MILESTONES if meta["streak"] == m and m not in meta["streak_milestones"]]
    for m in hit:
        meta["streak_milestones"].append(m)

    _schedule_save("credit_meta")
    return meta["streak"], hit


def grant_onboarding_bonus(user_id: int, amount: int) -> int:
    """One-time JC grant for a brand-new user. Returns new balance."""
    return add_credits(user_id, amount)


# ══════════════════════════════════════════════════════════════════════════════
# REFERRALS
# ══════════════════════════════════════════════════════════════════════════════
#
# How attribution works:
#   - Every user can generate their own stable referral code with get_or_create_referral_code().
#   - A code is only ever consumed via redeem_referral_code(), which must be the
#     FIRST thing a brand-new user does — it checks is_new_user() itself and
#     refuses anyone who has already been "seen" by the bot, so chatting first
#     and redeeming a code afterwards never counts.
#   - referred_by is also the de-dupe guard: once set for a user, that user can
#     never redeem a second code (prevents farming bonuses with the same code
#     on the same account, or chaining referrals).

import random as _random
import string as _string

REFERRAL_CODE_LENGTH = 8
REFERRAL_CODE_ALPHABET = _string.ascii_uppercase + _string.digits


def _generate_referral_code() -> str:
    return "".join(_random.choices(REFERRAL_CODE_ALPHABET, k=REFERRAL_CODE_LENGTH))


def get_or_create_referral_code(user_id: int) -> str:
    """Return the user's existing referral code, generating one if needed.
    Codes are stable for a user's lifetime (used by !invite / /invite)."""
    uid = str(user_id)
    code = _data["referral_codes"].get(uid)
    if code:
        return code

    existing = set(_data["referral_codes"].values())
    code = _generate_referral_code()
    while code in existing:
        code = _generate_referral_code()

    _data["referral_codes"][uid] = code
    _schedule_save("referral_codes")
    return code


def get_referrer_id_for_code(code: str) -> int | None:
    """Resolve a referral code back to the referrer's user_id, or None if invalid."""
    code = code.strip().upper()
    for uid, c in _data["referral_codes"].items():
        if c == code:
            return int(uid)
    return None


def has_been_referred(user_id: int) -> bool:
    """True if this user has already redeemed a referral code (ever)."""
    return str(user_id) in _data["referred_by"]


def redeem_referral_code(user_id: int, code: str) -> tuple[bool, str, int | None]:
    """
    Attempt to attribute `user_id` as referred by whoever owns `code`.

    This is intentionally strict — it's the only path that can ever set
    referred_by, and it refuses unless ALL of the following hold:
      - the code resolves to a real referrer
      - the redeemer isn't the referrer themselves
      - the redeemer is still a brand-new user (is_new_user) — i.e. this is
        the first thing they've ever done with Jarvis, not a retroactive claim
        after chatting normally
      - the redeemer has never redeemed any code before

    Returns (success, reason, referrer_id):
      reason is one of: "ok", "invalid_code", "self_referral",
      "already_seen" (chatted/used Jarvis before redeeming — too late),
      "already_referred" (already redeemed a code previously).
      referrer_id is the resolved referrer's id on success, else None.
    """
    referrer_id = get_referrer_id_for_code(code)
    if referrer_id is None:
        return False, "invalid_code", None

    if referrer_id == user_id:
        return False, "self_referral", None

    if not is_new_user(user_id):
        return False, "already_seen", None

    if has_been_referred(user_id):
        return False, "already_referred", None

    _data["referred_by"][str(user_id)] = referrer_id
    _schedule_save("referred_by")
    return True, "ok", referrer_id


# ══════════════════════════════════════════════════════════════════════════════
# GUILD LOGS
# ══════════════════════════════════════════════════════════════════════════════

def log_guild_join(guild_id: int, name: str, member_count: int, owner_id: int, joined_at: float | None = None) -> bool:
    """Record that the bot joined a guild.

    Returns True if this is a *new* entry (first time seeing this guild),
    False if the guild was already in the log (e.g. re-invite or on_ready scan).
    """
    gid = str(guild_id)
    if gid in _data["guild_logs"]:
        return False
    _data["guild_logs"][gid] = {
        "name":         name,
        "joined_at":    joined_at if joined_at is not None else time.time(),
        "member_count": member_count,
        "owner_id":     owner_id,
    }
    _schedule_save("guild_logs")
    return True


def get_guild_log(guild_id: int) -> dict | None:
    """Return the stored log entry for a guild, or None if not found."""
    return _data["guild_logs"].get(str(guild_id))


def get_all_guild_logs() -> dict[str, dict]:
    """Return a copy of all guild log entries, keyed by str(guild_id)."""
    return dict(_data["guild_logs"])


# ══════════════════════════════════════════════════════════════════════════════
# GAME STATS  (win/loss records, used by /profile and achievements)
# ══════════════════════════════════════════════════════════════════════════════

_GAME_STATS_DEFAULT = {
    "chess_wins": 0, "chess_losses": 0,
    "mafia_wins": 0, "mafia_losses": 0,
    "hangman_wins": 0,
}


def get_game_stats(user_id: int) -> dict:
    """Return a copy of the user's win/loss record, backfilled with defaults."""
    stats = dict(_GAME_STATS_DEFAULT)
    stats.update(_data["game_stats"].get(str(user_id), {}))
    return stats


def record_game_result(user_id: int, game: str, result: str) -> dict:
    """Record a win or loss for `game` ("chess"/"mafia"/"hangman") and
    `result` ("win"/"loss"). Returns the user's updated stats dict.

    Hangman has no "losses" bucket since it's a free-for-all shared game —
    only the person who guesses the word gets a recorded result.
    """
    uid = str(user_id)
    stats = _data["game_stats"].setdefault(uid, dict(_GAME_STATS_DEFAULT))
    for key, default in _GAME_STATS_DEFAULT.items():
        stats.setdefault(key, default)
    key = f"{game}_{'wins' if result == 'win' else 'losses'}"
    if key in stats:
        stats[key] += 1
    _schedule_save("game_stats")
    return dict(stats)


# ══════════════════════════════════════════════════════════════════════════════
# SONGS PLAYED  (lifetime counter — song_history above is capped/trimmed,
# this is not, so it stays accurate for achievements like "played 100 songs")
# ══════════════════════════════════════════════════════════════════════════════

def get_songs_played(user_id: int) -> int:
    return int(_data["songs_played"].get(str(user_id), 0))


def get_favorite_song(user_id: int) -> str | None:
    """Return the most-played track title from the user's (capped) song
    history, or None if they haven't played anything yet."""
    history = get_song_history(user_id)
    if not history:
        return None
    counts: dict[str, int] = {}
    for track in history:
        title = track.get("title")
        if title:
            counts[title] = counts.get(title, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


# ══════════════════════════════════════════════════════════════════════════════
# BADGES  (achievement unlocks — never purchasable, always earned)
# ══════════════════════════════════════════════════════════════════════════════

def get_badges(user_id: int) -> list[str]:
    return list(_data["badges"].get(str(user_id), []))


def unlock_badge(user_id: int, badge_id: str) -> bool:
    """Grant `badge_id` to the user if they don't already have it.
    Returns True if it was newly unlocked (so the caller can announce it)."""
    uid = str(user_id)
    owned = _data["badges"].setdefault(uid, [])
    if badge_id in owned:
        return False
    owned.append(badge_id)
    _schedule_save("badges")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# TITLES & BANNERS  (cosmetic, bot-rendered only — never real Discord roles,
# so behavior is identical across every server the bot is in)
# ══════════════════════════════════════════════════════════════════════════════

def _cosmetic_entry(kind: str, user_id: int) -> dict:
    uid = str(user_id)
    store = _data[kind]  # "titles" or "banners"
    entry = store.setdefault(uid, {"owned": [], "equipped": None})
    entry.setdefault("owned", [])
    entry.setdefault("equipped", None)
    return entry


def get_titles(user_id: int) -> dict:
    return dict(_cosmetic_entry("titles", user_id))


def grant_title(user_id: int, title_id: str) -> bool:
    """Add `title_id` to the user's owned titles. Returns True if newly owned."""
    entry = _cosmetic_entry("titles", user_id)
    if title_id in entry["owned"]:
        return False
    entry["owned"].append(title_id)
    _schedule_save("titles")
    return True


def equip_title(user_id: int, title_id: str | None) -> bool:
    """Equip an owned title (or None to unequip). Returns False if the
    user doesn't own that title."""
    entry = _cosmetic_entry("titles", user_id)
    if title_id is not None and title_id not in entry["owned"]:
        return False
    entry["equipped"] = title_id
    _schedule_save("titles")
    return True


def get_equipped_title(user_id: int) -> str | None:
    return _cosmetic_entry("titles", user_id).get("equipped")


def get_banners(user_id: int) -> dict:
    return dict(_cosmetic_entry("banners", user_id))


def grant_banner(user_id: int, banner_id: str) -> bool:
    entry = _cosmetic_entry("banners", user_id)
    if banner_id in entry["owned"]:
        return False
    entry["owned"].append(banner_id)
    _schedule_save("banners")
    return True


def equip_banner(user_id: int, banner_id: str | None) -> bool:
    entry = _cosmetic_entry("banners", user_id)
    if banner_id is not None and banner_id not in entry["owned"]:
        return False
    entry["equipped"] = banner_id
    _schedule_save("banners")
    return True


def get_equipped_banner(user_id: int) -> str | None:
    return _cosmetic_entry("banners", user_id).get("equipped")


def get_banner_borders(user_id: int) -> dict:
    """Banner borders are a separate cosmetic slot from the banner itself —
    a person can mix any owned border with any owned banner color/gradient
    instead of the two being locked together."""
    return dict(_cosmetic_entry("banner_borders", user_id))


def grant_banner_border(user_id: int, border_id: str) -> bool:
    entry = _cosmetic_entry("banner_borders", user_id)
    if border_id in entry["owned"]:
        return False
    entry["owned"].append(border_id)
    _schedule_save("banner_borders")
    return True


def equip_banner_border(user_id: int, border_id: str | None) -> bool:
    entry = _cosmetic_entry("banner_borders", user_id)
    if border_id is not None and border_id not in entry["owned"]:
        return False
    entry["equipped"] = border_id
    _schedule_save("banner_borders")
    return True


def get_equipped_banner_border(user_id: int) -> str | None:
    return _cosmetic_entry("banner_borders", user_id).get("equipped")


def revoke_title(user_id: int, title_id: str) -> None:
    """Remove an owned title entirely — used when a paid subscription lapses.
    Safe to call even if the user doesn't own it."""
    entry = _cosmetic_entry("titles", user_id)
    if title_id in entry["owned"]:
        entry["owned"].remove(title_id)
        if entry["equipped"] == title_id:
            entry["equipped"] = None
        _schedule_save("titles")


# ══════════════════════════════════════════════════════════════════════════════
# TITLE SUBSCRIPTIONS  (recurring weekly JC billing for premium titles)
# ══════════════════════════════════════════════════════════════════════════════

def get_subscriptions(user_id: int) -> dict[str, float]:
    return dict(_data["title_subscriptions"].get(str(user_id), {}))


def set_subscription(user_id: int, title_id: str, next_charge_epoch: float) -> None:
    uid = str(user_id)
    subs = _data["title_subscriptions"].setdefault(uid, {})
    subs[title_id] = next_charge_epoch
    _schedule_save("title_subscriptions")


def clear_subscription(user_id: int, title_id: str) -> None:
    uid = str(user_id)
    subs = _data["title_subscriptions"].get(uid)
    if subs and title_id in subs:
        del subs[title_id]
        _schedule_save("title_subscriptions")


def get_all_subscriptions() -> dict[str, dict[str, float]]:
    """Return a copy of every user's subscriptions, keyed by str(user_id)."""
    return {uid: dict(subs) for uid, subs in _data["title_subscriptions"].items()}


# ══════════════════════════════════════════════════════════════════════════════
# PROFILE SECTION VISIBILITY  (per-user opt-out for /profile fields that
# don't apply to them — e.g. someone who never touches the music cog can
# hide the "Songs Played" / "Favorite Song" fields so their card isn't
# cluttered with zeroes)
# ══════════════════════════════════════════════════════════════════════════════

def get_hidden_sections(user_id: int) -> list[str]:
    return list(_data["profile_hidden"].get(str(user_id), []))


def set_section_hidden(user_id: int, section: str, hidden: bool) -> None:
    """Add/remove `section` from the user's hidden-sections list. Safe to
    call redundantly (hiding an already-hidden section, or showing an
    already-visible one) — only schedules a save when something changes."""
    uid = str(user_id)
    hidden_list = _data["profile_hidden"].setdefault(uid, [])
    if hidden:
        if section not in hidden_list:
            hidden_list.append(section)
            _schedule_save("profile_hidden")
    else:
        if section in hidden_list:
            hidden_list.remove(section)
            _schedule_save("profile_hidden")


def is_section_hidden(user_id: int, section: str) -> bool:
    return section in _data["profile_hidden"].get(str(user_id), [])