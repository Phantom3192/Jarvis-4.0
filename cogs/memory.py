"""
Long-term user memory — one row per user in Turso.
Facts stored as a JSON blob. Smart deduplication prevents redundant entries.
50 users = 50 rows. No runaway growth.

Smart dedup logic:
- Same category + high word overlap → update the existing fact instead of adding
- "User is from India" + "User is from Patna, India" → keeps the more specific one
- Explicit "remember that..." always wins and replaces older explicit facts
"""
import os
import re
import json
import time
import asyncio
from typing import Any

from cogs.turso_db import TursoConnection

MAX_FACTS        = 20
MEMORY_DAYS      = 90
CLEANUP_INTERVAL = 86400

_db: TursoConnection | None = None


async def init_memory():
    global _db
    turso_url   = os.getenv("TURSO_URL",   "").strip().lstrip("=").strip()
    turso_token = os.getenv("TURSO_TOKEN", "").strip().lstrip("=").strip()
    if not turso_url or not turso_token:
        print("⚠️  Memory: TURSO_URL/TOKEN not set — long-term memory disabled.")
        return

    def _ensure_table():
        _db.conn.execute("""
            CREATE TABLE IF NOT EXISTS user_memory (
                user_id     TEXT PRIMARY KEY,
                facts       TEXT NOT NULL DEFAULT '[]',
                last_active REAL NOT NULL DEFAULT 0
            )
        """)
        _db.conn.commit()

    _db = TursoConnection("Memory", turso_url, turso_token, init_fn=_ensure_table)
    connected = await _db.connect_async()
    if not connected:
        print("❌ Memory DB init failed — long-term memory disabled.")
        _db = None
        return

    def _migrate():
        # Detect old multi-row schema (had an `id` column) and migrate it.
        try:
            old_rows = _db.conn.execute(
                "SELECT user_id, fact, category, timestamp FROM user_memory WHERE user_id != '' ORDER BY timestamp ASC"
            ).fetchall()
            _db.conn.execute("SELECT id FROM user_memory LIMIT 1").fetchone()
        except Exception:
            return 0  # already new schema or empty — nothing to migrate

        by_user: dict[str, list] = {}
        now = time.time()
        for uid, fact, category, ts in old_rows:
            by_user.setdefault(uid, []).append({"fact": fact, "category": category, "ts": ts})
        _db.conn.execute("DROP TABLE user_memory")
        _db.conn.execute("""
            CREATE TABLE user_memory (
                user_id     TEXT PRIMARY KEY,
                facts       TEXT NOT NULL DEFAULT '[]',
                last_active REAL NOT NULL DEFAULT 0
            )
        """)
        for uid, entries in by_user.items():
            entries = entries[-MAX_FACTS:]
            _db.conn.execute("""
                INSERT INTO user_memory (user_id, facts, last_active) VALUES (?, ?, ?)
            """, (uid, json.dumps(entries), now))
        _db.conn.commit()
        return len(by_user)

    migrated = await _db.run(_migrate, default=0)
    if migrated:
        print(f"✅ Migrated memory for {migrated} user(s) from old schema")

    print("✅ Memory DB connected (compact mode: 1 row/user)")
    asyncio.create_task(_cleanup_loop())
    asyncio.create_task(_db.keepalive_loop())


# ── Extraction patterns ───────────────────────────────────────────────────────

_PATTERNS: list[tuple[str, str, Any]] = [
    (r"\b(?:please\s+)?remember\s+(?:that\s+|to\s+)?(.+)", "explicit",
     lambda m: m.group(1).strip().rstrip(".")),
    (r"\bmy\s+name\s+is\s+([A-Za-z][A-Za-z\s]{1,30})", "identity",
     lambda m: f"User's name is {m.group(1).strip()}"),
    (r"\b(?:i['']?m|i\s+am)\s+called\s+([A-Za-z][A-Za-z\s]{1,30})", "identity",
     lambda m: f"User's name is {m.group(1).strip()}"),
    (r"\bcall\s+me\s+([A-Za-z][A-Za-z\s]{1,30})", "identity",
     lambda m: f"User's name is {m.group(1).strip()}"),
    (r"\bi(?:'m|\s+am)\s+(\d{1,2})\s+years?\s+old", "identity",
     lambda m: f"User is {m.group(1)} years old"),
    (r"\bi(?:'m|\s+am)\s+from\s+([A-Za-z][A-Za-z\s,]{2,40})", "identity",
     lambda m: f"User is from {m.group(1).strip()}"),
    (r"\bi\s+live\s+in\s+([A-Za-z][A-Za-z\s,]{2,40})", "identity",
     lambda m: f"User lives in {m.group(1).strip()}"),
    (r"\bi\s+work\s+as\s+(?:a\s+|an\s+)?(.+?)(?:\s+at\s+|\.|$)", "identity",
     lambda m: f"User works as {m.group(1).strip()}"),
    (r"\bi(?:'m|\s+am)\s+a(?:n)?\s+(developer|engineer|designer|student|teacher|doctor|writer|artist|gamer|programmer|coder|manager|lawyer|nurse|chef|musician)[^.]*", "identity",
     lambda m: f"User is a {m.group(1).strip()}"),
    (r"\bi\s+(?:really\s+)?(?:like|love|enjoy|adore)\s+(.{3,60}?)(?:\.|$|,)", "preference",
     lambda m: f"User likes {m.group(1).strip()}"),
    (r"\bi\s+(?:really\s+)?(?:hate|dislike|can't\s+stand|don't\s+like)\s+(.{3,60}?)(?:\.|$|,)", "preference",
     lambda m: f"User dislikes {m.group(1).strip()}"),
    (r"\bmy\s+fav(?:ou?rite)?\s+(\w+)\s+is\s+(.{2,40})", "preference",
     lambda m: f"User's favourite {m.group(1)} is {m.group(2).strip()}"),
    (r"\bi\s+speak\s+([A-Za-z]+(?:\s+and\s+[A-Za-z]+)*)", "identity",
     lambda m: f"User speaks {m.group(1).strip()}"),
]

_COMPILED = [
    (re.compile(pat, re.IGNORECASE), cat, fmt)
    for pat, cat, fmt in _PATTERNS
]

_SKIP = re.compile(
    r"^\s*(?:hi|hey|hello|ok|okay|thanks|thank you|yes|no|nope|yep|sure|lol|haha|k|cool|nice|wow|wtf|omg|bruh|lmao)\s*$",
    re.IGNORECASE
)

# Categories where only one fact should exist (newest wins)
_SINGLETON_CATEGORIES = {"identity"}

# Sub-keys that are truly singular — replace on match
_SINGLETON_PREFIXES = (
    "user's name is",
    "user is from",
    "user lives in",
    "user is a ",
    "user works as",
    r"user is \d",   # age
    "user speaks",
)
_SINGLETON_RE = re.compile(
    r"^(" + "|".join(_SINGLETON_PREFIXES) + r")",
    re.IGNORECASE
)


def extract_facts(user_message: str) -> list[tuple[str, str]]:
    if not user_message or len(user_message) < 8:
        return []
    if _SKIP.match(user_message.strip()):
        return []
    facts, seen = [], set()
    for pattern, category, formatter in _COMPILED:
        m = pattern.search(user_message)
        if m:
            try:
                fact = formatter(m)
                if fact and len(fact) > 5 and fact not in seen:
                    # If an explicit "remember ..." fact was captured, re-run
                    # identity/preference sub-patterns on the captured tail so that
                    # "remember to call me Phantom" becomes identity fact
                    # "User's name is Phantom" rather than a raw string.
                    if category == "explicit":
                        tail = fact
                        promoted = False
                        for sub_pat, sub_cat, sub_fmt in _COMPILED[1:]:
                            sm = sub_pat.search(tail)
                            if sm:
                                try:
                                    sub_fact = sub_fmt(sm)
                                    if sub_fact and len(sub_fact) > 5 and sub_fact not in seen:
                                        facts.append((sub_fact, sub_cat))
                                        seen.add(sub_fact)
                                        promoted = True
                                except Exception:
                                    pass
                        if promoted:
                            seen.add(fact)
                            continue
                    facts.append((fact, category))
                    seen.add(fact)
            except Exception:
                continue
    return facts


# ── Smart deduplication ───────────────────────────────────────────────────────

def _word_overlap(a: str, b: str) -> float:
    """Jaccard similarity between word sets of two strings."""
    wa = set(re.findall(r"\w+", a.lower()))
    wb = set(re.findall(r"\w+", b.lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _merge_facts(existing: list[dict], new_facts: list[tuple[str, str]]) -> list[dict]:
    """
    Merge new facts into the existing list intelligently:
    - Singleton prefixes (name, location, age, etc.) → replace the old one
    - High word overlap (>0.6) in same category → replace with newer/longer
    - Otherwise → append if under the cap
    """
    result = list(existing)
    now = time.time()

    for fact, category in new_facts:
        replaced = False

        # Check for singleton match (e.g. two "User is from X" facts)
        if _SINGLETON_RE.match(fact):
            for i, entry in enumerate(result):
                if _SINGLETON_RE.match(entry["fact"]) and \
                   entry["fact"].split()[0:3] == fact.split()[0:3]:  # same prefix words
                    # Keep the more specific (longer) one
                    if len(fact) >= len(entry["fact"]):
                        result[i] = {"fact": fact, "category": category, "ts": now}
                    replaced = True
                    break

        if not replaced:
            # Check semantic overlap within the same category
            for i, entry in enumerate(result):
                if entry["category"] == category and _word_overlap(fact, entry["fact"]) > 0.6:
                    # Same meaning — keep the longer/newer one
                    if len(fact) >= len(entry["fact"]):
                        result[i] = {"fact": fact, "category": category, "ts": now}
                    replaced = True
                    break

        if not replaced:
            result.append({"fact": fact, "category": category, "ts": now})

    # Cap and evict oldest
    if len(result) > MAX_FACTS:
        result.sort(key=lambda x: x["ts"])
        result = result[-MAX_FACTS:]

    return result


# ── DB operations ─────────────────────────────────────────────────────────────

async def save_facts(user_id: int, facts: list[tuple[str, str]]) -> None:
    if _db is None or not facts:
        return
    uid = str(user_id)

    def _do():
        now = time.time()
        row = _db.conn.execute(
            "SELECT facts FROM user_memory WHERE user_id = ?", (uid,)
        ).fetchone()
        existing = json.loads(row[0]) if row else []
        merged   = _merge_facts(existing, facts)
        blob     = json.dumps(merged)
        _db.conn.execute("""
            INSERT INTO user_memory (user_id, facts, last_active)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                facts       = excluded.facts,
                last_active = excluded.last_active
        """, (uid, blob, now))
        _db.conn.commit()

    await _db.run(_do)


async def get_facts(user_id: int) -> list[str]:
    if _db is None:
        return []
    uid = str(user_id)

    def _do():
        row = _db.conn.execute(
            "SELECT facts FROM user_memory WHERE user_id = ?", (uid,)
        ).fetchone()
        if not row:
            return []
        entries = json.loads(row[0])
        entries.sort(key=lambda x: x.get("ts", 0), reverse=True)
        return [e["fact"] for e in entries]

    return await _db.run(_do, default=[])


async def forget_facts(user_id: int) -> int:
    if _db is None:
        return 0
    uid = str(user_id)

    def _do():
        row = _db.conn.execute(
            "SELECT facts FROM user_memory WHERE user_id = ?", (uid,)
        ).fetchone()
        count = len(json.loads(row[0])) if row else 0
        _db.conn.execute("UPDATE user_memory SET facts = '[]' WHERE user_id = ?", (uid,))
        _db.conn.commit()
        return count

    return await _db.run(_do, default=0)


async def get_facts_count(user_id: int) -> int:
    if _db is None:
        return 0
    uid = str(user_id)

    def _do():
        row = _db.conn.execute(
            "SELECT facts FROM user_memory WHERE user_id = ?", (uid,)
        ).fetchone()
        return len(json.loads(row[0])) if row else 0

    return await _db.run(_do, default=0)


def build_memory_prompt(facts: list[str]) -> str:
    if not facts:
        return ""
    lines = "\n".join(f"- {f}" for f in facts)
    return (
        "\n\nThings you remember about this user from past conversations:\n"
        + lines +
        "\nUse this context naturally — don't recite it back, just let it inform your responses."
    )


async def _cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        await _cleanup_old_facts()


async def _cleanup_old_facts():
    if _db is None:
        return
    cutoff = time.time() - (MEMORY_DAYS * 86400)

    def _do():
        _db.conn.execute(
            "DELETE FROM user_memory WHERE last_active < ? AND last_active > 0", (cutoff,)
        )
        _db.conn.commit()

    await _db.run(_do)
    print(f"🧹 Cleaned up memory for users inactive {MEMORY_DAYS}+ days")