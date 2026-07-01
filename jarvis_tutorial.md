# How Jarvis 4.0 Works — A Complete Tutorial

Source: [Phantom3192/Jarvis-4.0](https://github.com/Phantom3192/Jarvis-4.0)

This is a walkthrough of a real, production Discord bot: what it's built with, how the pieces
fit together, and how to run it yourself. There's no README in the repo, so this document is
reverse-engineered directly from the code.

---

## 1. The big picture

Jarvis is a **multipurpose Discord bot** — an AI chatbot, a music player, a games hub (Chess,
Mafia, Hangman, a counting game), a YouTube/image search tool, and a small virtual-economy
system — all running as **one Python process**, with a tiny web API bolted on the side.

```
                     ┌────────────────────────┐
                     │        main.py         │
                     │  starts everything      │
                     └────────────┬─────────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              │                   │                   │
      Discord Gateway      19 Cogs (features)     FastAPI web server
      (bot.start)          loaded as extensions    (uvicorn, same process)
              │                   │                   │
              └─────────┬─────────┘                   │
                        │                              │
                 Turso (libSQL) database  ◄─────────────┘
                 — bans, credits, memory, history, settings
```

Everything runs with `asyncio.gather()` inside `main.py`, so the Discord bot and the web API
share one event loop and one process. This matters later when you deploy it.

---

## 2. Tech stack

| Piece | Library / Service | Purpose |
|---|---|---|
| Discord framework | `discord.py` (with voice extras) | Bot client, slash commands, prefix commands |
| AI chat | `groq` + `google-genai` | Talks to Groq and Gemini APIs |
| Database | `libsql_experimental` (Turso) | Persistent storage, SQLite-compatible, hosted |
| Music | `wavelink` (talks to a Lavalink node) | Voice-channel audio playback |
| YouTube search | `yt_dlp` | Search/trending/video-info (separate from music) |
| Web API | `fastapi` + `uvicorn` | `/api/stats`, `/api/categories` for an external website |
| Chess engine logic | `chess` (python-chess) | Board state, legal moves |
| Images | Pillow, Serper.dev API | Hangman art, Google Image Search |
| Voice encoding | `PyNaCl` | Required by discord.py for voice |

Deployment target: **Railway**, built with **Nixpacks** (`railway.toml` / `nixpacks.toml`).
Nixpacks installs `ffmpeg`, `libopus0`, fonts, and **Node.js 22** specifically because `yt-dlp`
needs a modern Node runtime to solve YouTube's JS-based signature challenges.

---

## 3. Running it yourself

### 3.1 Clone and install

```bash
git clone https://github.com/Phantom3192/Jarvis-4.0.git
cd Jarvis-4.0
pip install -r requirements.txt
```

### 3.2 Environment variables (`.env`)

The bot reads everything from environment variables via `python-dotenv`. Create a `.env` file
in the project root:

```bash
# Required — bot won't start without this
DISCORD_TOKEN=your_discord_bot_token

# AI providers — at least one of these is needed for /chat to work
GROQ_API_KEY=...
GROQ_API_KEY_1=...        # optional — add as many numbered keys as you want (up to 20)
GROQ_API_KEY_2=...
GEMINI_API_KEY=...
GEMINI_API_KEY_2=...

# Database (Turso) — optional but strongly recommended
# Without these, the bot runs in memory-only mode: everything resets on restart
TURSO_URL=libsql://your-db.turso.io
TURSO_TOKEN=...

# Image search
SERPER_API_KEY=...        # Serper.dev supports SERPER_API_KEY2, 3, 4... for key rotation

# Webhooks for logging/reports (all optional)
ERROR_WEBHOOK_URL=...
LOG_WEBHOOK_URL=...
SERVER_LOG_WEBHOOK_URL=...
BUGREPORT_WEBHOOK_URL=...
SUGGESTION_WEBHOOK_URL=...

# Web API
PORT=8000                 # port for the bundled FastAPI server
ALLOWED_ORIGIN=*          # CORS origin for the external website, comma-separated if multiple
```

### 3.3 Start it

```bash
python main.py
```

On boot, `main.py`:
1. Validates `DISCORD_TOKEN` exists.
2. Opens the Turso DB connection (or logs a warning and continues in memory-only mode).
3. Restores conversation history from the DB into memory.
4. Loads all 19 cogs (feature modules) — if one fails to load, it's logged and skipped, the
   rest of the bot still boots.
5. Starts the Discord gateway connection **and** the FastAPI server concurrently.
6. Retries login up to 5 times with exponential backoff if Discord rate-limits it (HTTP 429).

---

## 4. How a message becomes a reply — request lifecycle

Every command, whether typed as `!command` or used as `/command`, goes through the same gate
before it's allowed to run:

```
User sends command
        │
        ▼
 Is the user or their guild globally banned?  ──yes──▶ reply "🚫 banned" + one-time DM, stop
        │ no
        ▼
 Is the caller the bot owner?  ──yes──▶ skip burst/cooldown checks entirely
        │ no
        ▼
 Burst check: 20+ commands in 60s?  ──yes──▶ temp-ban (timeout) for 300s, stop
        │ no
        ▼
 Cooldown check: <2s since last command?  ──yes──▶ react ⏳, stop (silently ignored)
        │ no
        ▼
   Command actually runs
```

This logic lives in `main.py` as a global `@bot.check` (for prefix commands) and
`bot.tree.interaction_check` (for slash commands), and pulls its rules from `cogs/state.py`.
The burst limit, cooldown length, and timeout duration are all configurable at runtime via
`!settings` (owner-only) rather than hardcoded — they're read from a `settings` table with
in-memory caching.

---

## 5. The AI chat system (`cogs/ai.py`)

This is the largest and most complex cog. Here's how `/chat` (or a plain @mention / DM) turns
into a reply.

### 5.1 Multi-provider routing with failover

Jarvis doesn't rely on a single AI API. It maintains **pools of API keys** for two providers:

- **Groq** — serves `openai/gpt-oss-120b` for text and
  `meta-llama/llama-4-scout-17b-16e-instruct` for vision (image) requests. Every
  `GROQ_API_KEY`, `GROQ_API_KEY_1`, `GROQ_API_KEY_2`, ... you set becomes a client in a
  round-robin pool. If a key gets rate-limited, it's "backed off" for 30 seconds and skipped
  until then.
- **Gemini** — `gemini-2.0-flash` and `gemini-2.0-flash-lite`, with the same backoff pattern
  (20s) across up to 2 keys.

Users pick a preference with `/setmodel`:

| Key | What it does |
|---|---|
| `auto` (default) | Tries Groq first, silently falls back to Gemini if Groq fails/times out |
| `groq` | Forces Groq's GPT-OSS 120B (text only, no image support) |
| `gemini-flash` | Google's fast multimodal model — supports image attachments |
| `gemini-lite` | Lightest/fastest Gemini variant |

Model preference is stored **in memory only** (`_user_model` dict) — it resets when the bot
restarts, by design, since it's just a UI preference and not worth a DB write.

Requests use a short 2-second timeout per provider (`PROVIDER_TIMEOUT`) with one quick retry,
specifically so a failing provider doesn't stall the user — it just fails over fast.

### 5.2 System prompt & identity

Every request is built with a system prompt that hardcodes Jarvis's persona:

> "You are Jarvis, a sharp, efficient, and slightly witty AI assistant built for Discord by
> Phantom... If someone asks who made you, say Phantom — but never volunteer it unprompted...
> Never fabricate real-time bot data (ping, uptime, stats) — tell the user to run the actual
> command instead."

Servers can override this per-guild with `/setprompt` (stored in `cogs/state.py`'s `prompts`
table), and reset it with `/resetprompt`.

### 5.3 Conversation history & memory

Two separate systems track "what the AI knows about you":

1. **Short-term conversation history** (`cogs/history.py`) — the last couple of exchanges,
   kept small (`HISTORY_LIMIT = 2`) on purpose so replies stay fast and cheap. Persisted to
   Turso and restored on restart.
2. **Long-term facts memory** (`cogs/memory.py`) — a completely separate system that scans
   every message for patterns like "my name is...", "I live in...", "I like...", "remember
   that...", extracts a fact, and stores it. It's smart about **not duplicating** facts: if you
   say "I'm from India" and later "I'm from Patna, India," it replaces the old fact with the
   more specific one (using word-overlap similarity), rather than storing both. Capped at 20
   facts/user, auto-expires after 90 days of inactivity. View yours with `/mymemory`, wipe it
   with `/forgetme`.

### 5.4 Rate limiting

Every user gets **100 free AI messages per day** (`DAILY_AI_LIMIT`), resetting at midnight UTC.
A warning fires at 80/100. Once you hit the limit, you can either wait for the reset or spend
**50 Jarvis Credits** to reset your counter early (`/mylimit`, `AI_LIMIT_RESET_COST`).

> ⚠️ **Note on the limit-reached message**: it currently advertises `!trivia`, `/wyr`,
> `/truth`, `/dare`, and `/funfact` as things to do instead — none of these commands actually
> exist in the codebase. This looks like a leftover reference to features that were either
> planned or removed; don't be surprised if they 404.

---

## 6. The database layer (`cogs/state.py`, `cogs/turso_db.py`)

Jarvis uses **Turso** (a hosted, distributed SQLite/libSQL service) as its single source of
truth, wrapped by `cogs/turso_db.py`, which exists to solve one specific annoyance: Turso closes
idle connections after ~10 seconds, so every DB-touching cog needs auto-reconnect/retry logic.
Rather than duplicating that logic three times, `state.py`, `history.py`, and `memory.py` all
share one `TursoConnection` helper class that:

- Opens the connection.
- Detects "stream expired" errors specifically.
- Transparently reconnects and retries the query so the caller never sees the error.
- Runs a background keepalive loop to avoid the disconnect happening in the first place.

**In-memory mirroring**: `state.py` loads the whole state table into a Python dict at startup
and serves all reads from memory — writes go to memory immediately and are pushed to Turso via
a **debounced save** (waits 2 seconds after the last change before writing), so rapid-fire
updates don't hammer the database.

If `TURSO_URL`/`TURSO_TOKEN` aren't set, the bot **still runs** — just with no persistence.
Everything (bans, credits, history) resets when the process restarts.

### What's stored

- Global/guild bans, burst-timeout bans
- Per-user AI daily usage counters
- Per-guild custom AI prompts
- Bot settings (cooldowns, burst limits)
- Preferred display names, DND status, reminders
- Music playlists & song history
- Jarvis Credits balances + streaks + referral codes
- Long-term memory facts (separate table, `cogs/memory.py`)
- Conversation history (separate table, `cogs/history.py`)

---

## 7. Jarvis Credits (JC) — the economy system

`cogs/economy.py` defines a virtual currency, 🪙 **Jarvis Credits**, that other cogs plug into.
It has no commands of its own for earning — it's a shared toolbox.

**How you earn JC:**

| Action | Reward |
|---|---|
| First message of the day | 50 JC (daily check-in) |
| Being a brand-new user | 50 JC one-time (onboarding bonus) |
| Chatting with the AI | 5 JC per reply, capped at 100 JC/day |
| Having a suggestion/bug report accepted | 50 JC |
| Someone redeeming your referral code | 50 JC |
| 7-day chat streak | 200 JC bonus |
| 30-day chat streak | 500 JC bonus |

**How you spend JC:**

| Item | Cost |
|---|---|
| Reset your daily AI limit early | 50 JC |
| Save a counting-game streak after a mistake | 25 JC |
| Extra chess hint (beyond the free one per turn) | 15 JC |
| Mystery Box (random 100–300 JC payout) | 200 JC |

Commands: `/balance`, `/leaderboard`, `/streak`, `/shop`, `/mysterybox`, `/invite` (get your
referral code), `/redeem` (use a friend's code — first-time users only), `/transferjc` (send
credits to another user).

---

## 8. Games (`cogs/game.py`)

This one file (~156K) implements four separate games:

### Hangman
Classic word-guessing with an emoji-based ASCII gallows that changes color as you lose lives.
`!hangman` / `/hangman` to start, `!stophangman` to end.

### Counting game
A Turso-backed channel game (count up together, one number per message, don't repeat a user
twice in a row) with a persisted streak that can be protected with JC if broken by mistake.

### Chess (`!chess`, `/chess`)
Full rules via the `python-chess` library. Supports `!move`, `!resign`, `!draw` (offer/accept),
`!undo` (needs opponent agreement), `!hint` (asks the **AI cog** — `generate_ai_response` — for
a suggested move; first hint free, extra hints cost JC), `!chessboard` to redraw the board.

### Mafia ("Upgraded Edition")
The most elaborate feature in the repo. 11 roles: Mafia, Detective, Doctor, Villager,
Vigilante, Jester, Mayor, Serial Killer, Bodyguard, Spy, Escort. Highlights:

- Day/night phases **auto-advance** once everyone has voted/acted, or a timer expires.
- Live-updating countdown embed and a public "X/Y roles submitted" tracker.
- Anonymous voting mode (host's choice), with a full reveal at day's end.
- **Last wills** — players can write a message that's revealed publicly on death.
- A private DM relay so Mafia members can coordinate with each other.
- End-game recap: who killed who, who saved who.

Commands: `!mafia`/`/mafia` (start lobby), `!mafvote`, `!lastwill`, `!stopmafia`,
`!mafguide`/`/mafguide` (rules explainer).

---

## 9. Music (`cogs/music.py`) vs. YouTube search (`cogs/youtube.py`)

These are **two different systems** that both touch YouTube, easy to confuse:

- **`music.py`** plays audio *in a voice channel*, using **wavelink talking to a public
  Lavalink node** — no yt-dlp, no cookies, no local audio processing needed on the bot's own
  server. Commands: `/play`, `/skip`, `/stop`, `/pause`, `/resume`, `/queue`, `/nowplaying`,
  `/volume`, plus owner-only `!force*` overrides for debugging.
- **`youtube.py`** *searches* YouTube and posts rich embeds/links (Discord auto-previews
  YouTube links, so users can watch without leaving the app) — this one **does** use `yt_dlp`
  directly, with a 5-minute in-memory cache to avoid re-scraping on repeated searches.
  Commands: `/youtube` (`!yt`), `/yttrend` (curated trending categories), `/ytinfo` (deep-dive
  video details: description, likes, tags, chapters).

This is why the Nixpacks build installs Node.js 22 — `yt-dlp` needs it to solve YouTube's
JavaScript signature challenges, even though the *music* feature doesn't touch yt-dlp at all.

---

## 10. Other utility cogs

- **`image_search.py`** — `/image` / `!image`, backed by the Serper.dev Google Image Search
  API. Supports multiple `SERPER_API_KEY`, `SERPER_API_KEY2`, ... keys, round-robin rotated.
  Costs JC.
- **`admin.py`** — owner/moderator tools: `!global-ban`/`!global-unban`, `!guild-ban`, cooldown
  and burst-limit configuration (`!set-cooldown`, `!set-burst`), a `!settings` panel.
- **`stats.py`** / **`system.py`** — `/stats` (personal usage), `!ping`, `!uptime`, `!reload`
  (hot-reload all cogs), `!guildinfo`.
- **`prompts.py`** — `/setprompt`, `/resetprompt`, `/viewprompt` for per-server AI personality.
- **`announce.py`** / **`dm.py`** — owner-only mass DM / targeted DM tools.
- **`suggestions.py`** / **`bugreport.py`** — feedback intake, routed to a Discord webhook,
  rewards JC when accepted.
- **`help.py`** — the `/help` and admin-help UI, using category dropdowns. Its `CATEGORIES`
  dict is imported directly by the web API so the in-Discord help menu and an external website
  can never drift out of sync.
- **`summary.py`** — `/summary` asks the AI to summarize your recent conversation history.
- **`errorhandler.py`** — forwards uncaught errors (and any `print()` line that looks like an
  error) to a Discord webhook, and silences harmless "expired interaction" warnings from stale
  button clicks.
- **`http_session.py`** — one shared `aiohttp.ClientSession` for the whole bot, instead of
  opening a new one per request.
- **`message_splitter.py`** — safely splits any AI reply longer than Discord's 2000-character
  limit across multiple messages.

---

## 11. The bundled web API (`web/app.py`)

A deliberately tiny FastAPI app, run as a background task inside the *same process* as the
Discord bot (started from `main.py` via `uvicorn.Server(...).serve()`). It exposes exactly
three routes:

- `GET /api/stats` — live guild count, user count, uptime, latency, usage stats.
- `GET /api/categories` — the same category data `/help` uses, imported directly from
  `cogs/help.py` so it can never go out of sync with the real command list.
- `GET /healthz` — trivial health check.

The actual marketing website (HTML/CSS/JS) is a **separate project**, deployed independently,
that just polls these two JSON routes over HTTP. Sharing the process with the bot means these
routes can read `bot.guilds` directly with zero network hop or extra database query.

---

## 12. Deployment (Railway)

`railway.toml` tells Railway to build with Nixpacks and run `python main.py`.
`nixpacks.toml` adds system packages the bot needs that aren't part of a normal Python
buildpack:

```toml
aptPkgs = ["fonts-freefont-ttf", "fontconfig", "ffmpeg", "libopus0"]
nixPkgs = ["...", "nodejs_22"]
```

- `ffmpeg` / `libopus0` — audio processing/encoding for voice.
- Fonts — used for generating any image-based output (e.g. Hangman art).
- `nodejs_22` — required by `yt-dlp` (used in `youtube.py`) to solve YouTube's JS challenges;
  Debian's default `nodejs` package is too old (v18) for this.

---

## 13. Known rough edges (as of the current `main` branch)

If you're picking this codebase up to extend it, worth knowing about upfront:

1. **A currently-broken test.** `tests/test_admin_panel.py` imports
   `build_admin_panel_payload` from `web/app.py`, but that function doesn't exist in the
   current `web/app.py` — running `pytest` fails at collection. This looks like a regression
   from the most recent commit ("Removed 'Working on it' part"), which likely stripped an
   admin-panel feature without cleaning up its test.
2. **Dead command references.** The AI daily-limit message (`cogs/ai.py`) advertises
   `!trivia`, `/wyr`, `/truth`, `/dare`, `/funfact` — none of these are implemented anywhere.
3. **A stale docstring.** `music.py`'s module docstring says "no yt-dlp" — true for that file
   specifically (it uses wavelink/Lavalink), but `youtube.py` *does* use `yt_dlp` directly for
   search/trending, which is why Node.js 22 is still a build requirement.

---

## 14. Quick command cheat-sheet

| Category | Example commands |
|---|---|
| AI Chat | `/chat`, `/setmodel`, `/mymemory`, `/forgetme`, `/clearhistory`, `/remindme` |
| Economy | `/balance`, `/shop`, `/leaderboard`, `/streak`, `/invite`, `/redeem` |
| Games | `/chess`, `/mafia`, `/hangman`, `!move`, `!mafvote` |
| Music | `/play`, `/skip`, `/queue`, `/volume` |
| YouTube | `/youtube`, `/yttrend`, `/ytinfo` |
| Utility | `/help`, `/stats`, `!ping`, `!uptime`, `/image` |
| Server admin | `/setprompt`, `!settings`, `!guild-ban` |
| Bot owner only | `!global-ban`, `!global-announce`, `!reload`, `!forcejoin` |

---

*This tutorial reflects the state of the `main` branch as of commit `9f7e93e`
("Removed 'Working on it' part").*
