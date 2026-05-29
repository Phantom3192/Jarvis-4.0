"""
AI Image Generation cog — powered by Pollinations.ai.

Retries up to 3 times before giving up.
Rate limit: 1 image per user per 10 minutes (in-memory, resets on restart).
"""
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import io
import random
import time
import os
from urllib.parse import quote
import aiohttp
from cogs.http_session import get_session

# ── Constants ─────────────────────────────────────────────────────────────────

POLL_BASE_URL  = "https://image.pollinations.ai/prompt/{prompt}"
POLL_TIMEOUT   = aiohttp.ClientTimeout(total=40)
POLL_RETRIES   = 3
POLL_RETRY_DELAY = 2  # seconds between retries

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

MAX_PROMPT_LEN = 500
IMAGE_COOLDOWN = 10 * 60  # 10 minutes in seconds

# ── Per-user rate limit ───────────────────────────────────────────────────────

_last_generated: dict[int, float] = {}

def _check_cooldown(user_id: int) -> float:
    """Returns 0 if user may generate now, or seconds remaining if on cooldown."""
    elapsed = time.time() - _last_generated.get(user_id, 0)
    return max(0.0, IMAGE_COOLDOWN - elapsed)

def _mark_generated(user_id: int) -> None:
    _last_generated[user_id] = time.time()

def _fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s" if m else f"{s}s"

def _cooldown_embed(remaining: float, user: discord.User | discord.Member) -> discord.Embed:
    embed = discord.Embed(
        title="⏳ Cooldown Active",
        description=(
            f"You can only generate **1 image every 10 minutes**.\n\n"
            f"**Time remaining:** `{_fmt_time(remaining)}`"
        ),
        color=discord.Color.orange(),
    )
    embed.set_footer(
        text=f"Requested by {user.display_name}",
        icon_url=user.display_avatar.url,
    )
    return embed


# ── Pollinations fetch with retries ───────────────────────────────────────────

def _build_url(prompt: str, style: str | None, seed: int) -> str:
    full_prompt = f"{prompt}, {STYLES[style]}" if style and style in STYLES else prompt
    url = POLL_BASE_URL.format(prompt=quote(full_prompt))
    return url + f"?width=1024&height=1024&nologo=true&enhance=true&seed={seed}"

async def _fetch_image(prompt: str, style: str | None, seed: int) -> bytes | None:
    session = get_session()
    for attempt in range(1, POLL_RETRIES + 1):
        attempt_seed = seed if attempt == 1 else random.randint(1, 999_999)
        try:
            url = _build_url(prompt, style, attempt_seed)
            async with session.get(url, timeout=POLL_TIMEOUT) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if len(data) > 1000:
                        return data
                # fall through to retry
        except Exception:
            pass  # network blip — retry silently

        if attempt < POLL_RETRIES:
            await asyncio.sleep(POLL_RETRY_DELAY)

    return None


# ── Embed builder ─────────────────────────────────────────────────────────────

def _build_embed(
    prompt: str,
    style: str | None,
    user: discord.User | discord.Member,
    seed: int,
) -> discord.Embed:
    embed = discord.Embed(title="🎨 Image Generated", color=discord.Color.purple())
    embed.add_field(name="Prompt", value=f"`{prompt[:200]}`", inline=False)
    if style:
        embed.add_field(name="Style", value=style.capitalize(), inline=True)
    embed.add_field(name="Seed", value=f"`{seed}`", inline=True)
    embed.set_footer(
        text=f"Requested by {user.display_name}  •  Powered by Pollinations.ai",
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
        seed        = random.randint(1, 999_999)
        image_bytes = await _fetch_image(prompt, style, seed)

        if not image_bytes:
            await error_fn(
                "❌ Pollinations.ai didn't respond after 3 attempts. Please try again in a moment."
            )
            return

        _mark_generated(user.id)
        file  = discord.File(fp=io.BytesIO(image_bytes), filename="jarvis_image.png")
        embed = _build_embed(prompt, style, user, seed)
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

        remaining = _check_cooldown(interaction.user.id)
        if remaining > 0:
            await interaction.response.send_message(
                embed=_cooldown_embed(remaining, interaction.user),
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        await self._generate(
            prompt,
            style.value if style else None,
            interaction.user,
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

        remaining = _check_cooldown(ctx.author.id)
        if remaining > 0:
            await ctx.reply(embed=_cooldown_embed(remaining, ctx.author))
            return

        async with ctx.typing():
            await self._generate(
                prompt, style, ctx.author,
                send_fn=lambda **kw: ctx.reply(**kw),
                error_fn=lambda msg: ctx.reply(msg),
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Imagine(bot))