"""
/profile — a bot-rendered rank card (balance, streak, game records, favorite
song, level, badges) plus commands to equip earned/bought titles, banners,
banner borders, and to toggle which sections show up on the card.

Deliberately server-agnostic: nothing here ever creates, checks, or touches
a Discord role or permission, so it looks and behaves identically no matter
which of the bot's servers someone is in.
"""
import io
import math
import random as _random

import discord
from discord.ext import commands
from discord import app_commands
from PIL import Image, ImageDraw

from cogs.http_session import get_session
from cogs.state import (
    get_credits, get_streak, get_game_stats, get_songs_played,
    get_favorite_song, get_stats, get_badges, get_titles, get_banners,
    equip_title, equip_banner,
    get_hidden_sections, set_section_hidden,
    get_banner_borders, equip_banner_border,
)
from cogs.achievements import ACHIEVEMENTS, get_avatar_frame, next_avatar_frame_hint
from cogs.economy import (
    JC_EMOJI, JC_NAME, BANNER_COLORS, BANNER_GRADIENTS, BANNER_BORDERS,
    get_title_label, get_banner_label, get_banner_colors, get_border_label,
)

# ── Leveling ──────────────────────────────────────────────────────────────────
# Deliberately simple and transparent: level N needs N*25 total messages to
# reach. No hidden curve — easy for users to understand from the progress bar.
MESSAGES_PER_LEVEL = 25


def _level_for_messages(messages: int) -> tuple[int, int, int]:
    """Return (level, messages_into_level, messages_needed_for_next_level)."""
    level = 1 + math.floor(messages / MESSAGES_PER_LEVEL)
    into_level = messages % MESSAGES_PER_LEVEL
    return level, into_level, MESSAGES_PER_LEVEL


def _progress_bar(current: int, total: int, length: int = 10) -> str:
    filled = round((current / total) * length) if total else 0
    return "█" * filled + "░" * (length - filled)


def _normalize_text(s: str) -> str:
    """Strip emoji/punctuation and collapse whitespace so item names can be
    matched regardless of whether the user included the leading emoji —
    '💎 VIP', 'VIP', and 'vip' all normalize to the same string. Used for
    title, banner, and banner-border name matching so none of them require
    typing the emoji back."""
    return " ".join("".join(ch for ch in s if ch.isalnum() or ch.isspace()).lower().split())


# ── Profile section visibility ────────────────────────────────────────────────
# Lets someone hide a /profile field that just doesn't apply to them (e.g.
# no Chess field shown if they never touch that game) instead of always
# seeing every section, several of them at zero.
PROFILE_SECTIONS: dict[str, str] = {
    "balance": "💰 Balance",
    "streak":  "🔥 Streak",
    "level":   "📈 Level",
    "chess":   "♟️ Chess",
    "mafia":   "🎭 Mafia",
    "hangman": "🪢 Hangman",
    "music":   "🎵 Music",
    "badges":  "🏅 Badges",
}

_SECTION_ALIASES: dict[str, str] = {
    "songs": "music", "song": "music", "favorite_song": "music", "favoritesong": "music",
    "credits": "balance", "jc": "balance", "coins": "balance",
    "messages": "level", "xp": "level", "levels": "level",
    "badge": "badges", "achievements": "badges", "achievement": "badges",
    "streaks": "streak",
}


def _resolve_section(name: str) -> str | None:
    key = name.strip().lower().replace(" ", "_").replace("-", "_")
    key = _SECTION_ALIASES.get(key, key)
    return key if key in PROFILE_SECTIONS else None


# ── Banner + border image rendering ──────────────────────────────────────────
# Solid colors render as a flat fill; gradients render as a left-to-right
# blend. Borders are a fully separate cosmetic layer drawn on top, so any
# owned border can be mixed with any owned banner color/gradient.
_BANNER_W, _BANNER_H = 900, 120
_BORDER_RGB = (255, 255, 255)


def _hex_to_rgb(color: int) -> tuple[int, int, int]:
    return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)


def _lerp(a: int, b: int, t: float) -> int:
    return int(a + (b - a) * t)


def _draw_dashed_border(draw: ImageDraw.ImageDraw, margin=6, dash=14, gap=8) -> None:
    w, h = _BANNER_W, _BANNER_H
    for y in (margin, h - margin):
        x = margin
        while x < w - margin:
            draw.line([(x, y), (min(x + dash, w - margin), y)], fill=_BORDER_RGB, width=3)
            x += dash + gap
    for x in (margin, w - margin):
        y = margin
        while y < h - margin:
            draw.line([(x, y), (x, min(y + dash, h - margin))], fill=_BORDER_RGB, width=3)
            y += dash + gap


def _draw_dotted_border(draw: ImageDraw.ImageDraw, margin=6, spacing=16, r=3) -> None:
    w, h = _BANNER_W, _BANNER_H
    for y in (margin, h - margin):
        x = margin
        while x < w - margin:
            draw.ellipse([x - r, y - r, x + r, y + r], fill=_BORDER_RGB)
            x += spacing
    for x in (margin, w - margin):
        y = margin
        while y < h - margin:
            draw.ellipse([x - r, y - r, x + r, y + r], fill=_BORDER_RGB)
            y += spacing


def _draw_double_border(draw: ImageDraw.ImageDraw, margin=6, gap=7) -> None:
    w, h = _BANNER_W, _BANNER_H
    draw.rectangle([margin, margin, w - margin, h - margin], outline=_BORDER_RGB, width=3)
    draw.rectangle([margin + gap, margin + gap, w - margin - gap, h - margin - gap], outline=_BORDER_RGB, width=2)


def _draw_sparkle_border(draw: ImageDraw.ImageDraw, margin=6, step=20) -> None:
    # Deterministic seed so a given equipped border renders identically
    # every time the profile card is regenerated.
    rng = _random.Random(1337)
    w, h = _BANNER_W, _BANNER_H
    points = []
    x = margin
    while x < w - margin:
        points += [(x, margin), (x, h - margin)]
        x += step
    y = margin
    while y < h - margin:
        points += [(margin, y), (w - margin, y)]
        y += step
    for px, py in points:
        size = rng.choice([2, 3, 4])
        draw.line([(px - size, py), (px + size, py)], fill=_BORDER_RGB, width=1)
        draw.line([(px, py - size), (px, py + size)], fill=_BORDER_RGB, width=1)


_BORDER_DRAWERS = {
    "dashed":  _draw_dashed_border,
    "dotted":  _draw_dotted_border,
    "double":  _draw_double_border,
    "sparkle": _draw_sparkle_border,
}


def _render_banner_image(colors: list[int], border_style: str | None) -> bytes:
    """Render a banner strip. `colors` is [fill] for solid, [start, end]
    for a gradient. `border_style` (from BANNER_BORDERS) is drawn on top,
    independent of the fill — mix-and-match instead of one flat swap."""
    rgb_colors = [_hex_to_rgb(c) for c in colors] if colors else [(44, 62, 80)]
    img = Image.new("RGB", (_BANNER_W, _BANNER_H), rgb_colors[0])
    draw = ImageDraw.Draw(img)

    if len(rgb_colors) >= 2:
        start, end = rgb_colors[0], rgb_colors[1]
        for x in range(_BANNER_W):
            t = x / (_BANNER_W - 1)
            col = tuple(_lerp(start[i], end[i], t) for i in range(3))
            draw.line([(x, 0), (x, _BANNER_H)], fill=col)

    drawer = _BORDER_DRAWERS.get(border_style)
    if drawer:
        drawer(draw)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


# ── Avatar frame rendering ────────────────────────────────────────────────────
# A ring drawn around the avatar thumbnail, tiered to the user's best
# earned achievement (see cogs/achievements.py::get_avatar_frame). Purely
# cosmetic and automatic — never purchased, never manually equipped, and
# entirely separate from titles.
_AVATAR_SIZE = 256
_RING_WIDTH = 14


async def _render_framed_avatar(user: discord.User | discord.Member, ring_color: tuple[int, int, int]) -> bytes | None:
    """Download the user's avatar and composite a colored ring around it.
    Returns None on any failure so the caller can fall back to the plain
    avatar URL instead of breaking the whole profile card."""
    try:
        session = get_session()
        avatar_url = str(user.display_avatar.replace(size=256, static_format="png").url)
        async with session.get(avatar_url) as resp:
            if resp.status != 200:
                return None
            raw = await resp.read()
    except Exception:
        return None

    try:
        avatar = Image.open(io.BytesIO(raw)).convert("RGBA").resize((_AVATAR_SIZE, _AVATAR_SIZE))
    except Exception:
        return None

    mask = Image.new("L", (_AVATAR_SIZE, _AVATAR_SIZE), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, _AVATAR_SIZE, _AVATAR_SIZE), fill=255)

    canvas_size = _AVATAR_SIZE + _RING_WIDTH * 2
    canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    canvas.paste(avatar, (_RING_WIDTH, _RING_WIDTH), mask)

    draw = ImageDraw.Draw(canvas)
    half = _RING_WIDTH / 2
    draw.ellipse(
        [half, half, canvas_size - half, canvas_size - half],
        outline=ring_color, width=_RING_WIDTH,
    )

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


async def _build_profile(user: discord.User | discord.Member) -> tuple[discord.Embed, list[discord.File]]:
    """Build the full /profile embed + any attachments (banner image,
    framed avatar) it needs. Returns (embed, files) — pass files straight
    through to ctx.reply(files=...) / interaction.response.send_message(files=...)."""
    uid = user.id
    files: list[discord.File] = []
    hidden = set(get_hidden_sections(uid))

    balance = get_credits(uid)
    streak = get_streak(uid)
    stats = get_game_stats(uid)
    songs = get_songs_played(uid)
    favorite = get_favorite_song(uid)
    message_stats = get_stats(uid) or {}
    messages = message_stats.get("messages", 0)
    level, into_level, per_level = _level_for_messages(messages)

    equipped_title_id = get_titles(uid).get("equipped")
    equipped_title = get_title_label(equipped_title_id) if equipped_title_id else None

    equipped_banner_id = get_banners(uid).get("equipped")
    equipped_border_id = get_banner_borders(uid).get("equipped")
    banner_colors = get_banner_colors(equipped_banner_id) if equipped_banner_id else []
    border_style = BANNER_BORDERS[equipped_border_id][1] if equipped_border_id in BANNER_BORDERS else None

    color = discord.Color(banner_colors[0]) if banner_colors else discord.Color.blurple()

    display_name = f"{user.display_name}"
    if equipped_title:
        display_name = f"{display_name} — {equipped_title}"

    embed = discord.Embed(title=f"🪪 {display_name}", color=color)

    # Banner image: only rendered when there's an equipped color/gradient
    # and/or an equipped border — otherwise the card stays exactly as
    # before (no image attachment at all).
    if banner_colors or border_style:
        banner_bytes = _render_banner_image(banner_colors or [0x2C3E50], border_style)
        files.append(discord.File(io.BytesIO(banner_bytes), filename="banner.png"))
        embed.set_image(url="attachment://banner.png")

    # Avatar frame: automatic ring tied to the user's best earned
    # achievement tier, entirely separate from the title system above.
    frame = get_avatar_frame(uid)
    framed_bytes = None
    if frame:
        _tier_name, ring_color, _badge_id = frame
        framed_bytes = await _render_framed_avatar(user, ring_color)
    if framed_bytes:
        files.append(discord.File(io.BytesIO(framed_bytes), filename="avatar_frame.png"))
        embed.set_thumbnail(url="attachment://avatar_frame.png")
    else:
        embed.set_thumbnail(url=user.display_avatar.url)

    if "balance" not in hidden:
        embed.add_field(name=f"{JC_EMOJI} Balance", value=f"**{balance:,}** {JC_NAME}s", inline=True)
    if "streak" not in hidden:
        embed.add_field(name="🔥 Streak", value=f"**{streak}** day{'s' if streak != 1 else ''}", inline=True)
    if "level" not in hidden:
        embed.add_field(
            name=f"📈 Level {level}",
            value=f"`{_progress_bar(into_level, per_level)}` {into_level}/{per_level}",
            inline=True,
        )

    if "chess" not in hidden:
        embed.add_field(
            name="♟️ Chess",
            value=f"{stats['chess_wins']}W – {stats['chess_losses']}L",
            inline=True,
        )
    if "mafia" not in hidden:
        embed.add_field(
            name="🎭 Mafia",
            value=f"{stats['mafia_wins']}W – {stats['mafia_losses']}L",
            inline=True,
        )
    if "hangman" not in hidden:
        embed.add_field(
            name="🪢 Hangman",
            value=f"{stats['hangman_wins']} solved",
            inline=True,
        )

    if "music" not in hidden:
        embed.add_field(name="🎵 Songs Played", value=str(songs), inline=True)
        embed.add_field(name="🎧 Favorite Song", value=favorite or "—", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)  # spacer for a clean 3-column grid

    if "badges" not in hidden:
        badge_ids = get_badges(uid)
        if badge_ids:
            badge_line = "  ".join(
                ACHIEVEMENTS[b]["emoji"] for b in badge_ids if b in ACHIEVEMENTS
            )
            embed.add_field(name=f"🏅 Badges ({len(badge_ids)})", value=badge_line, inline=False)
        else:
            embed.add_field(name="🏅 Badges", value="None yet — play some games or chat to unlock some!", inline=False)

    footer = "Use /titles, /banners, and /borders to see what you own and can equip."
    if hidden:
        footer += f" • {len(hidden)} section(s) hidden — /profile show <section> to bring one back."
    embed.set_footer(text=footer)
    return embed, files


def _titles_embed(user: discord.User | discord.Member) -> discord.Embed:
    data = get_titles(user.id)
    owned = data["owned"]
    equipped = data["equipped"]
    embed = discord.Embed(title=f"🎖️ {user.display_name}'s Titles", color=discord.Color.blurple())
    if not owned:
        embed.description = "You don't own any titles yet — earn them through achievements or buy one from `/shop`."
        return embed
    lines = []
    for t in owned:
        marker = "✅ (equipped)" if t == equipped else ""
        lines.append(f"{get_title_label(t)} {marker}")
    embed.description = "\n".join(lines)
    embed.set_footer(text="Equip one with /title <name>, or /title none to unequip.")
    return embed


def _banners_embed(user: discord.User | discord.Member) -> discord.Embed:
    data = get_banners(user.id)
    owned = data["owned"]
    equipped = data["equipped"]
    embed = discord.Embed(title=f"🎨 {user.display_name}'s Banners", color=discord.Color.blurple())
    if not owned:
        embed.description = "You don't own any banners yet — buy one from `/shop`."
        return embed
    lines = []
    for b in owned:
        label = get_banner_label(b)
        style = "gradient" if b in BANNER_GRADIENTS else "solid"
        marker = "✅ (equipped)" if b == equipped else ""
        lines.append(f"{label} · {style} {marker}")
    embed.description = "\n".join(lines)
    embed.set_footer(text="Equip one with /banner <name>, or /banner none to unequip. Mix with /border <name>.")
    return embed


def _borders_embed(user: discord.User | discord.Member) -> discord.Embed:
    data = get_banner_borders(user.id)
    owned = data["owned"]
    equipped = data["equipped"]
    embed = discord.Embed(title=f"🖼️ {user.display_name}'s Banner Borders", color=discord.Color.blurple())
    if not owned:
        embed.description = "You don't own any banner borders yet — buy one from `/shop`."
        return embed
    lines = []
    for b in owned:
        label = get_border_label(b)
        marker = "✅ (equipped)" if b == equipped else ""
        lines.append(f"{label} {marker}")
    embed.description = "\n".join(lines)
    embed.set_footer(text="Equip one with /border <name>, or /border none to unequip. Works with any banner color.")
    return embed


def _frame_embed(user: discord.User | discord.Member) -> discord.Embed:
    frame = get_avatar_frame(user.id)
    embed = discord.Embed(title=f"🖼️ {user.display_name}'s Avatar Frame", color=discord.Color.blurple())
    if frame:
        tier_name, _color, badge_id = frame
        ach = ACHIEVEMENTS.get(badge_id, {})
        embed.description = (
            f"Current frame: **{tier_name}**, earned from **{ach.get('name', badge_id)}** {ach.get('emoji', '')}\n"
            "This ring shows automatically on your /profile thumbnail — nothing to equip."
        )
    else:
        embed.description = "You haven't unlocked an avatar frame yet — win games or build a streak to earn one."
    hint = next_avatar_frame_hint(user.id)
    if hint:
        embed.add_field(name="Next tier", value=hint, inline=False)
    return embed


class Profile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── !profile / /profile ─────────────────────────────────────────────────

    @commands.command(name="profile", aliases=["rank", "rankcard"])
    async def prefix_profile(self, ctx: commands.Context, *, arg: str = None):
        """!profile — view your (or someone else's) rank card.
        !profile hide <section> / !profile show <section> — toggle a section.
        !profile sections — list section visibility."""
        if arg:
            parts = arg.strip().split(maxsplit=1)
            sub = parts[0].lower()
            if sub in ("hide", "show") and len(parts) > 1:
                msg = self._handle_visibility(ctx.author, sub, parts[1])
                await ctx.reply(msg)
                return
            if sub in ("sections", "hidden"):
                await ctx.reply(self._sections_status(ctx.author))
                return
            try:
                target = await commands.UserConverter().convert(ctx, arg)
            except commands.UserNotFound:
                await ctx.reply(
                    f"❌ Couldn't find a user or section called `{arg}`. "
                    f"Use `!profile hide/show <section>` to toggle a section, or `!profile @user` to view someone else's."
                )
                return
        else:
            target = ctx.author
        embed, files = await _build_profile(target)
        await ctx.reply(embed=embed, files=files)

    @app_commands.command(name="profile", description="View your (or someone else's) Jarvis profile card")
    @app_commands.describe(user="User to look up (optional — leave empty for yourself)")
    async def slash_profile(self, interaction: discord.Interaction, user: discord.User = None):
        target = user or interaction.user
        embed, files = await _build_profile(target)
        await interaction.response.send_message(embed=embed, files=files)

    # ── !profile hide/show <section> / /profile-hide, /profile-show ────────

    def _sections_status(self, user: discord.User) -> str:
        hidden = set(get_hidden_sections(user.id))
        lines = []
        for key, label in PROFILE_SECTIONS.items():
            state_label = "hidden" if key in hidden else "shown"
            lines.append(f"`{key}` — {label} — **{state_label}**")
        return "**Section visibility:**\n" + "\n".join(lines) + "\n\nUse `/profile hide <section>` or `/profile show <section>` to toggle."

    def _handle_visibility(self, user: discord.User, action: str, section_name: str) -> str:
        section = _resolve_section(section_name)
        if section is None:
            valid = ", ".join(f"`{k}`" for k in PROFILE_SECTIONS)
            return f"❌ Unknown section `{section_name}`. Valid sections: {valid}"
        hide = action == "hide"
        set_section_hidden(user.id, section, hide)
        label = PROFILE_SECTIONS[section]
        if hide:
            return f"✅ {label} is now **hidden** from your /profile card. Use `/profile show {section}` to bring it back."
        return f"✅ {label} is now **shown** on your /profile card."

    @app_commands.command(name="profile-hide", description="Hide a section of your /profile card")
    @app_commands.describe(section="Which section to hide")
    @app_commands.choices(section=[
        app_commands.Choice(name=label, value=key) for key, label in PROFILE_SECTIONS.items()
    ])
    async def slash_profile_hide(self, interaction: discord.Interaction, section: app_commands.Choice[str]):
        msg = self._handle_visibility(interaction.user, "hide", section.value)
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="profile-show", description="Show a previously hidden section of your /profile card")
    @app_commands.describe(section="Which section to show")
    @app_commands.choices(section=[
        app_commands.Choice(name=label, value=key) for key, label in PROFILE_SECTIONS.items()
    ])
    async def slash_profile_show(self, interaction: discord.Interaction, section: app_commands.Choice[str]):
        msg = self._handle_visibility(interaction.user, "show", section.value)
        await interaction.response.send_message(msg, ephemeral=True)

    @commands.command(name="profilesections", aliases=["profilehidden"])
    async def prefix_profile_sections(self, ctx: commands.Context):
        """!profilesections — see which /profile sections are hidden/shown."""
        await ctx.reply(self._sections_status(ctx.author))

    # ── !titles / /titles ───────────────────────────────────────────────────

    @commands.command(name="titles", aliases=["mytitles"])
    async def prefix_titles(self, ctx: commands.Context):
        """!titles — see the titles you own and which one is equipped."""
        await ctx.reply(embed=_titles_embed(ctx.author))

    @app_commands.command(name="titles", description="See the titles you own and which one is equipped")
    async def slash_titles(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=_titles_embed(interaction.user), ephemeral=True)

    # ── !title <name> / /title <name> ───────────────────────────────────────

    @commands.command(name="title")
    async def prefix_title(self, ctx: commands.Context, *, name: str = None):
        """!title <name> — equip an owned title. !title none to unequip."""
        msg = self._handle_equip_title(ctx.author, name)
        await ctx.reply(msg)

    @app_commands.command(name="title", description="Equip one of your owned titles (or 'none' to unequip)")
    @app_commands.describe(name="The exact title to equip, or 'none' to unequip")
    async def slash_title(self, interaction: discord.Interaction, name: str = None):
        msg = self._handle_equip_title(interaction.user, name)
        await interaction.response.send_message(msg, ephemeral=True)

    def _handle_equip_title(self, user: discord.User, name: str | None) -> str:
        if not name:
            return "**Usage:** `/title <name>` — see `/titles` for what you own. Use `/title none` to unequip."
        name = name.strip()
        if name.lower() == "none":
            equip_title(user.id, None)
            return "✅ Title unequipped."
        owned = get_titles(user.id)["owned"]
        target = _normalize_text(name)
        match = None
        for owned_id in owned:
            label = get_title_label(owned_id)
            if name == owned_id or target == _normalize_text(label):
                match = owned_id
                break
        if match is None:
            return f"❌ You don't own a title called **{name}**. Check `/titles` for your list."
        equip_title(user.id, match)
        return f"✅ Equipped title: {get_title_label(match)}"

    # ── !banners / /banners ─────────────────────────────────────────────────

    @commands.command(name="banners", aliases=["mybanners"])
    async def prefix_banners(self, ctx: commands.Context):
        """!banners — see the profile banners you own."""
        await ctx.reply(embed=_banners_embed(ctx.author))

    @app_commands.command(name="banners", description="See the profile banners you own")
    async def slash_banners(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=_banners_embed(interaction.user), ephemeral=True)

    # ── !banner <name> / /banner <name> ─────────────────────────────────────

    @commands.command(name="banner")
    async def prefix_banner(self, ctx: commands.Context, *, name: str = None):
        """!banner <name> — equip an owned profile banner. !banner none to unequip."""
        msg = self._handle_equip_banner(ctx.author, name)
        await ctx.reply(msg)

    @app_commands.command(name="banner", description="Equip one of your owned profile banners (or 'none' to unequip)")
    @app_commands.describe(name="The exact banner name to equip, or 'none' to unequip")
    async def slash_banner(self, interaction: discord.Interaction, name: str = None):
        msg = self._handle_equip_banner(interaction.user, name)
        await interaction.response.send_message(msg, ephemeral=True)

    def _handle_equip_banner(self, user: discord.User, name: str | None) -> str:
        if not name:
            return "**Usage:** `/banner <name>` — see `/banners` for what you own. Use `/banner none` to unequip."
        if name.lower() == "none":
            equip_banner(user.id, None)
            return "✅ Banner unequipped."
        owned = get_banners(user.id)["owned"]
        target = _normalize_text(name)
        match = None
        for b in owned:
            label = get_banner_label(b)
            if name == b or target == _normalize_text(label):
                match = b
                break
        if match is None:
            return f"❌ You don't own a banner called **{name}**. Check `/banners` for your list."
        equip_banner(user.id, match)
        return f"✅ Equipped banner: {get_banner_label(match)}"

    # ── !borders / /borders ─────────────────────────────────────────────────

    @commands.command(name="borders", aliases=["myborders"])
    async def prefix_borders(self, ctx: commands.Context):
        """!borders — see the banner borders you own."""
        await ctx.reply(embed=_borders_embed(ctx.author))

    @app_commands.command(name="borders", description="See the banner borders you own")
    async def slash_borders(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=_borders_embed(interaction.user), ephemeral=True)

    # ── !border <name> / /border <name> ─────────────────────────────────────

    @commands.command(name="border")
    async def prefix_border(self, ctx: commands.Context, *, name: str = None):
        """!border <name> — equip an owned banner border. !border none to unequip."""
        msg = self._handle_equip_border(ctx.author, name)
        await ctx.reply(msg)

    @app_commands.command(name="border", description="Equip one of your owned banner borders (or 'none' to unequip)")
    @app_commands.describe(name="The exact border name to equip, or 'none' to unequip")
    async def slash_border(self, interaction: discord.Interaction, name: str = None):
        msg = self._handle_equip_border(interaction.user, name)
        await interaction.response.send_message(msg, ephemeral=True)

    def _handle_equip_border(self, user: discord.User, name: str | None) -> str:
        if not name:
            return "**Usage:** `/border <name>` — see `/borders` for what you own. Use `/border none` to unequip."
        if name.lower() == "none":
            equip_banner_border(user.id, None)
            return "✅ Banner border unequipped."
        owned = get_banner_borders(user.id)["owned"]
        target = _normalize_text(name)
        match = None
        for b in owned:
            label = get_border_label(b)
            if name == b or target == _normalize_text(label):
                match = b
                break
        if match is None:
            return f"❌ You don't own a banner border called **{name}**. Check `/borders` for your list."
        equip_banner_border(user.id, match)
        return f"✅ Equipped banner border: {get_border_label(match)} — mixes with whatever banner color you have equipped."

    # ── !frame / /frame ──────────────────────────────────────────────────────

    @commands.command(name="frame", aliases=["avatarframe"])
    async def prefix_frame(self, ctx: commands.Context):
        """!frame — see your current avatar-frame tier (automatic, tied to achievements)."""
        await ctx.reply(embed=_frame_embed(ctx.author))

    @app_commands.command(name="frame", description="See your current avatar-frame tier (automatic, tied to achievements)")
    async def slash_frame(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=_frame_embed(interaction.user), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Profile(bot))