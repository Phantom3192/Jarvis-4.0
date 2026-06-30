"""
Jarvis website — landing page, live stats API, and auto-generated docs.

Runs INSIDE the same process as the bot (see main.py), started as a second
asyncio task alongside bot.start(). Because it shares the process, route
handlers can read bot.guilds / seen_users / _START_TIME directly — no
database round-trip, no second deployment.

Docs content is NOT duplicated here. It's imported straight from
cogs/help.py's CATEGORIES dict, so the Discord !help menu and the website
docs page can never drift out of sync — one source of truth.
"""
import os
import re
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

WEB_DIR = Path(__file__).parent

# Bot client ID used to build the OAuth2 "Add to Server" invite link.
# Set DISCORD_CLIENT_ID in your .env / Railway variables.
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
INVITE_PERMISSIONS = "414531833920"  # send/embed/history/react/connect/speak/manage messages
INVITE_URL = (
    f"https://discord.com/oauth2/authorize?client_id={DISCORD_CLIENT_ID}"
    f"&permissions={INVITE_PERMISSIONS}&scope=bot%20applications.commands"
    if DISCORD_CLIENT_ID else "#"
)


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
    app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None)

    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    templates.env.filters["md_bold"] = lambda s: re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s or "")

    # Curated subset shown in the hero feature grid — full list lives in the docs section.
    HIGHLIGHT_KEYS = ["🤖 AI", "🧠 Memory", "♟️ Games", "🎵 Music", "🪙 Jarvis Credits", "⏰ Reminders"]

    # Lazy imports — avoids circular import issues at module load time,
    # and lets the site still boot even if a cog fails to load.
    def _get_categories():
        from cogs.help import CATEGORIES
        return CATEGORIES

    def _get_seen_users():
        from cogs.state import seen_users
        return seen_users

    def _get_start_time():
        from cogs.system import _START_TIME
        return _START_TIME

    @app.get("/", response_class=None)
    async def home(request: Request):
        categories = _get_categories()
        highlights = {k: categories[k] for k in HIGHLIGHT_KEYS if k in categories}
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "categories": categories,
                "highlights": highlights,
                "invite_url": INVITE_URL,
                "bot_name": bot.user.name if bot.user else "Jarvis",
            },
        )

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
        })

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return app