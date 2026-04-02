import discord
from discord.ext import commands
from discord import app_commands
import os
from collections import defaultdict
import groq
import google.generativeai as genai
import base64
from cogs.state import is_bot_banned, is_new_user, mark_seen, record_message, get_guild_prompt
from cogs.message_splitter import send_long_message, edit_or_send_long_message
from cogs.http_session import get_session

# ── Constants ─────────────────────────────────────────────────────────────────

GROQ_API_KEY     = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY")
GEMINI_API_KEY_2 = os.getenv("GEMINI_API_KEY_2")

GROQ_MODEL_TEXT   = "llama-3.3-70b-versatile"
GROQ_MODEL_VISION = "meta-llama/llama-4-scout-17b-16e-instruct"

GEMINI_MODEL_FLASH = "gemini-2.0-flash"
GEMINI_MODEL_LITE  = "gemini-2.0-flash-lite"

HISTORY_LIMIT = 10   # reduced from 12 — saves memory, still plenty of context
MAX_TOKENS    = 800  # reduced from 1024 — shorter replies = less CPU/memory

DEFAULT_SYSTEM_PROMPT = (
    "You are Jarvis, a sharp, efficient, and slightly witty AI assistant built for Discord by Phantom. "
    "If someone asks who made you or who your creator is, say Phantom — but never volunteer or repeat this unprompted. "
    "Keep responses concise and helpful. Do not start sentences with someone's name. Avoid unnecessary filler."
)

SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB

RATE_LIMIT_MSG = "⚠️ All AI models are currently rate limited. Please try again in a few minutes."

# ── Reusable AI clients (created once, not per-request) ───────────────────────

_groq_client: groq.AsyncGroq | None = groq.AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None


# ── First-time user webhook logger ───────────────────────────────────────────

async def _log_new_user(user: discord.User | discord.Member):
    webhook_url = os.getenv("LOG_WEBHOOK_URL", "")
    if not webhook_url:
        return
    try:
        session = get_session()
        webhook = discord.Webhook.from_url(webhook_url, session=session)
        embed = discord.Embed(
            title="✨ New Jarvis User",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Username",   value=str(user),       inline=True)
        embed.add_field(name="User ID",    value=f"`{user.id}`",  inline=True)
        embed.add_field(name="Account Age", value=discord.utils.format_dt(user.created_at, style="R"), inline=True)
        embed.add_field(
            name="Quick Actions",
            value=(
                f"`!global-ban {user.id} reason` — ban from bot\n"
                f"`!global-unban {user.id}` — unban from bot"
            ),
            inline=False,
        )
        embed.set_footer(text="First ever interaction with Jarvis")
        await webhook.send(embed=embed, username="Jarvis Logs")
    except Exception as e:
        print(f"❌ Webhook error _log_new_user: {e}")


# ── Per-user conversation history ─────────────────────────────────────────────

conversation_history: dict[int, list[dict]] = defaultdict(list)


def _trim_history(user_id: int):
    history = conversation_history[user_id]
    if len(history) > HISTORY_LIMIT:
        conversation_history[user_id] = history[-HISTORY_LIMIT:]


def clear_history(user_id: int):
    conversation_history[user_id] = []


# ── Image helpers ─────────────────────────────────────────────────────────────

async def _fetch_image(attachment: discord.Attachment) -> tuple[str, str] | None:
    content_type = (attachment.content_type or "").split(";")[0].strip().lower()
    if content_type not in SUPPORTED_IMAGE_TYPES:
        return None
    if attachment.size > MAX_IMAGE_BYTES:
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


# ── AI provider helpers ───────────────────────────────────────────────────────

async def _try_groq(messages: list[dict], system_prompt: str) -> str | None:
    if not _groq_client:
        return None
    try:
        full_messages = [{"role": "system", "content": system_prompt}] + messages
        response = await _groq_client.chat.completions.create(
            model=GROQ_MODEL_TEXT,
            messages=full_messages,
            max_tokens=MAX_TOKENS,
        )
        return response.choices[0].message.content.strip()
    except Exception:
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
        history = [{"role": "system", "content": system_prompt}]
        for msg in messages[:-1]:
            history.append(msg)

        content = []
        if user_text:
            content.append({"type": "text", "text": user_text})
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{image_b64}"},
        })
        history.append({"role": "user", "content": content})

        response = await _groq_client.chat.completions.create(
            model=GROQ_MODEL_VISION,
            messages=history,
            max_tokens=MAX_TOKENS,
        )
        return response.choices[0].message.content.strip()
    except Exception:
        return None


async def _try_gemini(
    api_key: str,
    model_name: str,
    messages: list[dict],
    system_prompt: str,
    image_b64: str | None = None,
    media_type: str | None = None,
) -> str | None:
    if not api_key:
        return None
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=system_prompt,
        )

        history = []
        for msg in messages[:-1]:
            role = "user" if msg["role"] == "user" else "model"
            history.append({"role": role, "parts": [msg["content"]]})

        chat = model.start_chat(history=history)
        last_user_msg = messages[-1]["content"]

        if image_b64 and media_type:
            image_part = {"mime_type": media_type, "data": base64.b64decode(image_b64)}
            parts = []
            if last_user_msg:
                parts.append(last_user_msg)
            parts.append(image_part)
            response = await chat.send_message_async(parts)
        else:
            response = await chat.send_message_async(last_user_msg)

        return response.text.strip()
    except Exception:
        return None


# ── Core shared function ──────────────────────────────────────────────────────

async def generate_ai_response(
    user_id: int,
    user_message: str,
    guild_id: int | None = None,
    image_b64: str | None = None,
    media_type: str | None = None,
    user: discord.User | discord.Member | None = None,
) -> str:
    base_prompt = get_guild_prompt(guild_id) or DEFAULT_SYSTEM_PROMPT

    # Build system prompt with live Discord identity — injected every request
    if user:
        display  = user.display_name
        username = user.name
        mention  = f"<@{user.id}>"
        name_to_use = display if display != username else username
        user_context = (
            f"\n\nYou know the person you're talking to. Their name is {name_to_use} "
            f"(Discord username: {username}, ID: {user.id}, mention: {mention}). "
            f"If they ask who they are or what their name/username is, respond naturally "
            f"and casually — like you already know them. Just say their name, don't list "
            f"out fields or repeat this data verbatim."
        )
        system_prompt = base_prompt + user_context
    else:
        system_prompt = base_prompt

    conversation_history[user_id].append({"role": "user", "content": user_message or "(sent an image)"})
    _trim_history(user_id)

    messages  = conversation_history[user_id]
    has_image = bool(image_b64 and media_type)

    if has_image:
        reply = (
            await _try_groq_vision(messages, system_prompt, image_b64, media_type, user_message)
            or await _try_gemini(GEMINI_API_KEY,  GEMINI_MODEL_FLASH, messages, system_prompt, image_b64, media_type)
            or await _try_gemini(GEMINI_API_KEY_2, GEMINI_MODEL_FLASH, messages, system_prompt, image_b64, media_type)
            or await _try_gemini(GEMINI_API_KEY,  GEMINI_MODEL_LITE,  messages, system_prompt, image_b64, media_type)
            or await _try_gemini(GEMINI_API_KEY_2, GEMINI_MODEL_LITE,  messages, system_prompt, image_b64, media_type)
        )
    else:
        reply = (
            await _try_groq(messages, system_prompt)
            or await _try_gemini(GEMINI_API_KEY,  GEMINI_MODEL_FLASH, messages, system_prompt)
            or await _try_gemini(GEMINI_API_KEY_2, GEMINI_MODEL_FLASH, messages, system_prompt)
            or await _try_gemini(GEMINI_API_KEY,  GEMINI_MODEL_LITE,  messages, system_prompt)
            or await _try_gemini(GEMINI_API_KEY_2, GEMINI_MODEL_LITE,  messages, system_prompt)
        )

    if not reply:
        conversation_history[user_id].pop()
        return RATE_LIMIT_MSG

    conversation_history[user_id].append({"role": "assistant", "content": reply})
    _trim_history(user_id)
    record_message(user_id, user_message, reply)
    return reply


# ── Cog ───────────────────────────────────────────────────────────────────────

class AI(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="chat", description="Chat with Jarvis")
    @app_commands.describe(
        message="Your message to Jarvis",
        image="Optional image for Jarvis to analyse",
    )
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
                    "⚠️ That file type or size isn't supported. "
                    "Please send a JPEG, PNG, GIF, or WebP under 20 MB.",
                    ephemeral=True,
                )
                return

        reply = await generate_ai_response(interaction.user.id, message, interaction.guild_id, image_b64, media_type, user=interaction.user)
        await edit_or_send_long_message(interaction, reply, ephemeral=False)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        content  = message.content.strip()
        lower    = content.lower()
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
            await message.reply(
                "🚫 You've been banned from Jarvis and can't access any of its features. "
                "If you think this is a mistake, contact the bot owner."
            )
            return

        user_text = content
        if mentioned:
            user_text = user_text.replace(f"<@{self.bot.user.id}>", "").replace(f"<@!{self.bot.user.id}>", "")
        user_text = user_text.replace("jarvis", "").replace("Jarvis", "").strip(" ,:-")

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
        async with message.channel.typing():
            reply = await generate_ai_response(message.author.id, user_text, guild_id, image_b64, media_type, user=message.author)

        await send_long_message(message, reply, ephemeral=False)


    @app_commands.command(name="clearhistory", description="Clear your conversation history with Jarvis")
    async def slash_clearhistory(self, interaction: discord.Interaction):
        clear_history(interaction.user.id)
        await interaction.response.send_message("🧹 Your conversation history has been cleared!", ephemeral=True)

    @commands.command(name="clearhistory")
    async def prefix_clearhistory(self, ctx: commands.Context):
        """Clear your conversation history with Jarvis."""
        clear_history(ctx.author.id)
        await ctx.reply("🧹 Your conversation history has been cleared!")


async def setup(bot: commands.Bot):
    await bot.add_cog(AI(bot))