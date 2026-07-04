"""
Jarvis bot API — a tiny public JSON API exposing live stats and the docs
category list, nothing else.

This runs INSIDE the bot process (see main.py), started as a second asyncio
task alongside bot.start(). Because it shares the process, route handlers
can read bot.guilds / seen_users / _START_TIME directly — no database
round-trip needed.

The actual landing page (HTML/CSS/JS) lives in a SEPARATE project/repo and
is deployed as its own Railway service. That website polls the two routes
below over plain HTTP:

    GET /api/stats       -> live guild/user/uptime/latency numbers
    GET /api/categories  -> the !help category data (single source of
                             truth — imported straight from cogs/help.py,
                             so the Discord !help menu and the website docs
                             page can never drift out of sync)
    GET /api/leaderboard -> top Jarvis Credit holders (username + avatar
                             resolved via the Discord API. Refreshed by a
                             background loop every _LEADERBOARD_REFRESH_SECS
                             — NOT triggered by website requests — so no
                             matter how many visitors hit the site at once,
                             Discord's API only ever gets called on a fixed
                             schedule, never scaled by traffic.

Keeping this split means the bot and the website can be deployed,
scaled, and restarted completely independently.
"""
import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse


def _fmt_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


LEADERBOARD_SIZE = 10
_LEADERBOARD_REFRESH_SECS = 60.0  # how often the background loop below
                                    # re-pulls balances + resolves Discord
                                    # usernames — independent of how many
                                    # people are visiting the website.
_leaderboard_cache: dict = {"data": [], "ts": 0.0}


def create_app(bot) -> FastAPI:
    leaderboard_task: dict = {"task": None}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        leaderboard_task["task"] = asyncio.create_task(_refresh_leaderboard_loop())
        try:
            yield
        finally:
            task = leaderboard_task["task"]
            if task:
                task.cancel()

    app = FastAPI(title="Jarvis API", docs_url=None, redoc_url=None, lifespan=lifespan)

    # The website is a separate domain/project, so the browser-facing fetch
    # calls it makes need CORS. Lock this down to your website's real domain
    # in production via the ALLOWED_ORIGIN env var (comma-separated if you
    # ever run more than one frontend) — defaults to "*" so things work
    # out of the box.
    allowed_origins = os.getenv("ALLOWED_ORIGIN", "*")
    origins = [o.strip() for o in allowed_origins.split(",")] if allowed_origins != "*" else ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    # Lazy imports — avoids circular import issues at module load time,
    # and lets the API still boot even if a cog fails to load.
    def _get_categories():
        from cogs.help import CATEGORIES
        return CATEGORIES

    def _get_seen_users():
        from cogs.state import seen_users
        return seen_users

    def _get_start_time():
        from cogs.system import _START_TIME
        return _START_TIME

    def _get_usage_stats():
        from cogs.system import get_usage_stats
        return get_usage_stats()

    async def _build_leaderboard() -> list[dict]:
        from cogs.state import get_all_credits

        balances = get_all_credits()
        ranked = sorted(
            ((uid, bal) for uid, bal in balances.items() if bal > 0),
            key=lambda kv: kv[1],
            reverse=True,
        )[:LEADERBOARD_SIZE]

        entries = []
        for rank, (uid, bal) in enumerate(ranked, start=1):
            user = bot.get_user(int(uid))
            if user is None:
                try:
                    user = await bot.fetch_user(int(uid))
                except Exception:
                    user = None
            entries.append({
                "rank": rank,
                "user_id": str(uid),
                "name": user.display_name if user else f"User {uid}",
                "credits": bal,
            })
        return entries

    async def _refresh_leaderboard_loop() -> None:
        """Runs on a fixed schedule for the whole life of the process —
        NOT triggered by website requests. This is what keeps Discord API
        usage (fetch_user for uncached users) constant regardless of how
        many people are hitting the website at once: a viral traffic spike
        still only costs one refresh every _LEADERBOARD_REFRESH_SECS,
        exactly like a quiet day.
        """
        await bot.wait_until_ready()  # bot.get_user/fetch_user need a live session
        while True:
            try:
                entries = await _build_leaderboard()
                _leaderboard_cache["data"] = entries
                _leaderboard_cache["ts"] = time.monotonic()
            except Exception as e:
                print(f"⚠️ Leaderboard background refresh failed: {e}")
                # Keep serving whatever's already cached — never let a
                # failed refresh blank out the leaderboard.
            await asyncio.sleep(_LEADERBOARD_REFRESH_SECS)

    @app.get("/api/leaderboard")
    async def api_leaderboard():
        # Pure cache read — this endpoint never calls Discord itself. If
        # the background loop hasn't completed its first refresh yet
        # (e.g. right after a restart, before the bot has fully logged
        # in), this briefly serves an empty list rather than blocking the.
        # request on a live Discord fetch.
        entries = _leaderboard_cache["data"]

        return JSONResponse({
            "bot_name": bot.user.name if bot.user else "Jarvis",
            "currency_name": "Jarvis Credit",
            "currency_emoji": "🪙",
            "leaderboard": entries,
        })

    @app.get("/api/categories")
    async def api_categories():
        try:
            categories = _get_categories()
        except Exception:
            categories = {}
        return JSONResponse({
            "bot_name": bot.user.name if bot.user else "Jarvis",
            "categories": categories,
        })

    @app.get("/api/stats")
    async def api_stats():
        try:
            guild_count = len(bot.guilds)
        except Exception:
            guild_count = 0
        try:
            user_count = len(_get_seen_users())
        except Exception:
            user_count = 0
        try:
            uptime_seconds = time.monotonic() - _get_start_time()
        except Exception:
            uptime_seconds = 0
        try:
            latency_ms = round(bot.latency * 1000) if bot.latency == bot.latency else None  # NaN guard
        except Exception:
            latency_ms = None
        try:
            usage = _get_usage_stats()
        except Exception:
            usage = {"available": False}

        return JSONResponse({
            "guilds": guild_count,
            "users": user_count,
            "uptime_seconds": round(uptime_seconds),
            "uptime_human": _fmt_uptime(uptime_seconds),
            "latency_ms": latency_ms,
            "online": bot.is_ready() if hasattr(bot, "is_ready") else True,
            "bot_name": bot.user.name if bot.user else "Jarvis",
            "usage": usage,
        })

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return app