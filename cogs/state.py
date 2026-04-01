"""
Shared mutable state across cogs.
Import bot_bans, seen_users, stats, guild_prompts from here — never from other cogs directly.
This ensures all cogs always read/write the same objects.
"""
import json
import os
import time

BAN_FILE    = "bot_bans.json"
SEEN_FILE   = "seen_users.json"
STATS_FILE  = "stats.json"
PROMPTS_FILE = "guild_prompts.json"

# ── Ban state ─────────────────────────────────────────────────────────────────

def _load_bans() -> dict:
    if os.path.exists(BAN_FILE):
        with open(BAN_FILE, "r") as f:
            return json.load(f)
    return {}

def save_bans():
    with open(BAN_FILE, "w") as f:
        json.dump(bot_bans, f, indent=2)

def is_bot_banned(user_id: int) -> bool:
    return str(user_id) in bot_bans

bot_bans: dict = _load_bans()

# ── Seen users state ──────────────────────────────────────────────────────────

def _load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()

def mark_seen(user_id: int):
    seen_users.add(user_id)
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen_users), f)

def is_new_user(user_id: int) -> bool:
    return user_id not in seen_users

seen_users: set = _load_seen()

# ── Stats state ───────────────────────────────────────────────────────────────

def _load_stats() -> dict:
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r") as f:
            return json.load(f)
    return {}

def _save_stats():
    with open(STATS_FILE, "w") as f:
        json.dump(user_stats, f, indent=2)

def record_message(user_id: int, user_text: str, reply_text: str):
    """Record a completed AI interaction for stats tracking."""
    uid = str(user_id)
    now = time.time()
    # Rough token estimate: ~4 chars per token
    tokens = (len(user_text) + len(reply_text)) // 4

    if uid not in user_stats:
        user_stats[uid] = {
            "messages": 0,
            "tokens_est": 0,
            "first_seen": now,
            "last_seen": now,
        }

    user_stats[uid]["messages"]   += 1
    user_stats[uid]["tokens_est"] += tokens
    user_stats[uid]["last_seen"]   = now
    _save_stats()

def get_stats(user_id: int) -> dict | None:
    """Return stats dict for a user, or None if they have no history."""
    return user_stats.get(str(user_id))

user_stats: dict = _load_stats()

# ── Guild prompt state ────────────────────────────────────────────────────────

def _load_prompts() -> dict:
    if os.path.exists(PROMPTS_FILE):
        with open(PROMPTS_FILE, "r") as f:
            return json.load(f)
    return {}

def _save_prompts():
    with open(PROMPTS_FILE, "w") as f:
        json.dump(guild_prompts, f, indent=2)

def get_guild_prompt(guild_id: int | None) -> str | None:
    """Return custom system prompt for a guild, or None to use default."""
    if guild_id is None:
        return None
    return guild_prompts.get(str(guild_id))

def set_guild_prompt(guild_id: int, prompt: str):
    guild_prompts[str(guild_id)] = prompt
    _save_prompts()

def reset_guild_prompt(guild_id: int) -> bool:
    """Remove custom prompt. Returns True if one existed."""
    uid = str(guild_id)
    if uid in guild_prompts:
        del guild_prompts[uid]
        _save_prompts()
        return True
    return False

guild_prompts: dict = _load_prompts()