"""
AI cog — handles all chat interactions with Jarvis.

CHANGES vs previous version:
- Per-user model preference stored in _user_model (in-memory dict).
  Users pick via /setmodel or !setmodel. Preference is reset on bot restart
  (intentionally lightweight — no DB needed for a UI preference).
- generate_ai_response now reads the user's preferred model and routes
  to that provider first, falling back to others if it fails.
- MODELS dict is the single source of truth for available choices.
- !ping / !uptime moved to system.py — not duplicated here.
"""
import discord
from discord.ext import commands
from discord import app_commands
import os
import re
from collections import defaultdict
import groq
import google.genai as genai
import base64
from cogs.state import (
    is_bot_banned, is_new_user, mark_seen, record_message, get_guild_prompt,
    is_ai_rate_limited, increment_ai_usage, get_ai_usage,
    DAILY_AI_LIMIT, WARN_AT,
)
from cogs.message_splitter import send_long_message, edit_or_send_long_message
from cogs.http_session import get_session

# ── API keys & models ─────────────────────────────────────────────────────────

GROQ_API_KEY     = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY")
GEMINI_API_KEY_2 = os.getenv("GEMINI_API_KEY_2")

GROQ_MODEL_TEXT    = "llama-3.3-70b-versatile"
GROQ_MODEL_VISION  = "meta-llama/llama-4-scout-17b-16e-instruct"
GEMINI_MODEL_FLASH = "gemini-2.0-flash"
GEMINI_MODEL_LITE  = "gemini-2.0-flash-lite"

HISTORY_LIMIT = 10
MAX_TOKENS    = 800

# ── Available models for /setmodel ───────────────────────────────────────────
# key        → internal identifier stored per user
# "label"    → shown in Discord UI
# "desc"     → shown in the info embed
# "supports_vision" → whether this model can handle image attachments

MODELS: dict[str, dict] = {
    "auto": {
        "label":           "🔄 Auto (default)",
        "desc":            "Tries Groq Llama first, falls back to Gemini automatically.",
        "supports_vision": True,
    },
    "groq": {
        "label":           "⚡ Groq — Llama 3.3 70B",
        "desc":            "Fast. Best for quick questions and conversation.",
        "supports_vision": False,
    },
    "gemini-flash": {
        "label":           "✨ Gemini 2.0 Flash",
        "desc":            "Google's fast multimodal model. Supports images.",
        "supports_vision": True,
    },
    "gemini-lite": {
        "label":           "🪶 Gemini 2.0 Flash-Lite",
        "desc":            "Lightest model. Best for simple tasks when speed matters.",
        "supports_vision": True,
    },
}

MODEL_CHOICES = [
    app_commands.Choice(name=v["label"], value=k)
    for k, v in MODELS.items()
]

# ── Per-user model preference (in-memory) ─────────────────────────────────────
# Resets on restart — intentional, no DB overhead needed for a UI preference.
_user_model: dict[int, str] = {}

def get_user_model(user_id: int) -> str:
    return _user_model.get(user_id, "auto")

def set_user_model(user_id: int, model_key: str) -> None:
    _user_model[user_id] = model_key

# ── System prompt ─────────────────────────────────────────────────────────────

DEFAULT_SYSTEM_PROMPT = (
    "You are Jarvis, a sharp, efficient, and slightly witty AI assistant built for Discord by Phantom. "
    "If someone asks who made you or who your creator is, say Phantom — but never volunteer or repeat this unprompted. "
    "Keep responses concise and helpful. Do not start sentences with someone's name. Avoid unnecessary filler."
)

SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES       = 20 * 1024 * 1024
RATE_LIMIT_MSG        = "⚠️ All AI models are currently rate limited. Please try again in a few minutes."

DAILY_LIMIT_MSG = (
    "⏳ **You've reached your daily Jarvis AI limit!**\n\n"
    "You've used all **{limit}** of your free AI messages for today. "
    "Your limit resets at **midnight UTC** — come back then and I'll be ready.\n\n"
    "In the meantime, here's what you can still do:\n"
    "🎮 **`!trivia`** or **`/trivia`** — test your knowledge with a trivia question\n"
    "🪢 **`!hangman`** or **`/hangman`** — play a round of hangman\n"
    "🤔 **`/wyr`** — Would You Rather? fun for the whole server\n"
    "🫣 **`/truth`** or **`/dare`** — classic truth or dare\n"
    "🤯 **`/funfact`** — get a random fun fact\n"
    "📊 **`/stats`** — check your Jarvis usage stats\n"
    "💡 **`!feedback`** — share ideas or suggestions with the developer\n\n"
    "See you tomorrow! 👋"
)

WARN_LIMIT_MSG = (
    "⚠️ **Heads up** — you've used **{count}/{limit}** AI messages today. "
    "You have **{remaining}** left before your limit resets at midnight UTC."
)

# ── Compiled regex ────────────────────────────────────────────────────────────

_MENTION_RE = re.compile(r"<@!?\d+>")
_JARVIS_RE  = re.compile(r"\bjarvis\b", re.IGNORECASE)

# ── Groq client ───────────────────────────────────────────────────────────────

_groq_client: groq.AsyncGroq | None = (
    groq.AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
)

# ── Gemini client cache (new google-generativeai API) ──────────────────────
# The newer SDK doesn't use configure() — we pass api_key directly to GenerativeModel
_gemini_clients: dict[str, object] = {}

# ── History stores ────────────────────────────────────────────────────────────

private_history: dict[int, list[dict]] = defaultdict(list)
group_history:   dict[int, list[dict]] = defaultdict(list)
active_groups:   dict[int, set[int]]   = {}


def _is_in_group(user_id: int, channel_id: int) -> bool:
    return channel_id in active_groups and user_id in active_groups[channel_id]

def _get_history(user_id: int, channel_id: int) -> list[dict]:
    return group_history[channel_id] if _is_in_group(user_id, channel_id) else private_history[user_id]

def _trim(history: list[dict]) -> None:
    if len(history) > HISTORY_LIMIT:
        del history[:-HISTORY_LIMIT]

def clear_history(user_id: int, channel_id: int | None = None) -> None:
    if channel_id and _is_in_group(user_id, channel_id):
        group_history[channel_id].clear()
    else:
        private_history[user_id].clear()

def start_group(channel_id: int, user_ids: list[int]) -> None:
    active_groups[channel_id] = set(user_ids)
    group_history[channel_id].clear()

def end_group(channel_id: int) -> None:
    active_groups.pop(channel_id, None)
    group_history[channel_id].clear()

def get_group_members(channel_id: int) -> set[int] | None:
    return active_groups.get(channel_id)


# ── Webhook logger ────────────────────────────────────────────────────────────

async def _log_new_user(user: discord.User | discord.Member) -> None:
    webhook_url = os.getenv("LOG_WEBHOOK_URL", "")
    if not webhook_url:
        return
    try:
        session = get_session()
        webhook = discord.Webhook.from_url(webhook_url, session=session)
        embed   = discord.Embed(
            title="✨ New Jarvis User",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Username",    value=str(user),      inline=True)
        embed.add_field(name="User ID",     value=f"`{user.id}`", inline=True)
        embed.add_field(name="Account Age", value=discord.utils.format_dt(user.created_at, style="R"), inline=True)
        embed.add_field(
            name="Quick Actions",
            value=f"`!global-ban {user.id} reason`\n`!global-unban {user.id}`",
            inline=False,
        )
        embed.set_footer(text="First ever interaction with Jarvis")
        await webhook.send(embed=embed, username="Jarvis Logs")
    except Exception as e:
        print(f"❌ Webhook error _log_new_user: {e}")


# ── Image fetch ───────────────────────────────────────────────────────────────

async def _fetch_image(attachment: discord.Attachment) -> tuple[str, str] | None:
    content_type = (attachment.content_type or "").split(";")[0].strip().lower()
    if content_type not in SUPPORTED_IMAGE_TYPES or attachment.size > MAX_IMAGE_BYTES:
        return None
    try:
        session = get_session()
        async with session.get(attachment.url) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
        return base64.b64encode(data).decode("utf-8"), content_type
    except Exception:
        return None


# ── AI providers ──────────────────────────────────────────────────────────────

async def _try_groq(messages: list[dict], system_prompt: str) -> str | None:
    if not _groq_client:
        return None
    try:
        resp = await _groq_client.chat.completions.create(
            model=GROQ_MODEL_TEXT,
            messages=[{"role": "system", "content": system_prompt}] + messages,
            max_tokens=MAX_TOKENS,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e)[:100]  # First 100 chars
        print(f"[Groq API Error] {error_type}: {error_msg}")
        return None


async def _try_groq_vision(
    messages: list[dict],
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
) -> str | None:
    if not _groq_client:
        return None
    try:
        history = [{"role": "system", "content": system_prompt}] + messages[:-1]
        content: list[dict] = []
        if user_text:
            content.append({"type": "text", "text": user_text})
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{image_b64}"},
        })
        history.append({"role": "user", "content": content})
        resp = await _groq_client.chat.completions.create(
            model=GROQ_MODEL_VISION,
            messages=history,
            max_tokens=MAX_TOKENS,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e)[:100]  # First 100 chars
        print(f"[Groq Vision Error] {error_type}: {error_msg}")
        return None


async def _try_gemini(
    api_key: str | None,
    model_name: str,
    messages: list[dict],
    system_prompt: str,
    image_b64: str | None = None,
    media_type: str | None = None,
) -> str | None:
    if not api_key:
        return None
    try:
        # Use the newer google-generativeai API — pass api_key directly to GenerativeModel
        model   = genai.GenerativeModel(model_name=model_name, api_key=api_key, system_instruction=system_prompt)
        history = [
            {"role": "user" if m["role"] == "user" else "model", "parts": [m["content"]]}
            for m in messages[:-1]
        ]
        chat = model.start_chat(history=history)
        last = messages[-1]["content"]
        if image_b64 and media_type:
            parts = (([last] if last else []) +
                     [{"mime_type": media_type, "data": base64.b64decode(image_b64)}])
            resp = await chat.send_message_async(parts)
        else:
            resp = await chat.send_message_async(last)
        return resp.text.strip()
    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e)[:100]  # First 100 chars
        print(f"[Gemini {model_name} Error] {error_type}: {error_msg}")
        return None


# ── Core response ─────────────────────────────────────────────────────────────

async def generate_ai_response(
    user_id: int,
    user_message: str,
    channel_id: int,
    guild_id: int | None = None,
    image_b64: str | None = None,
    media_type: str | None = None,
    user: discord.User | discord.Member | None = None,
) -> str:
    if is_ai_rate_limited(user_id):
        return DAILY_LIMIT_MSG.format(limit=DAILY_AI_LIMIT)

    base_prompt = get_guild_prompt(guild_id) or DEFAULT_SYSTEM_PROMPT
    in_group    = _is_in_group(user_id, channel_id)

    if user:
        name     = user.display_name if user.display_name != user.name else user.name
        username = user.name
        if in_group:
            n = len(active_groups[channel_id])
            system_prompt = (
                base_prompt +
                f"\n\nThis is a shared group conversation between {n} people. "
                f"Messages are prefixed with the sender's name so you know who said what. "
                f"The person who just sent this message is {name} (username: {username}). "
                f"Only mention their name/username if they directly ask about their own identity."
            )
        else:
            system_prompt = (
                base_prompt +
                f"\n\nThe person messaging you is {name} (username: {username}). "
                f"Only reveal this if they directly ask who they are. "
                f"Never volunteer their name or username unprompted."
            )
    else:
        system_prompt = base_prompt

    history  = _get_history(user_id, channel_id)
    has_image = bool(image_b64 and media_type)

    stored = (
        f"[{user.display_name}]: {user_message or '(sent an image)'}"
        if (in_group and user)
        else (user_message or "(sent an image)")
    )
    history.append({"role": "user", "content": stored})
    _trim(history)

    # ── Route based on user's preferred model ─────────────────────────────────
    preferred = get_user_model(user_id)

    if preferred == "groq":
        if has_image:
            # Groq vision → Gemini fallback
            reply = (
                await _try_groq_vision(history, system_prompt, image_b64, media_type, user_message)
                or await _try_gemini(GEMINI_API_KEY,  GEMINI_MODEL_FLASH, history, system_prompt, image_b64, media_type)
                or await _try_gemini(GEMINI_API_KEY_2, GEMINI_MODEL_FLASH, history, system_prompt, image_b64, media_type)
            )
        else:
            reply = (
                await _try_groq(history, system_prompt)
                or await _try_gemini(GEMINI_API_KEY,  GEMINI_MODEL_FLASH, history, system_prompt)
                or await _try_gemini(GEMINI_API_KEY_2, GEMINI_MODEL_FLASH, history, system_prompt)
            )

    elif preferred == "gemini-flash":
        reply = (
            await _try_gemini(GEMINI_API_KEY,  GEMINI_MODEL_FLASH, history, system_prompt, image_b64, media_type)
            or await _try_gemini(GEMINI_API_KEY_2, GEMINI_MODEL_FLASH, history, system_prompt, image_b64, media_type)
            or await _try_groq(history, system_prompt)   # text fallback only
        )

    elif preferred == "gemini-lite":
        reply = (
            await _try_gemini(GEMINI_API_KEY,  GEMINI_MODEL_LITE, history, system_prompt, image_b64, media_type)
            or await _try_gemini(GEMINI_API_KEY_2, GEMINI_MODEL_LITE, history, system_prompt, image_b64, media_type)
            or await _try_gemini(GEMINI_API_KEY,  GEMINI_MODEL_FLASH, history, system_prompt, image_b64, media_type)
            or await _try_groq(history, system_prompt)
        )

    else:  # "auto" — original waterfall
        if has_image:
            reply = (
                await _try_groq_vision(history, system_prompt, image_b64, media_type, user_message)
                or await _try_gemini(GEMINI_API_KEY,  GEMINI_MODEL_FLASH, history, system_prompt, image_b64, media_type)
                or await _try_gemini(GEMINI_API_KEY_2, GEMINI_MODEL_FLASH, history, system_prompt, image_b64, media_type)
                or await _try_gemini(GEMINI_API_KEY,  GEMINI_MODEL_LITE,  history, system_prompt, image_b64, media_type)
                or await _try_gemini(GEMINI_API_KEY_2, GEMINI_MODEL_LITE,  history, system_prompt, image_b64, media_type)
            )
        else:
            reply = (
                await _try_groq(history, system_prompt)
                or await _try_gemini(GEMINI_API_KEY,  GEMINI_MODEL_FLASH, history, system_prompt)
                or await _try_gemini(GEMINI_API_KEY_2, GEMINI_MODEL_FLASH, history, system_prompt)
                or await _try_gemini(GEMINI_API_KEY,  GEMINI_MODEL_LITE,  history, system_prompt)
                or await _try_gemini(GEMINI_API_KEY_2, GEMINI_MODEL_LITE,  history, system_prompt)
            )

    if not reply:
        history.pop()
        print(f"[AI Response] All providers failed for user {user_id} in channel {channel_id}")
        return RATE_LIMIT_MSG

    history.append({"role": "assistant", "content": reply})
    _trim(history)
    record_message(user_id, user_message, reply)

    new_count = increment_ai_usage(user_id)
    if new_count == WARN_AT:
        remaining = DAILY_AI_LIMIT - new_count
        reply += f"\n\n{WARN_LIMIT_MSG.format(count=new_count, limit=DAILY_AI_LIMIT, remaining=remaining)}"

    return reply


# ── Shared embed builders ─────────────────────────────────────────────────────

def _build_mylimit_embed(user_id: int) -> discord.Embed:
    count, day = get_ai_usage(user_id)
    remaining  = max(0, DAILY_AI_LIMIT - count)
    pct        = count / DAILY_AI_LIMIT

    if remaining == 0:
        colour, status = discord.Color.red(),    "❌ Limit reached"
    elif pct >= 0.75:
        colour, status = discord.Color.orange(), "⚠️ Running low"
    else:
        colour, status = discord.Color.green(),  "✅ Good to go"

    bar_filled = int(pct * 10)
    bar        = "█" * bar_filled + "░" * (10 - bar_filled)

    embed = discord.Embed(
        title="📊 Your Daily AI Limit",
        color=colour,
        description=(
            f"`{bar}` {count}/{DAILY_AI_LIMIT}\n"
            f"**Status:** {status}\n"
            f"**Remaining:** {remaining} message(s)\n"
            f"**Resets:** midnight UTC (daily)"
        ),
    )
    embed.set_footer(text=f"Limit resets every day at 00:00 UTC  •  {day}")
    return embed


def _build_model_embed(user_id: int) -> discord.Embed:
    current = get_user_model(user_id)
    info    = MODELS[current]
    embed   = discord.Embed(
        title="🤖 Your AI Model",
        color=discord.Color.blurple(),
        description=f"**Current:** {info['label']}\n{info['desc']}",
    )
    embed.add_field(
        name="Available Models",
        value="\n".join(
            f"{'▶ ' if k == current else '  '}`{k}` — {v['label']}"
            for k, v in MODELS.items()
        ),
        inline=False,
    )
    if not MODELS[current]["supports_vision"] and current != "auto":
        embed.set_footer(text="⚠️ This model doesn't support images — image messages will fall back to Gemini.")
    else:
        embed.set_footer(text="Use /setmodel or !setmodel to change  •  Resets on bot restart")
    return embed


# ── Cog ───────────────────────────────────────────────────────────────────────

class AI(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /chat ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="chat", description="Chat with Jarvis")
    @app_commands.describe(message="Your message to Jarvis", image="Optional image for Jarvis to analyse")
    async def chat(
        self,
        interaction: discord.Interaction,
        message: str,
        image: discord.Attachment | None = None,
    ):
        await interaction.response.defer(thinking=True)
        if is_new_user(interaction.user.id):
            mark_seen(interaction.user.id)
            await _log_new_user(interaction.user)

        image_b64, media_type = None, None
        if image:
            result = await _fetch_image(image)
            if result:
                image_b64, media_type = result
            else:
                await interaction.followup.send(
                    "⚠️ Unsupported file type or too large (max 20 MB, JPEG/PNG/GIF/WebP).",
                    ephemeral=True,
                )
                return

        reply = await generate_ai_response(
            interaction.user.id, message, interaction.channel_id,
            interaction.guild_id, image_b64, media_type, user=interaction.user,
        )
        await edit_or_send_long_message(interaction, reply, ephemeral=False)

    # ── on_message ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        content = message.content.strip()
        lower   = content.lower()

        mentioned     = self.bot.user in message.mentions
        replied_to_me = (
            message.reference is not None
            and message.reference.resolved is not None
            and isinstance(message.reference.resolved, discord.Message)
            and message.reference.resolved.author == self.bot.user
        )
        named = "jarvis" in lower

        if not (mentioned or replied_to_me or named):
            return

        if is_bot_banned(message.author.id):
            await message.reply("🚫 You've been banned from Jarvis. Contact the bot owner if you think this is a mistake.")
            return

        # ── Group start ────────────────────────────────────────────────────
        if re.search(r"\b(group|public)\s+conversation\b", lower):
            participants = [u for u in message.mentions if u.id != self.bot.user.id and not u.bot]
            all_ids      = list({message.author.id} | {u.id for u in participants})
            if len(all_ids) < 2:
                await message.reply(
                    "⚠️ Mention at least one other person to start a group conversation.\n"
                    "**Example:** `Jarvis group conversation @friend`"
                )
                return
            start_group(message.channel.id, all_ids)
            names = " & ".join(
                (message.guild.get_member(uid) or message.author).display_name
                for uid in all_ids
            )
            await message.reply(
                f"👥 **Group conversation started!**\n"
                f"**Participants:** {names}\n"
                "Your messages to Jarvis in this channel are now shared between you.\n"
                "Say `Jarvis end group` when you're done to return to private mode."
            )
            return

        # ── Add to group ───────────────────────────────────────────────────
        if re.search(r"\badd\b.+\bto\s+group\b", lower) or re.search(r"\badd\b.+\bgroup\b", lower):
            members = get_group_members(message.channel.id)
            if not members:
                await message.reply("⚠️ There's no active group conversation. Start one first with `Jarvis group conversation @user`.")
                return
            to_add = [u for u in message.mentions if u.id != self.bot.user.id and not u.bot and u.id not in members]
            if not to_add:
                await message.reply("⚠️ Those users are already in the group, or no valid users were mentioned.")
                return
            active_groups[message.channel.id].update(u.id for u in to_add)
            names = ", ".join(u.display_name for u in to_add)
            total = len(active_groups[message.channel.id])
            await message.reply(f"➕ **{names}** joined the group conversation! ({total} participants total)")
            return

        # ── Remove from group ──────────────────────────────────────────────
        if re.search(r"\bremove\b.+\bfrom\s+group\b", lower) or re.search(r"\bkick\b.+\bgroup\b", lower):
            members = get_group_members(message.channel.id)
            if not members:
                await message.reply("⚠️ There's no active group conversation in this channel.")
                return
            to_remove = [u for u in message.mentions if u.id != self.bot.user.id and not u.bot and u.id in members]
            if not to_remove:
                await message.reply("⚠️ Those users aren't in the group, or no valid users were mentioned.")
                return
            for u in to_remove:
                active_groups[message.channel.id].discard(u.id)
            names = ", ".join(u.display_name for u in to_remove)
            total = len(active_groups[message.channel.id])
            if total < 2:
                end_group(message.channel.id)
                await message.reply(f"➖ **{names}** left the group. Not enough participants — group ended. Everyone is back to private mode.")
            else:
                await message.reply(f"➖ **{names}** removed from the group conversation. ({total} participants remaining)")
            return

        # ── End group ──────────────────────────────────────────────────────
        if re.search(r"\b(end|stop)\s+group\b", lower):
            if get_group_members(message.channel.id):
                end_group(message.channel.id)
                await message.reply("🔒 Group conversation ended. Everyone is back to private mode.")
            else:
                await message.reply("ℹ️ There's no active group conversation in this channel.")
            return

        # ── Normal message ─────────────────────────────────────────────────
        user_text = _MENTION_RE.sub("", content)
        user_text = _JARVIS_RE.sub("", user_text).strip(" ,:-")

        image_b64, media_type = None, None
        for attachment in message.attachments:
            result = await _fetch_image(attachment)
            if result:
                image_b64, media_type = result
                break

        if not user_text and not image_b64:
            await message.reply("Yes? What can I help you with?")
            return

        if is_new_user(message.author.id):
            mark_seen(message.author.id)
            await _log_new_user(message.author)

        guild_id = message.guild.id if message.guild else None
        try:
            async with message.channel.typing():
                reply = await generate_ai_response(
                    message.author.id, user_text, message.channel.id,
                    guild_id, image_b64, media_type, user=message.author,
                )
        except Exception as e:
            # Network error (DNS failure, connection timeout, etc.) — proceed without typing indicator
            print(f"[Warning] Failed to show typing indicator: {type(e).__name__}: {e}")
            reply = await generate_ai_response(
                message.author.id, user_text, message.channel.id,
                guild_id, image_b64, media_type, user=message.author,
            )
        await send_long_message(message, reply, ephemeral=False)

    # ── /mylimit & !mylimit ───────────────────────────────────────────────────

    @app_commands.command(name="mylimit", description="Check how many AI messages you have left today")
    async def slash_mylimit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=_build_mylimit_embed(interaction.user.id), ephemeral=True
        )

    @commands.command(name="mylimit")
    async def prefix_mylimit(self, ctx: commands.Context):
        """Check how many AI messages you have left today."""
        await ctx.reply(embed=_build_mylimit_embed(ctx.author.id))

    # ── /clearhistory & !clearhistory ─────────────────────────────────────────

    @app_commands.command(name="clearhistory", description="Clear your Jarvis conversation history")
    async def slash_clearhistory(self, interaction: discord.Interaction):
        clear_history(interaction.user.id, interaction.channel_id)
        in_group = bool(get_group_members(interaction.channel_id))
        await interaction.response.send_message(
            f"🧹 {'Group conversation' if in_group else 'Your'} history has been cleared!",
            ephemeral=True,
        )

    @commands.command(name="clearhistory")
    async def prefix_clearhistory(self, ctx: commands.Context):
        """Clear your conversation history with Jarvis."""
        clear_history(ctx.author.id, ctx.channel.id)
        in_group = bool(get_group_members(ctx.channel.id))
        await ctx.reply(f"🧹 {'Group conversation' if in_group else 'Your'} history has been cleared!")

    # ── /setmodel & !setmodel ─────────────────────────────────────────────────

    @app_commands.command(name="setmodel", description="Choose which AI model Jarvis uses for your messages")
    @app_commands.describe(model="The model to use for your conversations")
    @app_commands.choices(model=MODEL_CHOICES)
    async def slash_setmodel(self, interaction: discord.Interaction, model: app_commands.Choice[str]):
        set_user_model(interaction.user.id, model.value)
        info = MODELS[model.value]
        await interaction.response.send_message(
            embed=_build_model_embed(interaction.user.id), ephemeral=True
        )

    @commands.command(name="setmodel")
    async def prefix_setmodel(self, ctx: commands.Context, model: str = None):
        """Choose your AI model. Usage: !setmodel <auto|groq|gemini-flash|gemini-lite>"""
        if not model or model not in MODELS:
            keys = ", ".join(f"`{k}`" for k in MODELS)
            await ctx.reply(
                f"**Usage:** `!setmodel <model>`\n"
                f"**Available:** {keys}\n\n"
                + "\n".join(f"**`{k}`** — {v['label']}: {v['desc']}" for k, v in MODELS.items())
            )
            return
        set_user_model(ctx.author.id, model)
        await ctx.reply(embed=_build_model_embed(ctx.author.id))

    # ── /mymodel & !mymodel ───────────────────────────────────────────────────

    @app_commands.command(name="mymodel", description="Check which AI model you're currently using")
    async def slash_mymodel(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=_build_model_embed(interaction.user.id), ephemeral=True
        )

    @commands.command(name="mymodel")
    async def prefix_mymodel(self, ctx: commands.Context):
        """Check your current AI model preference."""
        await ctx.reply(embed=_build_model_embed(ctx.author.id))


async def setup(bot: commands.Bot):
    await bot.add_cog(AI(bot))