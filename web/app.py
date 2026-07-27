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
import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
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

    def _get_ping_stats():
        from cogs.system import get_ping_stats
        return get_ping_stats()

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
        # in), this briefly serves an empty list rather than blocking the
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
            ping = _get_ping_stats()
            latency_ms = ping.get("ws_ms")
            api_latency_ms = ping.get("api_ms")
        except Exception:
            latency_ms = None
            api_latency_ms = None
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
            "api_latency_ms": api_latency_ms,
            "online": bot.is_ready() if hasattr(bot, "is_ready") else True,
            "bot_name": bot.user.name if bot.user else "Jarvis",
            "usage": usage,
        })

    @app.post("/webhook/topgg")
    async def webhook_topgg(request: Request):
        """top.gg calls this every time someone votes for the bot (or when
        the "Send Test" button on their Webhooks dashboard is used).

        This is top.gg's *current* (v1) webhook scheme, which signs each
        request with HMAC-SHA256 instead of the old plain shared-secret
        Authorization header:

            X-Topgg-Signature: t=<unix ts>,v1=<hex hmac>

        The signed content is "{timestamp}.{raw request body}", HMAC'd with
        TOPGG_WEBHOOK_AUTH (the "whs_..." secret shown on top.gg's Webhooks
        page for this listing) as the key. Reference implementation:
        https://github.com/top-gg/webhooks-v2-nodejs-example

        The payload shape also changed — events now look like
        {"type": "vote.create", "data": {...}} instead of the old flat
        {"type": "upvote", "user": "..."}. The voter's Discord ID lives at
        data.user.platform_id (data.user.id is top.gg's own internal ID,
        NOT the Discord ID — using it by mistake would silently credit the
        wrong/nonexistent account). See https://docs.top.gg/webhooks/events
        """
        secret = os.getenv("TOPGG_WEBHOOK_AUTH", "")
        if not secret:
            print("⚠️ /webhook/topgg hit but TOPGG_WEBHOOK_AUTH is not set — rejecting.", flush=True)
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        signature_header = request.headers.get("x-topgg-signature", "")
        if not signature_header:
            print("⚠️ /webhook/topgg hit with no X-Topgg-Signature header — rejecting.", flush=True)
            return JSONResponse({"error": "missing signature"}, status_code=401)

        try:
            sig_parts = dict(part.split("=", 1) for part in signature_header.split(","))
            timestamp = sig_parts["t"]
            signature = sig_parts["v1"]
        except (KeyError, ValueError):
            print(f"⚠️ /webhook/topgg malformed signature header: {signature_header!r} — rejecting.", flush=True)
            return JSONResponse({"error": "invalid signature format"}, status_code=400)

        # Must HMAC the exact raw bytes top.gg sent — re-serializing the
        # parsed JSON would very likely produce a different byte sequence
        # (key order, spacing) and make every signature fail to match.
        raw_body = await request.body()
        expected_digest = hmac.new(
            secret.encode("utf-8"),
            f"{timestamp}.{raw_body.decode('utf-8')}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected_digest, signature):
            print("⚠️ /webhook/topgg signature mismatch — rejecting.", flush=True)
            return JSONResponse({"error": "invalid signature"}, status_code=403)

        try:
            payload = json.loads(raw_body)
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        event_type = payload.get("type")
        data = payload.get("data") or {}

        if event_type == "webhook.test":
            # top.gg's "Test" button on the webhook page — signature already
            # verified above, so this just confirms end-to-end connectivity.
            print("✅ /webhook/topgg test event verified successfully.", flush=True)
            return JSONResponse({"status": "ok"})

        if event_type != "vote.create":
            # Unknown/future event type — acknowledge so top.gg doesn't keep
            # retrying, but there's nothing for us to record.
            return JSONResponse({"status": "ignored"})

        user_id_raw = (data.get("user") or {}).get("platform_id")
        if not user_id_raw:
            return JSONResponse({"error": "missing user"}, status_code=400)

        try:
            user_id = int(user_id_raw)
        except (TypeError, ValueError):
            return JSONResponse({"error": "invalid user id"}, status_code=400)

        from cogs.vote import record_vote
        stats = record_vote(user_id)
        print(
            f"🗳️ Vote recorded for {user_id} — "
            f"streak {stats['streak']}, total {stats['total_votes']}, "
            f"pending boxes {stats['pending_boxes']}",
            flush=True,
        )

        return JSONResponse({"status": "ok"})

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return app