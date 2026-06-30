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

Keeping this split means the bot and the website can be deployed,
scaled, and restarted completely independently.
"""
import os
import time

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


def create_app(bot) -> FastAPI:
    app = FastAPI(title="Jarvis API", docs_url=None, redoc_url=None)

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

        return JSONResponse({
            "guilds": guild_count,
            "users": user_count,
            "uptime_seconds": round(uptime_seconds),
            "uptime_human": _fmt_uptime(uptime_seconds),
            "latency_ms": latency_ms,
            "online": bot.is_ready() if hasattr(bot, "is_ready") else True,
            "bot_name": bot.user.name if bot.user else "Jarvis",
        })

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return app