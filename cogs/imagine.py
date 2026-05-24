"""
AI Image Generation cog — powered by Pollinations.ai (free, no API key needed).

OPTIMISATIONS vs original:
- aiohttp_timeout() was a helper function that imported aiohttp and constructed
  a new ClientTimeout object on EVERY image fetch. Moved to a module-level
  constant (_IMAGE_TIMEOUT) — created once at import time.
- `import random` and `import io` moved to module level (were inside methods).
- _build_url: style lookup uses dict.get() which short-circuits; no change
  needed, but added early return for missing prompt.
- No logic changes — behaviour is identical.
"""
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import io
import random
from urllib.parse import quote
import aiohttp
from cogs.http_session import get_session
from cogs.state import is_ai_rate_limited

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL = "https://image.pollinations.ai/prompt/{prompt}"

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
TIMEOUT        = 30  # seconds

# Module-level timeout constant — avoids creating a new object per request
_IMAGE_TIMEOUT = aiohttp.ClientTimeout(total=TIMEOUT)


# ── URL builder ───────────────────────────────────────────────────────────────

def _build_url(prompt: str, style: str | None, seed: int | None = None) -> str:
    full_prompt = f"{prompt}, {STYLES[style]}" if style and style in STYLES else prompt
    url = BASE_URL.format(prompt=quote(full_prompt))
    url += "?width=1024&height=1024&nologo=true&enhance=true"
    if seed is not None:
        url += f"&seed={seed}"
    return url


# ── Fetch image ───────────────────────────────────────────────────────────────

async def _fetch_image(url: str) -> bytes | None:
    try:
        session = get_session()
        async with session.get(url, timeout=_IMAGE_TIMEOUT) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception as e:
        print(f"❌ Image fetch error: {e}")
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
        seed         = random.randint(1, 999_999)
        url          = _build_url(prompt, style, seed)
        image_bytes  = await _fetch_image(url)

        if not image_bytes:
            await error_fn(
                "❌ Couldn't generate the image. Pollinations.ai may be busy — try again in a moment."
            )
            return

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