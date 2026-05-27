"""
Long-term user memory — stores important facts about users across sessions.
"""
import os
import re
import time
import asyncio
from typing import Any

MAX_FACTS        = 20
MEMORY_DAYS      = 90
CLEANUP_INTERVAL = 86400

_conn = None


async def init_memory():
    global _conn
    turso_url   = os.getenv("TURSO_URL",   "").strip().lstrip("=").strip()
    turso_token = os.getenv("TURSO_TOKEN", "").strip().lstrip("=").strip()
    if not turso_url or not turso_token:
        print("⚠️  Memory: TURSO_URL/TOKEN not set — long-term memory disabled.")
        return
    try:
        import libsql_experimental as libsql
        _conn = libsql.connect(database=turso_url, auth_token=turso_token)
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS user_memory (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   TEXT    NOT NULL,
                fact      TEXT    NOT NULL,
                category  TEXT    NOT NULL DEFAULT 'general',
                timestamp REAL    NOT NULL
            )
        """)
        _conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_user
            ON user_memory (user_id, timestamp)
        """)
        _conn.commit()
        print("✅ Memory DB connected")
        asyncio.create_task(_cleanup_loop())
    except Exception as e:
        print(f"❌ Memory DB init failed: {e}")
        _conn = None


_PATTERNS: list[tuple[str, str, Any]] = [
    (r"\b(?:please\s+)?remember\s+(?:that\s+)?(.+)", "explicit",
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

_SKIP_PATTERNS = re.compile(
    r"^\s*(?:hi|hey|hello|ok|okay|thanks|thank you|yes|no|nope|yep|sure|lol|haha|k|cool|nice|wow|wtf|omg|bruh|lmao)\s*$",
    re.IGNORECASE
)


def extract_facts(user_message: str) -> list[tuple[str, str]]:
    if not user_message or len(user_message) < 8:
        return []
    if _SKIP_PATTERNS.match(user_message.strip()):
        return []
    facts = []
    seen  = set()
    for pattern, category, formatter in _COMPILED:
        m = pattern.search(user_message)
        if m:
            try:
                fact = formatter(m)
                if fact and len(fact) > 5 and fact not in seen:
                    facts.append((fact, category))
                    seen.add(fact)
            except Exception:
                continue
    return facts


async def save_facts(user_id: int, facts: list[tuple[str, str]]) -> None:
    if _conn is None or not facts:
        return
    uid = str(user_id)
    now = time.time()
    try:
        existing_rows = _conn.execute(
            "SELECT fact FROM user_memory WHERE user_id = ?", (uid,)
        ).fetchall()
        existing = {row[0].lower() for row in existing_rows}
    except Exception:
        existing = set()

    new_facts = [(f, c) for f, c in facts if f.lower() not in existing]
    if not new_facts:
        return

    try:
        for fact, category in new_facts:
            _conn.execute(
                "INSERT INTO user_memory (user_id, fact, category, timestamp) VALUES (?, ?, ?, ?)",
                (uid, fact, category, now)
            )
        _conn.execute("""
            DELETE FROM user_memory
            WHERE user_id = ?
            AND id NOT IN (
                SELECT id FROM user_memory
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            )
        """, (uid, uid, MAX_FACTS))
        _conn.commit()
    except Exception as e:
        print(f"[Memory] save error for {uid}: {e}")


async def get_facts(user_id: int) -> list[str]:
    if _conn is None:
        return []
    uid = str(user_id)
    try:
        rows = _conn.execute(
            "SELECT fact FROM user_memory WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
            (uid, MAX_FACTS)
        ).fetchall()
        return [row[0] for row in rows]
    except Exception as e:
        print(f"[Memory] get error for {uid}: {e}")
        return []


async def forget_facts(user_id: int) -> int:
    if _conn is None:
        return 0
    uid = str(user_id)
    try:
        row = _conn.execute(
            "SELECT COUNT(*) FROM user_memory WHERE user_id = ?", (uid,)
        ).fetchone()
        count = row[0] if row else 0
        _conn.execute("DELETE FROM user_memory WHERE user_id = ?", (uid,))
        _conn.commit()
        return count
    except Exception as e:
        print(f"[Memory] forget error for {uid}: {e}")
        return 0


async def get_facts_count(user_id: int) -> int:
    if _conn is None:
        return 0
    uid = str(user_id)
    try:
        row = _conn.execute(
            "SELECT COUNT(*) FROM user_memory WHERE user_id = ?", (uid,)
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


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
    if _conn is None:
        return
    cutoff = time.time() - (MEMORY_DAYS * 86400)
    try:
        _conn.execute("DELETE FROM user_memory WHERE timestamp < ?", (cutoff,))
        _conn.commit()
        print(f"🧹 Cleaned up memory facts older than {MEMORY_DAYS} days")
    except Exception as e:
        print(f"[Memory] cleanup error: {e}")