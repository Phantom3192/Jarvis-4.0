"""
AI Image Generation cog — powered by Pollinations.ai (free, no API key needed).
Supports /imagine and !imagine with optional style flags.
"""
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
from urllib.parse import quote
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
TIMEOUT        = 30   # seconds to wait for image


# ── URL builder ───────────────────────────────────────────────────────────────

def _build_url(prompt: str, style: str | None, seed: int | None = None) -> str:
    full_prompt = prompt
    if style and style in STYLES:
        full_prompt = f"{prompt}, {STYLES[style]}"

    encoded = quote(full_prompt)
    url = BASE_URL.format(prompt=encoded)
    url += "?width=1024&height=1024&nologo=true&enhance=true"
    if seed is not None:
        url += f"&seed={seed}"
    return url


# ── Fetch image ───────────────────────────────────────────────────────────────

async def _fetch_image(url: str) -> bytes | None:
    try:
        session = get_session()
        async with session.get(url, timeout=aiohttp_timeout()) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception as e:
        print(f"❌ Image fetch error: {e}")
    return None


def aiohttp_timeout():
    import aiohttp
    return aiohttp.ClientTimeout(total=TIMEOUT)


# ── Embed builder ─────────────────────────────────────────────────────────────

def _build_embed(
    prompt: str,
    style: str | None,
    user: discord.User | discord.Member,
    seed: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="🎨 Image Generated",
        color=discord.Color.purple(),
    )
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

    # ── Core logic ────────────────────────────────────────────────────────────

    async def _generate(
        self,
        prompt: str,
        style: str | None,
        user: discord.User | discord.Member,
        send_fn,           # callable that sends the result
        error_fn,          # callable that sends errors
    ):
        import random
        seed = random.randint(1, 999999)
        url  = _build_url(prompt, style, seed)

        image_bytes = await _fetch_image(url)

        if not image_bytes:
            await error_fn(
                "❌ Couldn't generate the image. Pollinations.ai may be busy — try again in a moment."
            )
            return

        file  = discord.File(
            fp=__import__("io").BytesIO(image_bytes),
            filename="jarvis_image.png",
        )
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

        async def send_fn(**kwargs):
            await interaction.followup.send(**kwargs)

        async def error_fn(msg):
            await interaction.followup.send(msg)

        await self._generate(prompt, style_val, interaction.user, send_fn, error_fn)

    # ── Prefix command ────────────────────────────────────────────────────────
    # Usage:
    #   !imagine a dragon flying over a city
    #   !imagine a dragon --style anime
    #   !imagine a futuristic city --style cinematic

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

        # Parse --style flag
        style = None
        if "--style" in args:
            try:
                idx   = args.index("--style")
                rest  = args[idx + len("--style"):].strip()
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
            async def send_fn(**kwargs):
                await ctx.reply(**kwargs)

            async def error_fn(msg):
                await ctx.reply(msg)

            await self._generate(prompt, style, ctx.author, send_fn, error_fn)


async def setup(bot: commands.Bot):
    await bot.add_cog(Imagine(bot))