"""
Shared mutable state across cogs — backed by Turso (LibSQL).
Stores bans, seen users, stats, guild prompts, and rate limits.
All data persists across restarts via Turso.
Bot runs fine in memory-only mode if TURSO_URL/TOKEN are not set.
"""
import os
import time
import asyncio
import json
from datetime import datetime, timezone

_conn = None  # libsql connection

# ── In-memory mirrors ─────────────────────────────────────────────────────────

_data = {
    "bans":        {},   # str(user_id) → {"reason": str, "expires": float|None}
    "seen":        set(),# set of int user_ids
    "stats":       {},   # str(user_id) → {"messages", "tokens_est", "first_seen", "last_seen"}
    "prompts":     {},   # str(guild_id) → prompt string
    "rate_limits": {},   # str(user_id)  → {"count": int, "day": "YYYY-MM-DD"}
}


# ── DB init ───────────────────────────────────────────────────────────────────

async def init_db():
    """Call once at startup. Connects to Turso and loads all state into memory."""
    global _conn

    turso_url   = os.getenv("TURSO_URL",   "").strip().lstrip("=").strip()
    turso_token = os.getenv("TURSO_TOKEN", "").strip().lstrip("=").strip()

    if not turso_url or not turso_token:
        print(
            "⚠️  TURSO_URL or TURSO_TOKEN not set.\n"
            "   Jarvis will run in memory-only mode — all data will be lost on restart.\n"
            "   Add TURSO_URL and TURSO_TOKEN to your .env to persist data."
        )
        return

    try:
        import libsql_experimental as libsql
        _conn = libsql.connect(database=turso_url, auth_token=turso_token)

        # ── Create state table ────────────────────────────────────────────────
        # One row per key — stores JSON blobs for each data category
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        _conn.commit()

        # ── Load all state from DB ─────────────────────────────────────────────
        rows = _conn.execute("SELECT key, value FROM state").fetchall()
        db   = {row[0]: json.loads(row[1]) for row in rows}

        if "bans" in db:
            _data["bans"] = db["bans"]
        if "seen" in db:
            _data["seen"] = set(db["seen"])
        if "stats" in db:
            _data["stats"] = db["stats"]
        if "prompts" in db:
            _data["prompts"] = db["prompts"]
        if "rate_limits" in db:
            _data["rate_limits"] = db["rate_limits"]

        print("✅ Turso state DB connected")

    except Exception as e:
        print(
            f"❌ Turso state DB connection failed: {e}\n"
            "   Jarvis will run in memory-only mode."
        )
        _conn = None


# ── Save helpers ──────────────────────────────────────────────────────────────

def _save_key(key: str, value):
    """Upsert a single key into the state table."""
    if _conn is None:
        return
    try:
        serialised = json.dumps(value)
        _conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [key, serialised]
        )
        _conn.commit()
    except Exception as e:
        print(f"❌ Turso save error ({key}): {e}")


# ── Debounced save ────────────────────────────────────────────────────────────

_save_tasks: dict[str, asyncio.Task] = {}

def _schedule_save(key: str):
    """Debounce saves — waits 2s after last change before writing."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = _save_tasks.get(key)
    if task and not task.done():
        task.cancel()
    _save_tasks[key] = loop.create_task(_debounced_save(key))

async def _debounced_save(key: str, delay: float = 2.0):
    await asyncio.sleep(delay)
    if key == "bans":
        _save_key("bans", _data["bans"])
    elif key == "seen":
        _save_key("seen", list(_data["seen"]))
    elif key == "stats":
        _save_key("stats", _data["stats"])
    elif key == "prompts":
        _save_key("prompts", _data["prompts"])
    elif key == "rate_limits":
        _save_key("rate_limits", _data["rate_limits"])


# ══════════════════════════════════════════════════════════════════════════════
# BAN STATE
# ══════════════════════════════════════════════════════════════════════════════

class _BanProxy(dict):
    """Proxy so existing cog code (bot_bans[x] = y, del bot_bans[x]) still works."""
    def __contains__(self, key):       return key in _data["bans"]
    def __getitem__(self, key):        return _data["bans"][key]
    def __setitem__(self, key, value):
        _data["bans"][key] = value
        _schedule_save("bans")
    def __delitem__(self, key):
        del _data["bans"][key]
        _schedule_save("bans")
    def __iter__(self):                return iter(_data["bans"])
    def __len__(self):                 return len(_data["bans"])
    def get(self, key, default=None):  return _data["bans"].get(key, default)
    def items(self):                   return _data["bans"].items()
    def keys(self):                    return _data["bans"].keys()
    def values(self):                  return _data["bans"].values()
    def pop(self, key, *args):
        val = _data["bans"].pop(key, *args)
        _schedule_save("bans")
        return val

bot_bans: dict = _BanProxy()

def save_bans():
    _schedule_save("bans")

def is_bot_banned(user_id: int) -> bool:
    return str(user_id) in _data["bans"]


# ══════════════════════════════════════════════════════════════════════════════
# SEEN USERS
# ══════════════════════════════════════════════════════════════════════════════

class _SeenProxy(set):
    def __contains__(self, item): return item in _data["seen"]
    def __iter__(self):           return iter(_data["seen"])
    def __len__(self):            return len(_data["seen"])
    def add(self, item):
        if item not in _data["seen"]:
            _data["seen"].add(item)
            _schedule_save("seen")

seen_users: set = _SeenProxy()

def mark_seen(user_id: int):
    if user_id not in _data["seen"]:
        _data["seen"].add(user_id)
        _schedule_save("seen")

def is_new_user(user_id: int) -> bool:
    return user_id not in _data["seen"]


# ══════════════════════════════════════════════════════════════════════════════
# STATS
# ══════════════════════════════════════════════════════════════════════════════

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
    _schedule_save("stats")

def get_stats(user_id: int) -> dict | None:
    return _data["stats"].get(str(user_id))


# ══════════════════════════════════════════════════════════════════════════════
# GUILD PROMPTS
# ══════════════════════════════════════════════════════════════════════════════

def get_guild_prompt(guild_id: int | None) -> str | None:
    if guild_id is None:
        return None
    return _data["prompts"].get(str(guild_id))

def set_guild_prompt(guild_id: int, prompt: str):
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

DAILY_AI_LIMIT = 50
WARN_AT        = 40

def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def get_ai_usage(user_id: int) -> tuple[int, str]:
    uid   = str(user_id)
    today = _today_utc()
    entry = _data["rate_limits"].get(uid)
    if not entry or entry.get("day") != today:
        _data["rate_limits"][uid] = {"count": 0, "day": today}
        _schedule_save("rate_limits")
        return 0, today
    return entry["count"], today

def increment_ai_usage(user_id: int) -> int:
    uid   = str(user_id)
    today = _today_utc()
    entry = _data["rate_limits"].get(uid)
    if not entry or entry.get("day") != today:
        _data["rate_limits"][uid] = {"count": 1, "day": today}
    else:
        _data["rate_limits"][uid]["count"] += 1
    _schedule_save("rate_limits")
    return _data["rate_limits"][uid]["count"]

def is_ai_rate_limited(user_id: int) -> bool:
    count, _ = get_ai_usage(user_id)
    return count >= DAILY_AI_LIMIT

def reset_ai_usage(user_id: int):
    uid = str(user_id)
    _data["rate_limits"][uid] = {"count": 0, "day": _today_utc()}
    _schedule_save("rate_limits")