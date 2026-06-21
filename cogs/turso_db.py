"""
Shared Turso/libsql connection helper.

Turso (Hrana protocol) closes a connection's stream after ~10 seconds of
inactivity. Any query sent after that comes back as a 404
"stream not found" / "STREAM_EXPIRED" error — this is normal, permanent,
server-enforced behavior, not a bug, and it cannot be disabled from the
client side. See: https://docs.turso.tech/sdk/http/reference

This module gives every Turso-backed cog (state.py, history.py, memory.py)
one consistent, battle-tested way to:
  1. Open a connection (`connect`)
  2. Detect a dropped-stream error (`is_stream_error`)
  3. Run any DB operation with automatic reconnect + retry, so a dropped
     stream is *always* invisible to the caller and can *never* crash an
     asyncio Task uncaught (`run_with_retry`)
  4. Keep the connection warm so it ideally never goes idle long enough
     to be dropped in the first place (`keepalive_loop`)

Using this in all three cogs means there's exactly one place that knows
how to recover from a dropped stream, instead of three slightly different
copies that can drift out of sync and reintroduce the same class of bug.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, TypeVar

T = TypeVar("T")


def is_stream_error(e: Exception) -> bool:
    """True if `e` looks like a Turso/Hrana dropped-stream error."""
    msg = str(e).lower()
    return (
        "stream not found" in msg
        or "stream_expired" in msg
        or "expired due to inactivity" in msg
        or ("404" in msg and "hrana" in msg)
    )


def connect(turso_url: str, turso_token: str):
    """Open a fresh libsql connection. Raises on failure — caller decides
    what to do (initial connect vs. reconnect have different fallbacks)."""
    import libsql_experimental as libsql
    return libsql.connect(database=turso_url, auth_token=turso_token)


class TursoConnection:
    """Wraps a single libsql connection with self-healing retry.

    Every blocking call goes through `run`, which:
      - executes off the event loop (asyncio.to_thread)
      - on a dropped-stream error, reconnects and retries up to
        `max_retries` times with a short backoff
      - never lets an exception escape uncaught — callers get a clean
        return value (via `default`) instead of a crashed task, and a
        single clearly-logged line instead of a traceback
    """

    def __init__(self, label: str, turso_url: str, turso_token: str,
                 init_fn: Callable[[], None] | None = None):
        """
        label:   short tag for log lines, e.g. "State", "History", "Memory"
        init_fn: optional callable(conn=None) run right after every
                 successful (re)connect — e.g. CREATE TABLE IF NOT EXISTS.
                 Receives no args; read self.conn inside it.
        """
        self.label = label
        self.turso_url = turso_url
        self.turso_token = turso_token
        self.init_fn = init_fn
        self.conn = None
        self._lock = asyncio.Lock()  # serialize reconnects across concurrent callers
        self._last_activity = 0.0

    def connect_sync(self) -> bool:
        """Blocking connect — call via asyncio.to_thread from async code."""
        try:
            self.conn = connect(self.turso_url, self.turso_token)
            if self.init_fn:
                self.init_fn()
            self._last_activity = time.monotonic()
            return True
        except Exception as e:
            print(f"[{self.label}] Connect failed: {e}")
            self.conn = None
            return False

    async def connect_async(self) -> bool:
        if not self.turso_url or not self.turso_token:
            return False
        async with self._lock:
            return await asyncio.to_thread(self.connect_sync)

    async def run(self, fn: Callable[[], T], *, default: T = None,
                   max_retries: int = 2) -> T:
        """Run a blocking DB function with auto-reconnect on dropped streams.

        `fn` must be a zero-arg callable that uses `self.conn` directly,
        e.g.  lambda: self.conn.execute("SELECT 1")
        Always returns — never raises — so it's safe to call from a
        fire-and-forget asyncio.Task without crashing it.
        """
        if self.conn is None:
            return default

        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                result = await asyncio.to_thread(fn)
                self._last_activity = time.monotonic()
                return result
            except Exception as e:
                last_err = e
                if is_stream_error(e):
                    if attempt < max_retries:
                        print(f"[{self.label}] Stream error, reconnecting "
                              f"(attempt {attempt + 1}/{max_retries})...")
                        async with self._lock:
                            # Another concurrent caller may have already
                            # reconnected while we waited for the lock.
                            if time.monotonic() - self._last_activity > 0.5:
                                await asyncio.to_thread(self.connect_sync)
                        if self.conn is None:
                            print(f"[{self.label}] Reconnect failed, giving up.")
                            return default
                        continue  # retry fn on the fresh connection
                    else:
                        print(f"[{self.label}] Still failing after "
                              f"{max_retries} reconnect attempt(s): {e}")
                        return default
                else:
                    # Not a stream error — don't burn retries on it, just log.
                    print(f"[{self.label}] DB error: {e}")
                    return default
        print(f"[{self.label}] Unexpected retry exhaustion: {last_err}")
        return default

    async def keepalive_loop(self, interval: float = 8.0) -> None:
        """Ping every `interval` seconds so the stream stays warm and
        (ideally) never idles out in the first place. Even if it does,
        `run()` on the next real call will transparently recover."""
        while True:
            await asyncio.sleep(interval)
            if self.conn is None:
                continue
            # Skip the ping if a real query already happened recently —
            # no need to double up on traffic.
            if time.monotonic() - self._last_activity < interval * 0.8:
                continue
            await self.run(lambda: self.conn.execute("SELECT 1"), default=None)