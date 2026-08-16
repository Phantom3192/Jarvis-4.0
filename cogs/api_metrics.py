"""
api_metrics.py — Lightweight rolling-window metrics behind the owner-only
!api / /api dashboard (cogs/system.py).

Three timing categories are tracked, each fed from a real hot path already
in the codebase — nothing here is simulated:

  - inference  : every Groq/Gemini AI call, timed in cogs/ai.py
  - db         : every Turso round-trip, timed in cogs/turso_db.py
  - command    : full prefix-command handling time, timed in cogs/system.py

Each category keeps its last _WINDOW_SECONDS of samples in a deque (for
fastest/slowest/avg) plus an all-time running count and latest value (for
the counters that shouldn't reset just because traffic went quiet for a
few minutes). Recording a sample is just an append + a cheap prune, so
it's safe to call from any hot path without adding meaningful latency.
"""
from __future__ import annotations

import time
from collections import deque

_WINDOW_SECONDS = 300.0  # 5 min rolling window — matches the embed footer


class _RollingStat:
    __slots__ = ("_samples", "all_time_count", "all_time_latest")

    def __init__(self) -> None:
        self._samples: deque[tuple[float, float]] = deque()  # (monotonic_ts, latency_ms)
        self.all_time_count = 0
        self.all_time_latest: float | None = None

    def record(self, latency_ms: float) -> None:
        now = time.monotonic()
        self._samples.append((now, latency_ms))
        self.all_time_count += 1
        self.all_time_latest = latency_ms
        self._prune(now)

    def _prune(self, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        cutoff = now - _WINDOW_SECONDS
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def window_values(self) -> list[float]:
        self._prune()
        return [v for _, v in self._samples]


_inference = _RollingStat()
_db = _RollingStat()
_command = _RollingStat()

_active_users: dict[int, float] = {}  # user_id -> last-seen (monotonic)


# ── recording (called from ai.py / turso_db.py / system.py) ─────────────────

def record_inference(latency_ms: float) -> None:
    _inference.record(latency_ms)


def record_db(latency_ms: float) -> None:
    _db.record(latency_ms)


def record_command(latency_ms: float, user_id: int | None = None) -> None:
    _command.record(latency_ms)
    if user_id is not None:
        touch_activity(user_id)


def touch_activity(user_id: int) -> None:
    _active_users[user_id] = time.monotonic()


def _prune_active_users() -> None:
    cutoff = time.monotonic() - _WINDOW_SECONDS
    for uid in [u for u, ts in _active_users.items() if ts < cutoff]:
        del _active_users[uid]


# ── formatting helpers ───────────────────────────────────────────────────────

def _fmt_ms(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        return f"{value / 1000:.3f}s"
    return f"{value:.0f} ms"


def _activity_label(events_per_5min: int) -> tuple[str, str]:
    per_min = events_per_5min / (_WINDOW_SECONDS / 60.0)
    if per_min >= 20:
        return "🔴", "Very High Activity"
    if per_min >= 8:
        return "🟠", "High Activity"
    if per_min >= 2:
        return "🟡", "Moderate Activity"
    if per_min > 0:
        return "🟢", "Low Activity"
    return "⚪", "Idle"


# ── embed builder ────────────────────────────────────────────────────────────

def build_api_embed(bot, *, model_label: str, backend_label: str):
    import discord

    _prune_active_users()

    infer_vals = _inference.window_values()
    db_vals = _db.window_values()
    cmd_vals = _command.window_values()

    dot, activity_text = _activity_label(len(infer_vals) + len(cmd_vals))

    embed = discord.Embed(
        title="📡 API Status",
        color=discord.Color.from_str("#e91e8c"),
        timestamp=discord.utils.utcnow(),
    )

    embed.add_field(name="Activity", value=f"{dot} {activity_text}", inline=True)
    embed.add_field(name="Active Users (5m)", value=f"{len(_active_users)}", inline=True)
    embed.add_field(name="DB (latest)", value=_fmt_ms(_db.all_time_latest), inline=True)

    embed.add_field(
        name="DB (avg)",
        value=_fmt_ms(sum(db_vals) / len(db_vals)) if db_vals else "—",
        inline=True,
    )
    embed.add_field(name="Commands (5m)", value=f"{len(cmd_vals)}", inline=True)
    embed.add_field(name="AI Calls (5m)", value=f"{len(infer_vals)}", inline=True)

    embed.add_field(
        name="Inference Fastest",
        value=_fmt_ms(min(infer_vals)) if infer_vals else "—",
        inline=True,
    )
    embed.add_field(
        name="Inference Slowest",
        value=_fmt_ms(max(infer_vals)) if infer_vals else "—",
        inline=True,
    )
    embed.add_field(
        name="Inference Avg",
        value=_fmt_ms(sum(infer_vals) / len(infer_vals)) if infer_vals else "—",
        inline=True,
    )

    embed.add_field(name="Inference Latest", value=_fmt_ms(_inference.all_time_latest), inline=True)
    embed.add_field(
        name="Response Fastest",
        value=_fmt_ms(min(cmd_vals)) if cmd_vals else "—",
        inline=True,
    )
    embed.add_field(
        name="Response Slowest",
        value=_fmt_ms(max(cmd_vals)) if cmd_vals else "—",
        inline=True,
    )

    embed.add_field(
        name="Response Avg",
        value=_fmt_ms(sum(cmd_vals) / len(cmd_vals)) if cmd_vals else "—",
        inline=True,
    )
    embed.add_field(name="Response Latest", value=_fmt_ms(_command.all_time_latest), inline=True)
    embed.add_field(name="AI Replies (total)", value=f"{_inference.all_time_count:,}", inline=True)

    embed.set_footer(text=f"Model: {model_label}  |  Backend: {backend_label}  |  Window: 5 min rolling")
    return embed
