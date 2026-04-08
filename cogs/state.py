"""
Shared mutable state across cogs — backed by MongoDB Atlas.
Data persists across Railway restarts and redeployments.
"""
import os
import time
import asyncio
import motor.motor_asyncio

MONGODB_URL = os.getenv("MONGODB_URL", "")
_client = None
_db = None
_col = None  # single "jarvis" collection

# ── In-memory mirrors (so the rest of your cogs work unchanged) ───────────────

_data = {
    "bans":        {},
    "seen":        set(),
    "stats":       {},
    "prompts":     {},
    "rate_limits": {},   # uid → {"count": int, "day": "YYYY-MM-DD"}
}


# ── DB init (call once at startup) ───────────────────────────────────────────

async def init_db():
    global _client, _db, _col
    _client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URL)
    _db  = _client["jarvis"]
    _col = _db["state"]

    # Load everything from DB into memory
    doc = await _col.find_one({"_id": "main"})
    if doc:
        _data["bans"]        = doc.get("bans",        {})
        _data["stats"]       = doc.get("stats",       {})
        _data["prompts"]     = doc.get("prompts",     {})
        _data["rate_limits"] = doc.get("rate_limits", {})
        _data["seen"]        = set(doc.get("seen", []))
    else:
        # First run — create the document
        await _col.insert_one({
            "_id":         "main",
            "bans":        {},
            "seen":        [],
            "stats":       {},
            "prompts":     {},
            "rate_limits": {},
        })


async def _save():
    """Write current in-memory state to MongoDB."""
    if _col is None:
        return
    await _col.update_one(
        {"_id": "main"},
        {"$set": {
            "bans":        _data["bans"],
            "seen":        list(_data["seen"]),
            "stats":       _data["stats"],
            "prompts":     _data["prompts"],
            "rate_limits": _data["rate_limits"],
        }},
        upsert=True,
    )


# ── Debounced save (avoids hammering the DB on every message) ─────────────────

_save_task = None

def _schedule_save():
    global _save_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _save_task and not _save_task.done():
        _save_task.cancel()
    _save_task = loop.create_task(_debounced_save())

async def _debounced_save(delay: float = 2.0):
    await asyncio.sleep(delay)
    await _save()


# ── Ban state ─────────────────────────────────────────────────────────────────

class _BanProxy(dict):
    def __contains__(self, key):       return key in _data["bans"]
    def __getitem__(self, key):        return _data["bans"][key]
    def __setitem__(self, key, value): _data["bans"][key] = value
    def __delitem__(self, key):        del _data["bans"][key]
    def __iter__(self):                return iter(_data["bans"])
    def __len__(self):                 return len(_data["bans"])
    def get(self, key, default=None):  return _data["bans"].get(key, default)
    def items(self):                   return _data["bans"].items()
    def keys(self):                    return _data["bans"].keys()
    def values(self):                  return _data["bans"].values()
    def pop(self, key, *args):         return _data["bans"].pop(key, *args)

bot_bans: dict = _BanProxy()

def save_bans():
    _schedule_save()

def is_bot_banned(user_id: int) -> bool:
    return str(user_id) in _data["bans"]


# ── Seen users ────────────────────────────────────────────────────────────────

class _SeenProxy(set):
    def __contains__(self, item): return item in _data["seen"]
    def __iter__(self):           return iter(_data["seen"])
    def __len__(self):            return len(_data["seen"])
    def add(self, item):
        if item not in _data["seen"]:
            _data["seen"].add(item)
            _schedule_save()

seen_users: set = _SeenProxy()

def mark_seen(user_id: int):
    if user_id not in _data["seen"]:
        _data["seen"].add(user_id)
        _schedule_save()

def is_new_user(user_id: int) -> bool:
    return user_id not in _data["seen"]


# ── Stats ─────────────────────────────────────────────────────────────────────

def record_message(user_id: int, user_text: str, reply_text: str):
    uid    = str(user_id)
    now    = time.time()
    tokens = (len(user_text) + len(reply_text)) // 4
    if uid not in _data["stats"]:
        _data["stats"][uid] = {
            "messages":   0,
            "tokens_est": 0,
            "first_seen": now,
            "last_seen":  now,
        }
    s = _data["stats"][uid]
    s["messages"]   += 1
    s["tokens_est"] += tokens
    s["last_seen"]   = now
    _schedule_save()

def get_stats(user_id: int) -> dict | None:
    return _data["stats"].get(str(user_id))


# ── Guild prompts ─────────────────────────────────────────────────────────────

def get_guild_prompt(guild_id: int | None) -> str | None:
    if guild_id is None:
        return None
    return _data["prompts"].get(str(guild_id))

def set_guild_prompt(guild_id: int, prompt: str):
    _data["prompts"][str(guild_id)] = prompt
    _schedule_save()

def reset_guild_prompt(guild_id: int) -> bool:
    uid = str(guild_id)
    if uid in _data["prompts"]:
        del _data["prompts"][uid]
        _schedule_save()
        return True
    return False


# ── AI Rate limiting ──────────────────────────────────────────────────────────

DAILY_AI_LIMIT   = 20   # max AI messages per user per day
WARN_AT          = 15   # send a heads-up warning at this count

def _today_utc() -> str:
    """Return today's date as a YYYY-MM-DD string (UTC)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_ai_usage(user_id: int) -> tuple[int, str]:
    """
    Return (count_today, reset_date) for a user.
    Resets automatically if the stored day is not today.
    """
    uid   = str(user_id)
    today = _today_utc()
    entry = _data["rate_limits"].get(uid)

    if not entry or entry.get("day") != today:
        # New day — reset
        _data["rate_limits"][uid] = {"count": 0, "day": today}
        _schedule_save()
        return 0, today

    return entry["count"], today


def increment_ai_usage(user_id: int) -> int:
    """
    Increment the user's daily AI message count.
    Returns the new count after incrementing.
    """
    uid   = str(user_id)
    today = _today_utc()
    entry = _data["rate_limits"].get(uid)

    if not entry or entry.get("day") != today:
        _data["rate_limits"][uid] = {"count": 1, "day": today}
    else:
        _data["rate_limits"][uid]["count"] += 1

    _schedule_save()
    return _data["rate_limits"][uid]["count"]


def is_ai_rate_limited(user_id: int) -> bool:
    """Return True if the user has exhausted their daily AI quota."""
    count, _ = get_ai_usage(user_id)
    return count >= DAILY_AI_LIMIT


def reset_ai_usage(user_id: int):
    """Admin helper — manually reset a user's daily AI count."""
    uid = str(user_id)
    _data["rate_limits"][uid] = {"count": 0, "day": _today_utc()}
    _schedule_save()