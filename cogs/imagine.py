"""
AI Image Generation cog.

Primary provider  : HuggingFace Inference API (HUGGINGFACE_API_KEY in .env)
Fallback provider : Pollinations.ai (free, no key needed)

Rate limit: 1 image per user per 10 minutes (in-memory, resets on restart).
"""
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import io
import random
import time
from urllib.parse import quote
import aiohttp
from cogs.http_session import get_session

# ── Constants ─────────────────────────────────────────────────────────────────

# HuggingFace
HF_API_URL   = "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0"
HF_TIMEOUT   = aiohttp.ClientTimeout(total=60)   # SDXL can be slow on cold start

# Pollinations (fallback)
POLL_BASE_URL = "https://image.pollinations.ai/prompt/{prompt}"
POLL_TIMEOUT  = aiohttp.ClientTimeout(total=30)

STYLES = {
    "anime":      "anime style, vibrant colors, detailed illustration",
    "realistic":  "photorealistic, highly detailed, 8k, professional photography",
    "pixel":      "pixel art, retro 16-bit style",
    "cartoon":    "cartoon style, colorful, fun, animated",
    "sketch":     "pencil sketch, black and white, hand drawn",
    "watercolor": "watercolor painting, soft colors, artistic",
    "cinematic":  "cinematic, dramatic lighting, movie still, epic",
    "fantasy":    "fantasy art, magical, ethereal, detailed digital art",
}

STYLE_CHOICES = [
    app_commands.Choice(name=name.capitalize(), value=name)
    for name in STYLES
]

MAX_PROMPT_LEN   = 500
IMAGE_COOLDOWN   = 10 * 60   # 10 minutes in seconds

# ── Per-user rate limit ───────────────────────────────────────────────────────
# Maps user_id → timestamp of their last successful generation.
_last_generated: dict[int, float] = {}

def _check_cooldown(user_id: int) -> float:
    """
    Returns 0 if the user may generate now, or the seconds remaining otherwise.
    """
    last = _last_generated.get(user_id, 0)
    elapsed = time.time() - last
    remaining = IMAGE_COOLDOWN - elapsed
    return max(0.0, remaining)

def _mark_generated(user_id: int) -> None:
    _last_generated[user_id] = time.time()


# ── HuggingFace provider ──────────────────────────────────────────────────────

import os

def _get_hf_key() -> str | None:
    return os.getenv("HUGGINGFACE_API_KEY", "").strip() or None

async def _fetch_hf(prompt: str, style: str | None) -> bytes | None:
    api_key = _get_hf_key()
    if not api_key:
        return None

    full_prompt = f"{prompt}, {STYLES[style]}" if style and style in STYLES else prompt

    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "inputs": full_prompt,
        "parameters": {
            "width": 1024,
            "height": 1024,
            "num_inference_steps": 30,
            "guidance_scale": 7.5,
        },
        "options": {"wait_for_model": True},
    }

    try:
        session = get_session()
        async with session.post(
            HF_API_URL, json=payload, headers=headers, timeout=HF_TIMEOUT
        ) as resp:
            if resp.status == 200:
                content_type = resp.headers.get("Content-Type", "")
                if "image" in content_type:
                    return await resp.read()
                # HF sometimes returns JSON error even on 200
                body = await resp.json()
                print(f"[HF] Unexpected JSON on 200: {body}")
                return None
            elif resp.status == 503:
                # Model is loading — wait and retry once
                body = await resp.json()
                wait = body.get("estimated_time", 20)
                print(f"[HF] Model loading, waiting {wait:.0f}s…")
                await asyncio.sleep(min(float(wait), 30))
                async with session.post(
                    HF_API_URL, json=payload, headers=headers, timeout=HF_TIMEOUT
                ) as resp2:
                    if resp2.status == 200:
                        return await resp2.read()
            print(f"[HF] Failed with status {resp.status}")
    except Exception as e:
        print(f"[HF] Error: {e}")
    return None


# ── Pollinations fallback ─────────────────────────────────────────────────────

def _build_poll_url(prompt: str, style: str | None, seed: int) -> str:
    full_prompt = f"{prompt}, {STYLES[style]}" if style and style in STYLES else prompt
    url = POLL_BASE_URL.format(prompt=quote(full_prompt))
    url += f"?width=1024&height=1024&nologo=true&enhance=true&seed={seed}"
    return url

async def _fetch_pollinations(prompt: str, style: str | None, seed: int) -> bytes | None:
    url = _build_poll_url(prompt, style, seed)
    try:
        session = get_session()
        async with session.get(url, timeout=POLL_TIMEOUT) as resp:
            if resp.status == 200:
                return await resp.read()
            print(f"[Pollinations] Failed with status {resp.status}")
    except Exception as e:
        print(f"[Pollinations] Error: {e}")
    return None


# ── Unified fetch with fallback ───────────────────────────────────────────────

async def _fetch_image(prompt: str, style: str | None, seed: int) -> tuple[bytes | None, str]:
    """
    Try HuggingFace first; fall back to Pollinations.
    Returns (image_bytes, provider_name).
    """
    if _get_hf_key():
        data = await _fetch_hf(prompt, style)
        if data:
            return data, "HuggingFace (SDXL)"
        print("[Imagine] HuggingFace failed — falling back to Pollinations.ai")

    data = await _fetch_pollinations(prompt, style, seed)
    if data:
        return data, "Pollinations.ai"

    return None, "none"


# ── Embed builder ─────────────────────────────────────────────────────────────

def _build_embed(
    prompt: str,
    style: str | None,
    user: discord.User | discord.Member,
    seed: int,
    provider: str,
) -> discord.Embed:
    embed = discord.Embed(title="🎨 Image Generated", color=discord.Color.purple())
    embed.add_field(name="Prompt", value=f"`{prompt[:200]}`", inline=False)
    if style:
        embed.add_field(name="Style", value=style.capitalize(), inline=True)
    embed.add_field(name="Seed", value=f"`{seed}`", inline=True)
    embed.set_footer(
        text=f"Requested by {user.display_name}  •  Powered by {provider}",
        icon_url=user.display_avatar.url,
    )
    return embed


# ── Cog ───────────────────────────────────────────────────────────────────────

class Imagine(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _generate(
        self,
        prompt: str,
        style: str | None,
        user: discord.User | discord.Member,
        send_fn,
        error_fn,
    ) -> None:
        # ── Rate limit check ──────────────────────────────────────────────────
        remaining = _check_cooldown(user.id)
        if remaining > 0:
            mins = int(remaining) // 60
            secs = int(remaining) % 60
            time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
            await error_fn(
                f"⏳ You can only generate **1 image every 10 minutes**.\n"
                f"Please wait **{time_str}** before generating another image."
            )
            return

        seed               = random.randint(1, 999_999)
        image_bytes, provider = await _fetch_image(prompt, style, seed)

        if not image_bytes:
            await error_fn(
                "❌ Both image providers failed. Please try again in a moment."
            )
            return

        _mark_generated(user.id)

        file  = discord.File(fp=io.BytesIO(image_bytes), filename="jarvis_image.png")
        embed = _build_embed(prompt, style, user, seed, provider)
        embed.set_image(url="attachment://jarvis_image.png")
        await send_fn(file=file, embed=embed)

    # ── Slash command ─────────────────────────────────────────────────────────

    @app_commands.command(name="imagine", description="Generate an AI image from a text prompt 🎨")
    @app_commands.describe(
        prompt="Describe the image you want to generate",
        style="Optional art style",
    )
    @app_commands.choices(style=STYLE_CHOICES)
    async def slash_imagine(
        self,
        interaction: discord.Interaction,
        prompt: str,
        style: app_commands.Choice[str] | None = None,
    ):
        if len(prompt) > MAX_PROMPT_LEN:
            await interaction.response.send_message(
                f"❌ Prompt too long. Maximum is {MAX_PROMPT_LEN} characters.", ephemeral=True
            )
            return

        # Check cooldown before deferring to avoid a stuck "thinking" message
        remaining = _check_cooldown(interaction.user.id)
        if remaining > 0:
            mins = int(remaining) // 60
            secs = int(remaining) % 60
            time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
            await interaction.response.send_message(
                f"⏳ You can only generate **1 image every 10 minutes**.\n"
                f"Please wait **{time_str}** before generating another image.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        style_val = style.value if style else None

        await self._generate(
            prompt, style_val, interaction.user,
            send_fn=lambda **kw: interaction.followup.send(**kw),
            error_fn=lambda msg: interaction.followup.send(msg),
        )

    # ── Prefix command ────────────────────────────────────────────────────────

    @commands.command(name="imagine")
    async def prefix_imagine(self, ctx: commands.Context, *, args: str = None):
        """Generate an AI image. Usage: !imagine <prompt> [--style <style>]"""
        if not args:
            styles_list = ", ".join(f"`{s}`" for s in STYLES)
            await ctx.reply(
                "**Usage:** `!imagine <prompt> [--style <style>]`\n"
                f"**Available styles:** {styles_list}\n"
                "**Example:** `!imagine a wolf howling at the moon --style anime`"
            )
            return

        style = None
        if "--style" in args:
            try:
                idx             = args.index("--style")
                rest            = args[idx + len("--style"):].strip()
                style_candidate = rest.split()[0].lower()
                if style_candidate in STYLES:
                    style = style_candidate
                    args  = args[:idx].strip()
                else:
                    await ctx.reply(
                        f"❌ Unknown style `{style_candidate}`.\n"
                        f"Available: {', '.join(f'`{s}`' for s in STYLES)}"
                    )
                    return
            except (ValueError, IndexError):
                pass

        prompt = args.strip()
        if not prompt:
            await ctx.reply("❌ Please provide a prompt.")
            return
        if len(prompt) > MAX_PROMPT_LEN:
            await ctx.reply(f"❌ Prompt too long. Maximum is {MAX_PROMPT_LEN} characters.")
            return

        async with ctx.typing():
            await self._generate(
                prompt, style, ctx.author,
                send_fn=lambda **kw: ctx.reply(**kw),
                error_fn=lambda msg: ctx.reply(msg),
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Imagine(bot))