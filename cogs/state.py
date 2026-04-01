"""
Shared mutable state across cogs.
Optimized: single data file, debounced disk writes to reduce I/O.
"""
import json
import os
import time
import asyncio

DATA_FILE = "jarvis_data.json"

# ── Internal data store ───────────────────────────────────────────────────────

_data = {
    "bans":    {},
    "seen":    [],
    "stats":   {},
    "prompts": {},
}

_dirty = False          # True when in-memory data differs from disk
_save_task = None       # Pending debounced save task


def _load():
    global _data
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                loaded = json.load(f)
                # Merge keys so missing keys in old files don't crash
                for key in _data:
                    if key in loaded:
                        _data[key] = loaded[key]
        except (json.JSONDecodeError, OSError):
            pass


def _flush():
    """Write data to disk immediately."""
    global _dirty
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(_data, f, separators=(",", ":"))
        _dirty = False
    except OSError as e:
        print(f"❌ Failed to save data: {e}")


async def _debounced_save(delay: float = 2.0):
    """Wait `delay` seconds then flush. Cancelled and restarted on each new write."""
    await asyncio.sleep(delay)
    _flush()


def _schedule_save():
    """Mark dirty and schedule a debounced flush (cancels any pending one)."""
    global _dirty, _save_task
    _dirty = True
    loop = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (e.g. during initial load) — flush immediately
        _flush()
        return
    if _save_task and not _save_task.done():
        _save_task.cancel()
    _save_task = loop.create_task(_debounced_save())


# ── Ban state ─────────────────────────────────────────────────────────────────

def save_bans():
    _schedule_save()

def is_bot_banned(user_id: int) -> bool:
    return str(user_id) in _data["bans"]

@property
def bot_bans() -> dict:
    return _data["bans"]

# Expose as module-level dict proxy for compatibility with admin.py
import types

class _BanProxy(dict):
    """Thin dict subclass that proxies reads/writes to _data['bans']."""
    def __contains__(self, key):           return key in _data["bans"]
    def __getitem__(self, key):            return _data["bans"][key]
    def __setitem__(self, key, value):     _data["bans"][key] = value
    def __delitem__(self, key):            del _data["bans"][key]
    def __iter__(self):                    return iter(_data["bans"])
    def __len__(self):                     return len(_data["bans"])
    def get(self, key, default=None):      return _data["bans"].get(key, default)
    def items(self):                       return _data["bans"].items()
    def keys(self):                        return _data["bans"].keys()
    def values(self):                      return _data["bans"].values()
    def pop(self, key, *args):             return _data["bans"].pop(key, *args)

bot_bans: dict = _BanProxy()


# ── Seen users state ──────────────────────────────────────────────────────────

class _SeenProxy(set):
    """Thin set subclass that proxies reads/writes to _data['seen'] list."""
    def __contains__(self, item):  return item in _seen_set
    def __iter__(self):            return iter(_seen_set)
    def __len__(self):             return len(_seen_set)
    def add(self, item):
        if item not in _seen_set:
            _seen_set.add(item)
            _data["seen"] = list(_seen_set)
            _schedule_save()

_seen_set: set = set()

def mark_seen(user_id: int):
    if user_id not in _seen_set:
        _seen_set.add(user_id)
        _data["seen"] = list(_seen_set)
        _schedule_save()

def is_new_user(user_id: int) -> bool:
    return user_id not in _seen_set

seen_users: set = _SeenProxy()


# ── Stats state ───────────────────────────────────────────────────────────────

def record_message(user_id: int, user_text: str, reply_text: str):
    uid  = str(user_id)
    now  = time.time()
    tokens = (len(user_text) + len(reply_text)) // 4

    if uid not in _data["stats"]:
        _data["stats"][uid] = {
            "messages": 0,
            "tokens_est": 0,
            "first_seen": now,
            "last_seen": now,
        }
    s = _data["stats"][uid]
    s["messages"]   += 1
    s["tokens_est"] += tokens
    s["last_seen"]   = now
    _schedule_save()

def get_stats(user_id: int) -> dict | None:
    return _data["stats"].get(str(user_id))


# ── Guild prompt state ────────────────────────────────────────────────────────

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


# ── Startup load ──────────────────────────────────────────────────────────────

_load()
# Rebuild seen set from loaded list
_seen_set.update(int(x) for x in _data["seen"])
# Ensure seen list contains ints
_data["seen"] = list(_seen_set)