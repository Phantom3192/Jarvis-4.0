"""
Conversation history — one row per user in Turso.
Stores the last MAX_HISTORY messages as a JSON blob.
50 users = 50 rows. No runaway growth.
"""
import os
import json
import time
import asyncio

MAX_HISTORY      = 20
INACTIVE_DAYS    = 30
CLEANUP_INTERVAL = 86400

_conn = None


async def init_history():
    global _conn
    turso_url   = os.getenv("TURSO_URL",   "").strip().lstrip("=").strip()
    turso_token = os.getenv("TURSO_TOKEN", "").strip().lstrip("=").strip()
    if not turso_url or not turso_token:
        print("⚠️  History: TURSO_URL/TOKEN not set — session history won't persist across restarts.")
        return
    try:
        import libsql_experimental as libsql
        _conn = libsql.connect(database=turso_url, auth_token=turso_token)
        # One row per user — messages stored as JSON, last_active for cleanup
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS user_history (
                user_id     TEXT PRIMARY KEY,
                messages    TEXT NOT NULL DEFAULT '[]',
                last_active REAL NOT NULL DEFAULT 0
            )
        """)
        _conn.commit()

        # Migrate from old multi-row history table if it exists
        try:
            old_rows = _conn.execute(
                "SELECT user_id, role, content FROM history ORDER BY timestamp ASC"
            ).fetchall()
            if old_rows:
                by_user: dict[str, list] = {}
                for uid, role, content in old_rows:
                    by_user.setdefault(uid, []).append({"role": role, "content": content})
                now = time.time()
                for uid, msgs in by_user.items():
                    msgs = msgs[-MAX_HISTORY:]
                    _conn.execute("""
                        INSERT INTO user_history (user_id, messages, last_active)
                        VALUES (?, ?, ?)
                        ON CONFLICT(user_id) DO NOTHING
                    """, (uid, json.dumps(msgs), now))
                _conn.execute("DROP TABLE IF EXISTS history")
                _conn.commit()
                print(f"✅ Migrated {len(by_user)} user(s) from old history table")
        except Exception:
            pass  # old table doesn't exist — nothing to migrate
        print("✅ History DB connected (compact mode: 1 row/user)")
        asyncio.create_task(_cleanup_loop())
    except Exception as e:
        print(f"❌ History DB init failed: {e}")
        _conn = None


def _sync_add_message(uid: str, role: str, content: str) -> None:
    """Blocking DB write — always call via asyncio.to_thread."""
    now = time.time()
    row = _conn.execute(
        "SELECT messages FROM user_history WHERE user_id = ?", (uid,)
    ).fetchone()
    msgs = json.loads(row[0]) if row else []
    msgs.append({"role": role, "content": content})
    if len(msgs) > MAX_HISTORY:
        msgs = msgs[-MAX_HISTORY:]
    blob = json.dumps(msgs)
    _conn.execute("""
        INSERT INTO user_history (user_id, messages, last_active)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            messages    = excluded.messages,
            last_active = excluded.last_active
    """, (uid, blob, now))
    _conn.commit()

async def add_message(user_id: int, role: str, content: str) -> None:
    """Append a message to the user's history blob, trimming to MAX_HISTORY."""
    if _conn is None:
        return
    uid = str(user_id)
    try:
        await asyncio.to_thread(_sync_add_message, uid, role, content)
    except Exception as e:
        print(f"[History] add_message error for {uid}: {e}")


def _sync_get_history(uid: str) -> list[dict]:
    """Blocking DB read — always call via asyncio.to_thread."""
    row = _conn.execute(
        "SELECT messages FROM user_history WHERE user_id = ?", (uid,)
    ).fetchone()
    return json.loads(row[0]) if row else []

async def get_history(user_id: int) -> list[dict]:
    """Return stored messages for a user, oldest first."""
    if _conn is None:
        return []
    uid = str(user_id)
    try:
        return await asyncio.to_thread(_sync_get_history, uid)
    except Exception as e:
        print(f"[History] get_history error for {uid}: {e}")
        return []


def _sync_clear_history(uid: str) -> bool:
    """Blocking DB write — always call via asyncio.to_thread."""
    _conn.execute(
        "UPDATE user_history SET messages = '[]' WHERE user_id = ?", (uid,)
    )
    _conn.commit()
    return True

async def clear_history(user_id: int) -> bool:
    if _conn is None:
        return False
    uid = str(user_id)
    try:
        return await asyncio.to_thread(_sync_clear_history, uid)
    except Exception as e:
        print(f"[History] clear error for {uid}: {e}")
        return False


def _sync_load_all_histories() -> dict[int, list[dict]]:
    """Blocking DB read — always call via asyncio.to_thread."""
    rows = _conn.execute(
        "SELECT user_id, messages FROM user_history"
    ).fetchall()
    return {
        int(uid): json.loads(msgs)
        for uid, msgs in rows
        if msgs and msgs != "[]"
    }

async def load_all_histories() -> dict[int, list[dict]]:
    """Load all persisted histories at startup. Returns {user_id: [messages]}."""
    if _conn is None:
        return {}
    try:
        return await asyncio.to_thread(_sync_load_all_histories)
    except Exception as e:
        print(f"[History] load_all_histories error: {e}")
        return {}


async def _cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        await _cleanup_inactive()


def _sync_cleanup_inactive(cutoff: float) -> None:
    _conn.execute(
        "DELETE FROM user_history WHERE last_active < ? AND last_active > 0", (cutoff,)
    )
    _conn.commit()

async def _cleanup_inactive():
    if _conn is None:
        return
    cutoff = time.time() - (INACTIVE_DAYS * 86400)
    try:
        await asyncio.to_thread(_sync_cleanup_inactive, cutoff)
        print(f"🧹 Cleaned up histories inactive for {INACTIVE_DAYS}+ days")
    except Exception as e:
        print(f"[History] cleanup error: {e}")