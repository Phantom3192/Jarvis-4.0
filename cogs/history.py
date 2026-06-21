"""
Conversation history — one row per user in Turso.
Stores the last MAX_HISTORY messages as a JSON blob.
50 users = 50 rows. No runaway growth.

Connection handling (reconnect-on-dropped-stream, retry, off-event-loop
execution, keepalive) is delegated to cogs.turso_db.TursoConnection —
see that module's docstring for why this is needed.
"""
import os
import json
import time
import asyncio

from cogs.turso_db import TursoConnection

MAX_HISTORY      = 20
INACTIVE_DAYS    = 30
CLEANUP_INTERVAL = 86400

_db: TursoConnection | None = None


async def init_history():
    global _db
    turso_url   = os.getenv("TURSO_URL",   "").strip().lstrip("=").strip()
    turso_token = os.getenv("TURSO_TOKEN", "").strip().lstrip("=").strip()
    if not turso_url or not turso_token:
        print("⚠️  History: TURSO_URL/TOKEN not set — session history won't persist across restarts.")
        return

    def _ensure_table():
        _db.conn.execute("""
            CREATE TABLE IF NOT EXISTS user_history (
                user_id     TEXT PRIMARY KEY,
                messages    TEXT NOT NULL DEFAULT '[]',
                last_active REAL NOT NULL DEFAULT 0
            )
        """)
        _db.conn.commit()

    _db = TursoConnection("History", turso_url, turso_token, init_fn=_ensure_table)
    connected = await _db.connect_async()
    if not connected:
        print("❌ History DB init failed — session history won't persist.")
        _db = None
        return

    # Migrate from old multi-row history table if it exists (best-effort, one-time)
    def _migrate():
        try:
            old_rows = _db.conn.execute(
                "SELECT user_id, role, content FROM history ORDER BY timestamp ASC"
            ).fetchall()
        except Exception:
            return 0  # old table doesn't exist — nothing to migrate
        if not old_rows:
            return 0
        by_user: dict[str, list] = {}
        for uid, role, content in old_rows:
            by_user.setdefault(uid, []).append({"role": role, "content": content})
        now = time.time()
        for uid, msgs in by_user.items():
            msgs = msgs[-MAX_HISTORY:]
            _db.conn.execute("""
                INSERT INTO user_history (user_id, messages, last_active)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO NOTHING
            """, (uid, json.dumps(msgs), now))
        _db.conn.execute("DROP TABLE IF EXISTS history")
        _db.conn.commit()
        return len(by_user)

    migrated = await _db.run(_migrate, default=0)
    if migrated:
        print(f"✅ Migrated {migrated} user(s) from old history table")

    print("✅ History DB connected (compact mode: 1 row/user)")
    asyncio.create_task(_cleanup_loop())
    asyncio.create_task(_db.keepalive_loop())


# ── add_message ───────────────────────────────────────────────────────────────

async def add_message(user_id: int, role: str, content: str) -> None:
    """Append a message to the user's history blob, trimming to MAX_HISTORY."""
    if _db is None:
        return
    uid = str(user_id)

    def _do():
        now = time.time()
        row = _db.conn.execute(
            "SELECT messages FROM user_history WHERE user_id = ?", (uid,)
        ).fetchone()
        msgs = json.loads(row[0]) if row else []
        msgs.append({"role": role, "content": content})
        if len(msgs) > MAX_HISTORY:
            msgs = msgs[-MAX_HISTORY:]
        blob = json.dumps(msgs)
        _db.conn.execute("""
            INSERT INTO user_history (user_id, messages, last_active)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                messages    = excluded.messages,
                last_active = excluded.last_active
        """, (uid, blob, now))
        _db.conn.commit()

    await _db.run(_do)


# ── get_history ───────────────────────────────────────────────────────────────

async def get_history(user_id: int) -> list[dict]:
    """Return stored messages for a user, oldest first."""
    if _db is None:
        return []
    uid = str(user_id)

    def _do():
        row = _db.conn.execute(
            "SELECT messages FROM user_history WHERE user_id = ?", (uid,)
        ).fetchone()
        return json.loads(row[0]) if row else []

    return await _db.run(_do, default=[])


# ── clear_history ─────────────────────────────────────────────────────────────

async def clear_history(user_id: int) -> bool:
    if _db is None:
        return False
    uid = str(user_id)

    def _do():
        _db.conn.execute(
            "UPDATE user_history SET messages = '[]' WHERE user_id = ?", (uid,)
        )
        _db.conn.commit()
        return True

    return await _db.run(_do, default=False)


# ── load_all_histories ────────────────────────────────────────────────────────

async def load_all_histories() -> dict[int, list[dict]]:
    """Load all persisted histories at startup. Returns {user_id: [messages]}."""
    if _db is None:
        return {}

    def _do():
        rows = _db.conn.execute(
            "SELECT user_id, messages FROM user_history"
        ).fetchall()
        return {
            int(uid): json.loads(msgs)
            for uid, msgs in rows
            if msgs and msgs != "[]"
        }

    return await _db.run(_do, default={})


# ── cleanup ───────────────────────────────────────────────────────────────────

async def _cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        await _cleanup_inactive()


async def _cleanup_inactive():
    if _db is None:
        return
    cutoff = time.time() - (INACTIVE_DAYS * 86400)

    def _do():
        _db.conn.execute(
            "DELETE FROM user_history WHERE last_active < ? AND last_active > 0", (cutoff,)
        )
        _db.conn.commit()

    await _db.run(_do)
    print(f"🧹 Cleaned up histories inactive for {INACTIVE_DAYS}+ days")