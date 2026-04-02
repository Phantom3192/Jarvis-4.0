"""
Conversation history manager — backed by Turso (LibSQL).
Stores last 20 messages per user, auto-deletes after 30 days inactivity.
"""
import os
import time
import asyncio
import libsql

TURSO_URL   = os.getenv("TURSO_URL", "")
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "")

MAX_HISTORY      = 20    # messages per user
INACTIVE_DAYS    = 30    # auto-delete after this many days of inactivity
CLEANUP_INTERVAL = 86400 # run cleanup every 24 hours

_conn = None


# ── Init ──────────────────────────────────────────────────────────────────────

async def init_history():
    """Call once at startup to connect and create table."""
    global _conn
    _conn = libsql.connect(
        database=TURSO_URL,
        auth_token=TURSO_TOKEN,
    )
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            timestamp   REAL NOT NULL
        )
    """)
    _conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_history_user
        ON history (user_id, timestamp)
    """)
    _conn.commit()
    print("✅ Turso history DB connected")

    # Start background cleanup task
    asyncio.create_task(_cleanup_loop())


# ── Core functions ────────────────────────────────────────────────────────────

async def add_message(user_id: int, role: str, content: str):
    """
    Add a message to history and trim to MAX_HISTORY.
    role: 'user' or 'assistant'
    """
    uid = str(user_id)
    now = time.time()

    # Insert new message
    _conn.execute(
        "INSERT INTO history (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        [uid, role, content, now]
    )

    # Trim to last MAX_HISTORY messages
    _conn.execute("""
        DELETE FROM history
        WHERE user_id = ?
        AND id NOT IN (
            SELECT id FROM history
            WHERE user_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        )
    """, [uid, uid, MAX_HISTORY])

    _conn.commit()


async def get_history(user_id: int) -> list[dict]:
    """
    Get conversation history for a user.
    Returns list of {'role': ..., 'content': ...} dicts oldest first.
    """
    uid = str(user_id)
    result = _conn.execute(
        """
        SELECT role, content FROM history
        WHERE user_id = ?
        ORDER BY timestamp ASC
        """,
        [uid]
    ).fetchall()

    return [{"role": row[0], "content": row[1]} for row in result]


async def clear_history(user_id: int) -> bool:
    """
    Clear all history for a user.
    Returns True if anything was deleted.
    """
    uid = str(user_id)
    _conn.execute("DELETE FROM history WHERE user_id = ?", [uid])
    _conn.commit()
    return True


async def get_history_count(user_id: int) -> int:
    """Get number of stored messages for a user."""
    uid = str(user_id)
    result = _conn.execute(
        "SELECT COUNT(*) FROM history WHERE user_id = ?",
        [uid]
    ).fetchone()
    return result[0] if result else 0


# ── Auto cleanup ──────────────────────────────────────────────────────────────

async def _cleanup_loop():
    """Background task — deletes history of users inactive for 30+ days."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        await _cleanup_inactive()


async def _cleanup_inactive():
    cutoff = time.time() - (INACTIVE_DAYS * 86400)
    _conn.execute("""
        DELETE FROM history
        WHERE user_id IN (
            SELECT user_id FROM history
            GROUP BY user_id
            HAVING MAX(timestamp) < ?
        )
    """, [cutoff])
    _conn.commit()
    print(f"🧹 Cleaned up inactive user histories older than {INACTIVE_DAYS} days")