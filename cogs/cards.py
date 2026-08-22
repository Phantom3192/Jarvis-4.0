"""
Cards — collectible card feature.

Acquisition: a free weighted-random pull every day (/dailycard), and daily
quests that reward a card on completion (/quests, never Legendary).
!givecard still exists as an owner-only override for one-off grants/
corrections. Duplicates stack as a quantity rather than separate items (see
state.py's user_cards store), and cards can be traded directly between two
users via a mutual accept/decline flow. Cards belong to themed sets (see
CARD_SETS) — owning at least one copy of every card in a set pays out a
one-time JC bonus via /cardsets.

Commands:
  /cardinfo    — view full details on one specific card (rendered as an image)
  /cards       — view your (or someone else's) collection
  /carddex     — browse the full set of collectible cards, marking which
                 ones you own, and their rarity
  /cardtrade   — offer one of your cards to another user in exchange for JC
                 (or a straight gift if asking_price is 0)
  /dailycard   — claim your free daily weighted-random card pull
  /quests      — view today's card quests and claim finished ones
  /cardsets    — view set-completion progress and claim finished sets
  !givecard    — owner only, grants a card to a user (prefix-only override,
                 mainly for corrections/events — normal acquisition is
                 /dailycard and /quests)

Follows the same patterns as cogs/economy.py: state.py owns persistence,
this cog owns commands/UI, and pending trade offers are tracked in an
in-memory dict keyed by (sender_id, recipient_id) the same way
_pending_sub_gifts tracks subscription gifts.
"""
import io
import math
import os
import random
import re
import time

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from cogs.state import (
    spend_credits, add_credits,
    get_user_cards, get_card_quantity, add_card, remove_card, transfer_card,
    has_claimed_daily_card, claim_daily_card,
    get_daily_quests, assign_daily_quests, mark_quest_claimed,
    get_claimed_sets, claim_set_reward,
    get_stats, get_lifetime_earned, get_lifetime_spent,
    get_image_search_count,
    today_utc,
)

JC_EMOJI = "🪙"
JC_NAME = "Jarvis Credit"

# ── Rarity tiers ─────────────────────────────────────────────────────────────
# weight = relative pull chance (higher = more common). Color used for embeds.
RARITIES = {
    "Common":    {"weight": 925, "color": discord.Color.light_gray()},
    "Rare":      {"weight": 70,  "color": discord.Color.blue()},
    "Legendary": {"weight": 5,   "color": discord.Color.gold()},
}

# ── Card roster ──────────────────────────────────────────────────────────
CARD_RARITY: dict[str, str] = {
    # Legendary
    "Optimus Prime": "Legendary",
    "Megatron": "Legendary",
    "Bumblebee": "Legendary",
    "Starscream": "Legendary",
    "Optimus Primal": "Legendary",
    "Unicron": "Legendary",
    "Primus": "Legendary",
    "Galvatron": "Legendary",
    "Ultra Magnus": "Legendary",
    "Hot Rod / Rodimus Prime": "Legendary",

    # Rare
    "Ironhide": "Rare",
    "Ratchet": "Rare",
    "Jazz": "Rare",
    "Grimlock": "Rare",
    "Soundwave": "Rare",
    "Shockwave": "Rare",
    "Arcee": "Rare",
    "Cyclonus": "Rare",
    "Scourge": "Rare",
    "Blitzwing": "Rare",
    "Astrotrain": "Rare",
    "Cheetor": "Rare",
    "Rattrap": "Rare",
    "Dinobot (Beast Wars)": "Rare",
    "Rhinox": "Rare",
    "Blackarachnia": "Rare",
    "Waspinator": "Rare",
    "Tarantulas": "Rare",
    "Lugnut": "Rare",
    "Blackout": "Rare",
    "Barricade": "Rare",
    "Sideways": "Rare",
    "Vector Prime": "Rare",
    "Elita-1": "Rare",
    "Windblade": "Rare",
    "Alpha Trion": "Rare",
    "Jetfire (Skyfire)": "Rare",
    "Wreck-Gar": "Rare",
    "Springer": "Rare",
    "Kup": "Rare",

    # Common
    "Wheeljack": "Common",
    "Sideswipe": "Common",
    "Sunstreaker": "Common",
    "Mirage": "Common",
    "Hound": "Common",
    "Prowl": "Common",
    "Bluestreak": "Common",
    "Cliffjumper": "Common",
    "Slag": "Common",
    "Sludge": "Common",
    "Snarl": "Common",
    "Swoop": "Common",
    "Blaster": "Common",
    "Wheelie": "Common",
    "Sandstorm": "Common",
    "Perceptor": "Common",
    "Smokescreen": "Common",
    "Thundercracker": "Common",
    "Skywarp": "Common",
    "Ravage": "Common",
    "Rumble": "Common",
    "Frenzy": "Common",
    "Laserbeak": "Common",
    "Skyfire": "Common",
    "Bonecrusher": "Common",
    "Scavenger": "Common",
    "Hook": "Common",
    "Long Haul": "Common",
    "Mixmaster": "Common",
    "Scrapper": "Common",
    "Motormaster": "Common",
    "Dead End": "Common",
    "Drag Strip": "Common",
    "Wildrider": "Common",
    "Breakdown": "Common",
    "Blast Off": "Common",
    "Brawl": "Common",
    "Onslaught": "Common",
    "Swindle": "Common",
    "Vortex": "Common",
    "Megatron (Beast Wars)": "Common",
}

_RARITY_ORDER = ["Common", "Rare", "Legendary"]
_POWER_RANGES = {
    "Common": (18, 24),
    "Rare": (44, 53),
    "Legendary": (94, 98),
}
_SET_CHUNK_SIZE = 4  # how many cards land in each auto-generated set


def _slugify(name: str, taken: set) -> str:
    """Turn a display name into a stable storage id: lowercase,
    non-alphanumeric runs collapsed to underscores. Falls back to
    numbered suffixes on collision (e.g. two names that slugify the same)."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_") or "card"
    base, i = slug, 2
    while slug in taken:
        slug = f"{base}_{i}"
        i += 1
    taken.add(slug)
    return slug


_VALID_RARITIES = {r.lower(): r for r in _RARITY_ORDER}


def _normalize_rarity(raw: str) -> str:
    """Case-insensitive lookup into the canonical rarity names. Raises a
    clear error at import time if someone typos a rarity or uses one that
    no longer exists (e.g. 'epic'), instead of silently misbehaving."""
    key = raw.strip().lower()
    if key not in _VALID_RARITIES:
        raise ValueError(f"Unknown rarity {raw!r} in CARD_RARITY — must be one of: {', '.join(_RARITY_ORDER)}")
    return _VALID_RARITIES[key]


def _build_cards(card_rarity: dict[str, str]) -> tuple[dict, dict]:
    """Build (CARD_DEFS, CARD_SETS) from {name: rarity}. Each card gets a
    deterministic pseudo-random power within its rarity's range (seeded by
    the card's id, so it's stable across restarts), and cards are grouped
    into sets of _SET_CHUNK_SIZE in the order they appear in the dict."""
    taken_slugs: set = set()
    defs: dict[str, dict] = {}
    ordered_ids: list[str] = []

    for name, raw_rarity in card_rarity.items():
        rarity = _normalize_rarity(raw_rarity)
        card_id = _slugify(name, taken_slugs)
        rng = random.Random(card_id)
        lo, hi = _POWER_RANGES[rarity]
        defs[card_id] = {
            "name": name,
            "rarity": rarity,
            "power": rng.randint(lo, hi),
            "flavor": "No description yet — edit this card's flavor text in CARD_DEFS.",
            "set": None,  # filled in below
        }
        ordered_ids.append(card_id)

    sets: dict[str, dict] = {}
    for i in range(0, len(ordered_ids), _SET_CHUNK_SIZE):
        chunk = ordered_ids[i:i + _SET_CHUNK_SIZE]
        if not chunk:
            continue
        set_num = i // _SET_CHUNK_SIZE + 1
        set_id = f"set_{set_num}"
        for cid in chunk:
            defs[cid]["set"] = set_id
        sets[set_id] = {
            "name": f"Set {set_num}",
            "card_ids": chunk,
            "reward_credits": 300 if len(chunk) <= 4 else 400,
        }
    return defs, sets


# id is stable and used as the storage key — see CARD_RARITY above for how
# to add/edit cards safely.
CARD_DEFS, CARD_SETS = _build_cards(CARD_RARITY)


# ── Weighted random pulls ────────────────────────────────────────────────

def _pick_rarity(exclude: tuple[str, ...] = ()) -> str:
    """Pick a rarity tier using the RARITIES weights, optionally excluding
    some tiers entirely (their weight doesn't get redistributed to a
    default — it's just removed from the pool, so excluding Legendary
    makes the remaining tiers proportionally more likely relative to each
    other, matching the odds you'd get if Legendary weren't in the game)."""
    tiers = [t for t in RARITIES if t not in exclude]
    weights = [RARITIES[t]["weight"] for t in tiers]
    return random.choices(tiers, weights=weights, k=1)[0]


def _pick_card(rarity: str | None = None, exclude: tuple[str, ...] = ()) -> str:
    """Pick a random card_id. If rarity is given, restrict to that tier;
    otherwise roll a rarity first via _pick_rarity(), optionally excluding
    some tiers from that roll."""
    rarity = rarity or _pick_rarity(exclude=exclude)
    pool = [cid for cid, c in CARD_DEFS.items() if c["rarity"] == rarity]
    return random.choice(pool)


def _grant_random_card(user_id: int, exclude: tuple[str, ...] = ()) -> tuple[str, int]:
    """Roll and grant one random card (weighted by rarity) to user_id.
    Returns (card_id, new_quantity_owned)."""
    card_id = _pick_card(exclude=exclude)
    new_qty = add_card(user_id, card_id)
    return card_id, new_qty


# ── Card image rendering ────────────────────────────────────────────────
# Renders each card as a portrait PNG (gradient background tinted by
# rarity, rounded border, art frame with a procedural creature face, rarity/power
# badges, and wrapped flavor text) instead of a plain text embed.
# Falls back gracefully if fonts are missing on the host — mirrors the
# font-fallback pattern used for the chess board in cogs/game.py.

_CARD_W, _CARD_H = 500, 700

_FONT_BOLD_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
_FONT_REGULAR_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]
_FONT_ITALIC_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
]
_RARITY_PALETTE = {
    "Common":    {"border": (150, 150, 150), "grad_top": (60, 60, 65),  "grad_bottom": (28, 28, 31), "accent": (210, 210, 215)},
    "Rare":      {"border": (70, 130, 220),  "grad_top": (28, 48, 88),  "grad_bottom": (13, 22, 42), "accent": (130, 180, 245)},
    "Legendary": {"border": (230, 180, 40),  "grad_top": (90, 65, 10),  "grad_bottom": (35, 22, 5),  "accent": (255, 215, 90)},
}
_RARITY_STARS = {"Common": 1, "Rare": 2, "Legendary": 3}
_RARITY_STAR_MAX = 3


def _load_font(paths: list[str], size: int) -> "ImageFont.FreeTypeFont":
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default(size)


def _font_bold(size: int) -> "ImageFont.FreeTypeFont":
    return _load_font(_FONT_BOLD_PATHS, size)


def _font_regular(size: int) -> "ImageFont.FreeTypeFont":
    return _load_font(_FONT_REGULAR_PATHS, size)


def _font_italic(size: int) -> "ImageFont.FreeTypeFont":
    return _load_font(_FONT_ITALIC_PATHS, size)


def _draw_critter(draw: "ImageDraw.ImageDraw", cx: int, cy: int, r: int, card_id: str, body_color: tuple, line_color: tuple) -> None:
    """Draw a small procedural creature face — round head, and a random
    combination of ear/horn style, eye style, optional wings, and a
    muzzle, all seeded by the card's id so every card gets a distinct,
    stable-looking mascot. Pure vector shapes: no fonts, no emoji
    rendering, no bundled per-card image assets, so it looks identical
    on every host."""
    rng = random.Random(card_id)
    ear_style = rng.choice(["round", "pointy", "horn", "long", "fin"])
    eye_style = rng.choice(["round", "sleepy", "wide"])
    has_wings = rng.random() < 0.35
    muzzle_style = rng.choice(["short", "long", "none"])

    head_r = r * 0.62

    if has_wings:
        for side in (-1, 1):
            wx = cx + side * head_r * 0.9
            pts = [
                (cx + side * head_r * 0.3, cy - head_r * 0.1),
                (wx + side * head_r * 0.9, cy - head_r * 0.9),
                (wx + side * head_r * 0.7, cy + head_r * 0.1),
                (cx + side * head_r * 0.3, cy + head_r * 0.5),
            ]
            draw.polygon(pts, fill=(*line_color, 160), outline=line_color)

    if ear_style == "round":
        for side in (-1, 1):
            ex, ey = cx + side * head_r * 0.65, cy - head_r * 0.75
            er = head_r * 0.32
            draw.ellipse([ex - er, ey - er, ex + er, ey + er], fill=body_color, outline=line_color, width=2)
    elif ear_style == "pointy":
        for side in (-1, 1):
            bx = cx + side * head_r * 0.55
            by = cy - head_r * 0.55
            tipx = cx + side * head_r * 0.95
            tipy = cy - head_r * 1.35
            draw.polygon([(bx - 14, by), (tipx, tipy), (bx + side * 22, by - 6)], fill=body_color, outline=line_color, width=2)
    elif ear_style == "horn":
        for side in (-1, 1):
            bx = cx + side * head_r * 0.45
            by = cy - head_r * 0.7
            tipx = cx + side * head_r * 0.15
            tipy = cy - head_r * 1.5
            draw.line([(bx, by), (tipx, tipy)], fill=line_color, width=8)
    elif ear_style == "long":
        for side in (-1, 1):
            bx = cx + side * head_r * 0.4
            by = cy - head_r * 0.7
            draw.ellipse([bx - 16, by - head_r * 1.1, bx + 16, by + 10], fill=body_color, outline=line_color, width=2)
    elif ear_style == "fin":
        pts = [(cx - head_r * 0.5, cy - head_r * 0.8), (cx, cy - head_r * 1.5), (cx + head_r * 0.5, cy - head_r * 0.8)]
        draw.polygon(pts, fill=body_color, outline=line_color, width=2)

    draw.ellipse([cx - head_r, cy - head_r, cx + head_r, cy + head_r], fill=body_color, outline=line_color, width=3)

    if muzzle_style == "short":
        mr = head_r * 0.35
        my = cy + head_r * 0.35
        draw.ellipse([cx - mr, my - mr * 0.7, cx + mr, my + mr * 0.7], fill=(255, 255, 255, 230))
    elif muzzle_style == "long":
        mr = head_r * 0.3
        my = cy + head_r * 0.5
        draw.ellipse([cx - mr * 1.3, my - mr * 0.6, cx + mr * 1.3, my + mr * 0.9], fill=(255, 255, 255, 230))

    eye_off_x = head_r * 0.36
    eye_off_y = -head_r * 0.05
    for side in (-1, 1):
        ex = cx + side * eye_off_x
        ey = cy + eye_off_y
        if eye_style == "round":
            r_e = head_r * 0.16
            draw.ellipse([ex - r_e, ey - r_e, ex + r_e, ey + r_e], fill=(30, 30, 35, 255))
            draw.ellipse([ex - r_e * 0.35, ey - r_e * 0.35 - 2, ex + r_e * 0.05, ey + r_e * 0.05 - 2], fill=(255, 255, 255, 255))
        elif eye_style == "sleepy":
            r_e = head_r * 0.16
            draw.arc([ex - r_e, ey - r_e * 0.6, ex + r_e, ey + r_e * 0.6], start=200, end=340, fill=(30, 30, 35, 255), width=5)
        elif eye_style == "wide":
            r_e = head_r * 0.2
            draw.ellipse([ex - r_e, ey - r_e, ex + r_e, ey + r_e], fill=(255, 255, 255, 255), outline=(30, 30, 35, 255), width=2)
            draw.ellipse([ex - r_e * 0.45, ey - r_e * 0.45, ex + r_e * 0.45, ey + r_e * 0.45], fill=(30, 30, 35, 255))


def _draw_stars(draw: "ImageDraw.ImageDraw", center_x: int, y: int, filled: int, total: int, color: tuple, *, size: int = 11, gap: int = 8) -> None:
    """Draw a row of 5-pointed stars as vector shapes (filled or outline)
    instead of text glyphs — star characters like ★/☆ render as tofu boxes
    on hosts without a font that includes them, so we don't rely on any
    font here at all."""
    import math
    span = total * (size * 2 + gap) - gap
    start_x = center_x - span // 2
    for i in range(total):
        cx = start_x + i * (size * 2 + gap) + size
        points = []
        for k in range(10):
            angle = math.pi / 2 + k * math.pi / 5
            r = size if k % 2 == 0 else size * 0.42
            points.append((cx + r * math.cos(angle), y - r * math.sin(angle)))
        if i < filled:
            draw.polygon(points, fill=color)
        else:
            draw.polygon(points, outline=color)


def _vertical_gradient(size: tuple[int, int], top: tuple[int, int, int], bottom: tuple[int, int, int]) -> "Image.Image":
    w, h = size
    img = Image.new("RGB", (w, h), top)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    return img


def _wrap_text(draw: "ImageDraw.ImageDraw", text: str, font: "ImageFont.FreeTypeFont", max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


_CARD_ART_DIR = os.path.join(os.path.dirname(__file__), "assets", "card_art")


def _load_card_art(card_id: str, box_size: int) -> "Image.Image | None":
    """Look for a real art file at cogs/assets/card_art/<card_id>.png (or
    .jpg/.jpeg/.webp) and return it scaled to fit within a box_size x
    box_size square, preserving aspect ratio. Returns None if no art file
    exists yet for this card — caller should fall back to the procedural
    critter placeholder in that case, so cards can be swapped over to real
    art one at a time as they're finished."""
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        path = os.path.join(_CARD_ART_DIR, f"{card_id}{ext}")
        if os.path.exists(path):
            try:
                art = Image.open(path).convert("RGBA")
                art.thumbnail((box_size, box_size), Image.LANCZOS)
                return art
            except Exception:
                return None
    return None


def render_card_image(card_id: str, qty: int | None = None) -> bytes:
    """Render a single card as a PNG and return the raw bytes. If qty is
    given, a 'You own: xN' footer is added."""
    card = CARD_DEFS[card_id]
    pal = _RARITY_PALETTE[card["rarity"]]

    img = _vertical_gradient((_CARD_W, _CARD_H), pal["grad_top"], pal["grad_bottom"]).convert("RGBA")
    draw = ImageDraw.Draw(img, "RGBA")

    # Border
    draw.rounded_rectangle([6, 6, _CARD_W - 7, _CARD_H - 7], radius=20, outline=pal["border"], width=6)
    draw.rounded_rectangle([16, 16, _CARD_W - 17, _CARD_H - 17], radius=14, outline=pal["accent"], width=2)

    # Name banner
    name_font = _font_bold(34)
    name = card["name"]
    while draw.textlength(name, font=name_font) > _CARD_W - 80 and name_font.size > 18:
        name_font = _font_bold(name_font.size - 2)
    draw.text((_CARD_W // 2, 55), name, font=name_font, fill=(255, 255, 255, 255), anchor="mm")
    draw.line([(40, 85), (_CARD_W - 40, 85)], fill=pal["accent"], width=2)

    # Art frame
    art_l, art_t, art_r, art_b = 40, 105, _CARD_W - 40, 430
    draw.rounded_rectangle([art_l, art_t, art_r, art_b], radius=14, fill=(0, 0, 0, 90), outline=pal["accent"], width=2)
    cx, cy = (art_l + art_r) // 2, (art_t + art_b) // 2
    r_outer = min(art_r - art_l, art_b - art_t) // 2 - 10

    real_art = _load_card_art(card_id, box_size=(art_r - art_l - 20))
    if real_art is not None:
        img.paste(real_art, (cx - real_art.width // 2, cy - real_art.height // 2), real_art)
    else:
        _draw_critter(draw, cx, cy, r_outer, card_id, pal["border"], pal["accent"])

    # Rarity + stars
    draw.text((_CARD_W // 2, 455), card["rarity"].upper(), font=_font_bold(20), fill=pal["accent"], anchor="mm")
    _draw_stars(draw, _CARD_W // 2, 483, _RARITY_STARS[card["rarity"]], _RARITY_STAR_MAX, pal["accent"])

    # Power badge
    draw.rounded_rectangle([_CARD_W - 140, 505, _CARD_W - 40, 545], radius=10, fill=(0, 0, 0, 120), outline=pal["accent"], width=2)
    draw.text((_CARD_W - 90, 525), f"PWR {card['power']}", font=_font_bold(22), fill=(255, 255, 255, 255), anchor="mm")

    # Set label
    set_def = CARD_SETS.get(card.get("set"))
    if set_def:
        draw.text((40, 525), f"Set: {set_def['name']}", font=_font_regular(16), fill=(220, 220, 220, 255), anchor="lm")

    # Flavor text
    flavor_font = _font_italic(17)
    lines = _wrap_text(draw, card["flavor"], flavor_font, _CARD_W - 80)
    y = 570
    for line in lines[:4]:
        draw.text((_CARD_W // 2, y), line, font=flavor_font, fill=(210, 210, 210, 255), anchor="mm")
        y += 22

    if qty is not None:
        draw.text((_CARD_W // 2, _CARD_H - 30), f"You own: x{qty}", font=_font_bold(18), fill=pal["accent"], anchor="mm")

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def card_file(card_id: str, qty: int | None = None, *, filename: str = "card.png") -> discord.File:
    """Render card_id and wrap it as a discord.File ready to attach."""
    return discord.File(io.BytesIO(render_card_image(card_id, qty=qty)), filename=filename)


def _card_line(card_id: str, qty: int) -> str:
    card = CARD_DEFS[card_id]
    return f"🃏 **{card['name']}** ({card['rarity']}) — x{qty}"


def _collection_embed(member: discord.Member | discord.User) -> discord.Embed:
    inv = get_user_cards(member.id)
    total_copies = sum(inv.values())
    unique = len(inv)

    embed = discord.Embed(
        title=f"🃏 {member.display_name}'s Card Collection",
        description=(
            f"**{unique}/{len(CARD_DEFS)}** unique cards — **{total_copies}** total copies"
            if inv else "No cards yet — cards are granted by the bot owner for now."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    for rarity in _RARITY_ORDER:
        owned_in_tier = [
            (cid, qty) for cid, qty in inv.items()
            if CARD_DEFS[cid]["rarity"] == rarity
        ]
        if not owned_in_tier:
            continue
        lines = [_card_line(cid, qty) for cid, qty in sorted(owned_in_tier)]
        embed.add_field(name=rarity, value="\n".join(lines), inline=False)

    return embed


def _card_info_view(card_query: str, *, viewer_id: int) -> tuple[discord.Embed, discord.File | None]:
    """Look up a single card by name (case-insensitive) and build a detail
    embed + rendered card image for it, including how many copies the
    viewer personally owns. Returns (embed, file); file is None on a
    not-found error, in which case the embed alone carries the message."""
    card_query = card_query.strip().lower()
    match = next(
        ((cid, c) for cid, c in CARD_DEFS.items() if c["name"].lower() == card_query),
        None,
    )
    if match is None:
        embed = discord.Embed(
            description=f"❌ Unknown card **{card_query}** — check `/carddex` for the exact name.",
            color=discord.Color.red(),
        )
        return embed, None

    card_id, card = match
    owned = get_card_quantity(viewer_id, card_id)

    file = card_file(card_id, qty=owned, filename="card.png")
    embed = discord.Embed(title=f"🃏 {card['name']}", color=RARITIES[card["rarity"]]["color"])
    embed.set_image(url="attachment://card.png")
    return embed, file


def _dex_overview_embed(viewer_id: int) -> discord.Embed:
    owned_count = sum(1 for cid in CARD_DEFS if get_card_quantity(viewer_id, cid) > 0)
    total_weight = sum(r["weight"] for r in RARITIES.values())
    embed = discord.Embed(
        title="📖 Card Dex",
        description=(
            f"You own **{owned_count}/{len(CARD_DEFS)}** cards overall.\n"
            f"Use the dropdown below to browse a specific rarity, or `/cardinfo <name>` for one card's details."
        ),
        color=discord.Color.blurple(),
    )
    for rarity in _RARITY_ORDER:
        cards_in_tier = [cid for cid, c in CARD_DEFS.items() if c["rarity"] == rarity]
        owned_in_tier = sum(1 for cid in cards_in_tier if get_card_quantity(viewer_id, cid) > 0)
        pct = RARITIES[rarity]["weight"] / total_weight * 100
        embed.add_field(
            name=rarity,
            value=f"{owned_in_tier}/{len(cards_in_tier)} owned\n~{pct:.1f}% pull chance",
            inline=True,
        )
    embed.set_footer(text="See /cardsets for set-completion bonuses.")
    return embed


def _dex_rarity_embed(viewer_id: int, rarity: str) -> discord.Embed:
    cards_in_tier = [(cid, c) for cid, c in CARD_DEFS.items() if c["rarity"] == rarity]
    total_weight = sum(r["weight"] for r in RARITIES.values())
    pct = RARITIES[rarity]["weight"] / total_weight * 100
    owned_count = sum(1 for cid, _ in cards_in_tier if get_card_quantity(viewer_id, cid) > 0)

    embed = discord.Embed(
        title=f"📖 Card Dex — {rarity}",
        description=f"You own **{owned_count}/{len(cards_in_tier)}** {rarity} cards (~{pct:.1f}% pull chance).",
        color=RARITIES[rarity]["color"],
    )
    if cards_in_tier:
        lines = []
        for cid, c in cards_in_tier:
            owned = get_card_quantity(viewer_id, cid) > 0
            mark = "✅" if owned else "❔"
            name = f"**{c['name']}**" if owned else f"~~{c['name']}~~"
            lines.append(f"{mark} {name} — {c['flavor']}")
        embed.add_field(name=f"{rarity} cards", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name=f"{rarity} cards", value="*No cards defined for this rarity yet.*", inline=False)
    return embed


class _DexRaritySelect(discord.ui.Select):
    def __init__(self, viewer_id: int):
        self.viewer_id = viewer_id
        options = [discord.SelectOption(label="Overview", value="__overview__", emoji="📖", description="See counts across all rarities")]
        options += [
            discord.SelectOption(label=rarity, value=rarity, description=f"Browse {rarity} cards")
            for rarity in _RARITY_ORDER
        ]
        super().__init__(placeholder="Browse by rarity...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.viewer_id:
            await interaction.response.send_message("This isn't your dex to browse — run `/carddex` yourself!", ephemeral=True)
            return
        choice = self.values[0]
        embed = _dex_overview_embed(self.viewer_id) if choice == "__overview__" else _dex_rarity_embed(self.viewer_id, choice)
        await interaction.response.edit_message(embed=embed)


class DexView(discord.ui.View):
    def __init__(self, viewer_id: int, *, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.message: discord.Message | None = None
        self.add_item(_DexRaritySelect(viewer_id))

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ── Trading ────────────────────────────────────────────────────────────────
# Mirrors GiftSubRequestView in cogs/economy.py: nothing is deducted until
# the recipient explicitly accepts, so a decline/timeout never needs a
# refund because nothing was ever taken.

_pending_trades: dict[tuple[int, int], bool] = {}  # (sender_id, recipient_id) -> True while pending


class TradeRequestView(discord.ui.View):
    def __init__(
        self,
        sender: discord.User | discord.Member,
        recipient: discord.User | discord.Member,
        card_id: str,
        amount: int,
        asking_price: int,
        *,
        timeout: float = 60,
    ):
        super().__init__(timeout=timeout)
        self.sender = sender
        self.recipient = recipient
        self.card_id = card_id
        self.amount = amount
        self.asking_price = asking_price
        self.message: discord.Message | None = None
        self._resolved = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.recipient.id:
            await interaction.response.send_message("This trade offer isn't for you!", ephemeral=True)
            return False
        return True

    def _disable(self) -> None:
        for child in self.children:
            child.disabled = True

    async def _resolve(self, interaction: discord.Interaction, accepted: bool) -> None:
        if self._resolved:
            return
        self._resolved = True
        self._disable()
        _pending_trades.pop((self.sender.id, self.recipient.id), None)

        card = CARD_DEFS[self.card_id]

        if accepted:
            if self.asking_price > 0 and not spend_credits(self.recipient.id, self.asking_price):
                embed = discord.Embed(
                    description=f"❌ Trade failed — you no longer have enough {JC_EMOJI} to cover this offer.",
                    color=discord.Color.red(),
                )
                await interaction.response.edit_message(embed=embed, view=self)
                return

            if not transfer_card(self.sender.id, self.recipient.id, self.card_id, self.amount):
                # Sender no longer has the card(s) — refund the recipient if charged.
                if self.asking_price > 0:
                    add_credits(self.recipient.id, self.asking_price)
                embed = discord.Embed(
                    description=(
                        f"❌ Trade failed — **{self.sender.display_name}** no longer has "
                        f"enough {card['name']} to complete this trade. You weren't charged."
                    ),
                    color=discord.Color.red(),
                )
                await interaction.response.edit_message(embed=embed, view=self)
                return

            if self.asking_price > 0:
                add_credits(self.sender.id, self.asking_price)

            price_note = f" in exchange for **{self.asking_price:,}** {JC_EMOJI}" if self.asking_price > 0 else ""
            embed = discord.Embed(
                title="🃏 Trade Complete!",
                description=(
                    f"**{self.recipient.display_name}** received **{card['name']}** "
                    f"x{self.amount} from **{self.sender.display_name}**{price_note}."
                ),
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                title="🃏 Trade Declined",
                description=(
                    f"❌ **{self.recipient.display_name}** declined the trade offer from "
                    f"**{self.sender.display_name}**. Nothing was exchanged."
                ),
                color=discord.Color.red(),
            )

        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, accepted=True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, accepted=False)

    async def on_timeout(self) -> None:
        if self._resolved:
            return
        self._resolved = True
        _pending_trades.pop((self.sender.id, self.recipient.id), None)
        self._disable()
        if self.message:
            try:
                embed = discord.Embed(
                    title="🃏 Trade Expired",
                    description="⏰ This trade offer expired with no response. Nothing was exchanged.",
                    color=discord.Color.dark_gray(),
                )
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass


# ── Daily quests ─────────────────────────────────────────────────────────
# Each quest tracks progress against a stat that already exists elsewhere
# in state.py (messages sent, songs played, game wins, JC earned) rather
# than adding new per-action tracking. A "baseline" snapshot of that stat
# is taken the moment a quest is assigned for the day, and progress is
# (current value - baseline) — so it doesn't matter that e.g. songs_played
# is a lifetime counter, only how much it moved *today*. "claim_pull" is
# the one exception: it's inherently day-scoped already (has_claimed_daily_card
# checks "today"), so it reads directly with no baseline needed.

QUEST_DEFS: dict[str, dict] = {
    "chat100":    {"desc": "Send 100 messages to Jarvis",            "target": 100, "kind": "delta", "get": lambda uid: (get_stats(uid) or {}).get("messages", 0)},
    "spend100jc": {"desc": f"Spend 100 {JC_EMOJI} {JC_NAME}s",        "target": 100, "kind": "delta", "get": lambda uid: get_lifetime_spent(uid)},
    "earn300jc":  {"desc": f"Earn 300 {JC_EMOJI} {JC_NAME}s",         "target": 300, "kind": "delta", "get": lambda uid: get_lifetime_earned(uid)},
    "search5":    {"desc": "Do 5 image searches",                    "target": 5,   "kind": "delta", "get": lambda uid: get_image_search_count(uid)},
    "claimpull":  {"desc": "Claim your free daily card pull",        "target": 1,   "kind": "today", "get": lambda uid: 1 if has_claimed_daily_card(uid) else 0},
}
QUESTS_PER_DAY = 4
QUEST_REWARD_JC = 25
QUEST_COMPLETE_ALL_BONUS_JC = 100


def _get_or_assign_quests(user_id: int) -> dict:
    """Return today's quest record for user_id, assigning a fresh set
    (deterministically shuffled per user+day, so repeated calls the same
    day return the same set of quests) if none exists yet today."""
    entry = get_daily_quests(user_id)
    if entry is not None:
        return entry

    rng = random.Random(f"{user_id}-{today_utc()}")
    quest_ids = rng.sample(list(QUEST_DEFS.keys()), QUESTS_PER_DAY)
    baselines = {
        qid: QUEST_DEFS[qid]["get"](user_id)
        for qid in quest_ids
        if QUEST_DEFS[qid]["kind"] == "delta"
    }
    return assign_daily_quests(user_id, quest_ids, baselines)


def _quest_progress(user_id: int, quest_id: str, entry: dict) -> int:
    qdef = QUEST_DEFS[quest_id]
    current = qdef["get"](user_id)
    if qdef["kind"] == "delta":
        baseline = entry["baselines"].get(quest_id, current)
        return max(0, min(qdef["target"], current - baseline))
    return max(0, min(qdef["target"], current))


def _quests_embed(user: discord.User | discord.Member, entry: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"📜 {user.display_name}'s Daily Quests",
        description="Finish quests to earn JC and a random card. Quests reset every day.",
        color=discord.Color.teal(),
    )
    for qid in entry["quest_ids"]:
        qdef = QUEST_DEFS[qid]
        progress = _quest_progress(user.id, qid, entry)
        done = progress >= qdef["target"]
        claimed = qid in entry["claimed"]
        if claimed:
            status = "✅ Claimed"
        elif done:
            status = "🎁 Ready to claim!"
        else:
            status = f"{progress}/{qdef['target']}"
        embed.add_field(name=qdef["desc"], value=status, inline=False)
    unclaimed_done = [qid for qid in entry["quest_ids"] if qid not in entry["claimed"] and _quest_progress(user.id, qid, entry) >= QUEST_DEFS[qid]["target"]]
    if unclaimed_done:
        embed.set_footer(text="Use the buttons below to claim your rewards.")
    elif len(entry["claimed"]) == len(entry["quest_ids"]):
        embed.set_footer(text="All of today's quests are complete — nice work! Come back tomorrow for new ones.")
    return embed


class QuestClaimView(discord.ui.View):
    """One button per unclaimed-but-completed quest, shown under /quests.
    Rewards are granted on click so the view stays valid even if the user
    revisits /quests later the same day (already-claimed quests just don't
    get a button)."""

    def __init__(self, user: discord.User | discord.Member, entry: dict, *, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.user = user
        self.message: discord.Message | None = None
        for qid in entry["quest_ids"]:
            if qid in entry["claimed"]:
                continue
            if _quest_progress(user.id, qid, entry) < QUEST_DEFS[qid]["target"]:
                continue
            self.add_item(self._make_button(qid))

    def _make_button(self, quest_id: str) -> discord.ui.Button:
        button = discord.ui.Button(label=f"Claim: {QUEST_DEFS[quest_id]['desc'][:60]}", style=discord.ButtonStyle.success, emoji="🎁")

        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user.id:
                await interaction.response.send_message("These quests aren't yours to claim!", ephemeral=True)
                return
            entry = get_daily_quests(self.user.id)
            if entry is None or quest_id not in entry["quest_ids"] or quest_id in entry["claimed"]:
                await interaction.response.send_message("That quest was already claimed or has expired.", ephemeral=True)
                return
            mark_quest_claimed(self.user.id, quest_id)
            add_credits(self.user.id, QUEST_REWARD_JC)
            card_id, new_qty = _grant_random_card(self.user.id, exclude=("Legendary",))
            card_def = CARD_DEFS[card_id]

            bonus_note = ""
            entry = get_daily_quests(self.user.id)
            if entry and len(entry["claimed"]) == len(entry["quest_ids"]):
                add_credits(self.user.id, QUEST_COMPLETE_ALL_BONUS_JC)
                bonus_note = f"\n🏆 **All quests complete!** Bonus **+{QUEST_COMPLETE_ALL_BONUS_JC}** {JC_EMOJI}."

            button.disabled = True
            button.label = "Claimed"
            embed = discord.Embed(
                title=f"🃏 {card_def['name']}",
                description=(
                    f"🎁 Quest complete! You earned **+{QUEST_REWARD_JC}** {JC_EMOJI} and a new card, "
                    f"revealed below.{bonus_note}"
                ),
                color=RARITIES[card_def["rarity"]]["color"],
            )
            embed.set_image(url="attachment://card.png")
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(embed=embed, file=card_file(card_id, qty=new_qty))

        button.callback = callback
        return button

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ── Set completion rewards ──────────────────────────────────────────────

def _set_progress(user_id: int, set_id: str) -> tuple[int, int]:
    """Return (owned_unique, total) for a set — how many distinct cards in
    the set the user owns at least one copy of, out of the set's size."""
    set_def = CARD_SETS[set_id]
    owned = sum(1 for cid in set_def["card_ids"] if get_card_quantity(user_id, cid) > 0)
    return owned, len(set_def["card_ids"])


def _cardsets_embed(user: discord.User | discord.Member) -> discord.Embed:
    embed = discord.Embed(
        title=f"🧩 {user.display_name}'s Set Progress",
        description="Own at least one copy of every card in a set to claim its one-time bonus.",
        color=discord.Color.gold(),
    )
    claimed = get_claimed_sets(user.id)
    for set_id, set_def in CARD_SETS.items():
        owned, total = _set_progress(user.id, set_id)
        if set_id in claimed:
            status = "✅ Reward claimed"
        elif owned >= total:
            status = f"🎁 Complete ({owned}/{total}) — ready to claim!"
        else:
            status = f"{owned}/{total} collected"
        card_names = ", ".join(CARD_DEFS[cid]["name"] for cid in set_def["card_ids"])
        embed.add_field(
            name=f"{set_def['name']} — {set_def['reward_credits']:,} {JC_EMOJI} bonus",
            value=f"{status}\n*{card_names}*",
            inline=False,
        )
    return embed


class SetClaimView(discord.ui.View):
    def __init__(self, user: discord.User | discord.Member, *, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.user = user
        self.message: discord.Message | None = None
        claimed = get_claimed_sets(user.id)
        for set_id, set_def in CARD_SETS.items():
            if set_id in claimed:
                continue
            owned, total = _set_progress(user.id, set_id)
            if owned < total:
                continue
            self.add_item(self._make_button(set_id, set_def))

    def _make_button(self, set_id: str, set_def: dict) -> discord.ui.Button:
        button = discord.ui.Button(label=f"Claim: {set_def['name']}", style=discord.ButtonStyle.success, emoji="🧩")

        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user.id:
                await interaction.response.send_message("This set progress isn't yours to claim!", ephemeral=True)
                return
            owned, total = _set_progress(self.user.id, set_id)
            if owned < total:
                await interaction.response.send_message("You don't own every card in this set yet.", ephemeral=True)
                return
            if not claim_set_reward(self.user.id, set_id):
                await interaction.response.send_message("This set's reward was already claimed.", ephemeral=True)
                return
            add_credits(self.user.id, set_def["reward_credits"])
            button.disabled = True
            button.label = "Claimed"
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                f"🧩 **{set_def['name']}** set complete! You earned **+{set_def['reward_credits']:,}** {JC_EMOJI}."
            )

        button.callback = callback
        return button

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Cards(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── !cardinfo / /cardinfo ────────────────────────────────────────────
    @commands.command(name="cardinfo", aliases=["cinfo"])
    async def prefix_cardinfo(self, ctx: commands.Context, *, card: str):
        """!cardinfo <card name> — view full details on one specific card."""
        embed, file = _card_info_view(card, viewer_id=ctx.author.id)
        await ctx.reply(embed=embed, file=file) if file else await ctx.reply(embed=embed)

    @app_commands.command(name="cardinfo", description="View full details on one specific card")
    @app_commands.describe(card="The card's name, e.g. 'Card 1'")
    async def slash_cardinfo(self, interaction: discord.Interaction, card: str):
        embed, file = _card_info_view(card, viewer_id=interaction.user.id)
        if file:
            await interaction.response.send_message(embed=embed, file=file)
        else:
            await interaction.response.send_message(embed=embed)

    @slash_cardinfo.autocomplete("card")
    async def cardinfo_autocomplete(self, interaction: discord.Interaction, current: str):
        current = current.strip().lower()
        matches = [c["name"] for c in CARD_DEFS.values() if current in c["name"].lower()]
        return [app_commands.Choice(name=name, value=name) for name in matches[:25]]

    # ── !cards / /cards ──────────────────────────────────────────────────
    @commands.command(name="cards", aliases=["collection", "mycards"])
    async def prefix_cards(self, ctx: commands.Context, user: discord.User = None):
        """!cards [@user] — view your (or someone else's) card collection."""
        target = user or ctx.author
        await ctx.reply(embed=_collection_embed(target))

    @app_commands.command(name="cards", description="View your (or someone else's) card collection")
    @app_commands.describe(user="Whose collection to view — defaults to you")
    async def slash_cards(self, interaction: discord.Interaction, user: discord.User = None):
        target = user or interaction.user
        await interaction.response.send_message(embed=_collection_embed(target))

    # ── !dailycard / /dailycard ────────────────────────────────────────
    async def _do_dailycard(self, respond, user: discord.User | discord.Member):
        if not claim_daily_card(user.id):
            await respond(content="⏰ You've already claimed your free daily card pull today — come back after 00:00 UTC.", ephemeral=True)
            return
        card_id = _pick_card()
        new_qty = add_card(user.id, card_id)
        card_def = CARD_DEFS[card_id]
        embed = discord.Embed(
            title=f"🃏 Daily Pull! {card_def['name']}",
            description=f"You pulled **{card_def['name']}** ({card_def['rarity']}) — revealed below.",
            color=RARITIES[card_def["rarity"]]["color"],
        )
        embed.set_image(url="attachment://card.png")
        await respond(embed=embed, file=card_file(card_id, qty=new_qty))

    @commands.command(name="dailycard", aliases=["pullcard"])
    async def prefix_dailycard(self, ctx: commands.Context):
        """!dailycard — claim your free daily weighted-random card pull."""
        async def respond(content=None, embed=None, file=None, ephemeral=False):
            await ctx.reply(content=content, embed=embed, file=file)
        await self._do_dailycard(respond, ctx.author)

    @app_commands.command(name="dailycard", description="Claim your free daily weighted-random card pull")
    async def slash_dailycard(self, interaction: discord.Interaction):
        async def respond(content=None, embed=None, file=None, ephemeral=False):
            await interaction.response.send_message(content=content, embed=embed, file=file, ephemeral=ephemeral)
        await self._do_dailycard(respond, interaction.user)

    # ── !quests / /quests ─────────────────────────────────────────────
    @commands.command(name="quests", aliases=["cardquests"])
    async def prefix_quests(self, ctx: commands.Context):
        """!quests — view today's card quests and claim finished ones."""
        entry = _get_or_assign_quests(ctx.author.id)
        view = QuestClaimView(ctx.author, entry)
        view.message = await ctx.reply(embed=_quests_embed(ctx.author, entry), view=view)

    @app_commands.command(name="quests", description="View today's card quests and claim finished ones")
    async def slash_quests(self, interaction: discord.Interaction):
        entry = _get_or_assign_quests(interaction.user.id)
        view = QuestClaimView(interaction.user, entry)
        await interaction.response.send_message(embed=_quests_embed(interaction.user, entry), view=view)
        view.message = await interaction.original_response()

    # ── !cardsets / /cardsets ─────────────────────────────────────────
    @commands.command(name="cardsets", aliases=["csets"])
    async def prefix_cardsets(self, ctx: commands.Context):
        """!cardsets — view set-completion progress and claim finished sets."""
        view = SetClaimView(ctx.author)
        view.message = await ctx.reply(embed=_cardsets_embed(ctx.author), view=view)

    @app_commands.command(name="cardsets", description="View set-completion progress and claim finished sets")
    async def slash_cardsets(self, interaction: discord.Interaction):
        view = SetClaimView(interaction.user)
        await interaction.response.send_message(embed=_cardsets_embed(interaction.user), view=view)
        view.message = await interaction.original_response()

    # ── !carddex / /carddex ──────────────────────────────────────────────
    @commands.command(name="carddex", aliases=["dex"])
    async def prefix_carddex(self, ctx: commands.Context):
        """!carddex / !dex — browse the full set of collectible cards."""
        view = DexView(ctx.author.id)
        view.message = await ctx.reply(embed=_dex_overview_embed(ctx.author.id), view=view)

    @app_commands.command(name="carddex", description="Browse the full set of collectible cards")
    async def slash_carddex(self, interaction: discord.Interaction):
        view = DexView(interaction.user.id)
        await interaction.response.send_message(embed=_dex_overview_embed(interaction.user.id), view=view)
        view.message = await interaction.original_response()

    # ── !cardtrade / /cardtrade ──────────────────────────────────────────
    async def _start_trade(
        self,
        respond,  # async callable(content=None, embed=None, view=None, ephemeral=False) -> discord.Message-ish
        sender: discord.User | discord.Member,
        recipient: discord.User | discord.Member,
        card_name: str,
        amount: int,
        asking_price: int,
    ):
        """Shared validation + offer-sending logic for both command styles.
        `respond` must return the sent message (or None on an early error
        reply) so the caller can stash it on the view for on_timeout edits."""
        if recipient.id == sender.id:
            await respond(content="❌ You can't trade with yourself.", ephemeral=True)
            return
        if recipient.bot:
            await respond(content="❌ You can't trade with a bot.", ephemeral=True)
            return

        card_id = next(
            (cid for cid, c in CARD_DEFS.items() if c["name"].lower() == card_name.strip().lower()),
            None,
        )
        if card_id is None:
            await respond(content=f"❌ Unknown card **{card_name}** — check `/carddex` for the exact name.", ephemeral=True)
            return

        owned = get_card_quantity(sender.id, card_id)
        if owned < amount:
            await respond(
                content=f"❌ You only own **x{owned}** {CARD_DEFS[card_id]['name']}, can't offer **x{amount}**.",
                ephemeral=True,
            )
            return

        key = (sender.id, recipient.id)
        if key in _pending_trades:
            await respond(
                content=f"⚠️ You already have a pending trade offer to **{recipient.display_name}**. Wait for them to respond first.",
                ephemeral=True,
            )
            return

        _pending_trades[key] = True

        card_def = CARD_DEFS[card_id]
        price_note = f"for **{asking_price:,}** {JC_EMOJI}" if asking_price > 0 else "as a gift — free"
        embed = discord.Embed(
            title="🃏 Incoming Trade Offer",
            description=(
                f"**{sender.display_name}** wants to send you "
                f"**{card_def['name']}** x{amount} ({card_def['rarity']}) {price_note}.\n\n"
                f"Do you accept?"
            ),
            color=RARITIES[card_def["rarity"]]["color"],
        )
        embed.set_footer(text="This offer expires in 60 seconds. Nothing is exchanged unless you accept.")
        embed.set_thumbnail(url=sender.display_avatar.url)

        view = TradeRequestView(sender, recipient, card_id, amount, asking_price)
        view.message = await respond(
            content=f"{recipient.mention}, you've got a trade offer!", embed=embed, view=view
        )

    @commands.command(name="cardtrade", aliases=["trade"])
    async def prefix_cardtrade(
        self,
        ctx: commands.Context,
        recipient: discord.User,
        card: str,
        amount: int = 1,
        asking_price: int = 0,
    ):
        """!cardtrade @user <card name> [amount] [asking_price] — offer a
        card to another user, optionally for a JC price.
        Example: !cardtrade @Someone "Card 1" 2 50"""
        amount = max(1, min(amount, 99))
        asking_price = max(0, asking_price)

        async def respond(content=None, embed=None, view=None, ephemeral=False):
            return await ctx.reply(content=content, embed=embed, view=view)

        await self._start_trade(respond, ctx.author, recipient, card, amount, asking_price)

    @app_commands.command(name="cardtrade", description="Offer a card to another user, optionally for a JC price")
    @app_commands.describe(
        recipient="Who to send the trade offer to",
        card="Which card to offer (use its name, e.g. 'Card 1')",
        amount="How many copies to offer (default 1)",
        asking_price="JC to request in return — leave at 0 to just gift it",
    )
    async def slash_cardtrade(
        self,
        interaction: discord.Interaction,
        recipient: discord.User,
        card: str,
        amount: app_commands.Range[int, 1, 99] = 1,
        asking_price: app_commands.Range[int, 0, 1_000_000] = 0,
    ):
        sender = interaction.user

        async def respond(content=None, embed=None, view=None, ephemeral=False):
            await interaction.response.send_message(content=content, embed=embed, view=view, ephemeral=ephemeral)
            return None if ephemeral else await interaction.original_response()

        await self._start_trade(respond, sender, recipient, card, amount, asking_price)

    @slash_cardtrade.autocomplete("card")
    async def cardtrade_card_autocomplete(self, interaction: discord.Interaction, current: str):
        current = current.strip().lower()
        matches = [c["name"] for c in CARD_DEFS.values() if current in c["name"].lower()]
        return [app_commands.Choice(name=name, value=name) for name in matches[:25]]

    # ── !givecard (owner only) ──────────────────────────────────────────
    # Normal acquisition is /dailycard and /quests — this is an owner-only
    # override for one-off grants, corrections, and event rewards.
    @commands.command(name="givecard")
    @commands.is_owner()
    async def prefix_givecard(self, ctx: commands.Context, user: discord.User, *, card: str):
        """!givecard @user <card name> — owner only, grants one copy of a card."""
        card_id = next(
            (cid for cid, c in CARD_DEFS.items() if c["name"].lower() == card.strip().lower()),
            None,
        )
        if card_id is None:
            await ctx.reply(f"❌ Unknown card **{card}** — check `/carddex` for the exact name.")
            return

        card_def = CARD_DEFS[card_id]
        new_qty = add_card(user.id, card_id)
        embed = discord.Embed(
            title=f"🃏 {card_def['name']}",
            description=f"✅ Gave **{card_def['name']}** ({card_def['rarity']}) to **{user.display_name}** — they now own **x{new_qty}**.",
            color=RARITIES[card_def["rarity"]]["color"],
        )
        embed.set_image(url="attachment://card.png")
        await ctx.reply(embed=embed, file=card_file(card_id, qty=new_qty))

    @prefix_givecard.error
    async def givecard_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.NotOwner):
            await ctx.reply("🚫 Only the bot owner can use this command.")
        elif isinstance(error, (commands.MissingRequiredArgument, commands.UserNotFound, commands.BadArgument)):
            await ctx.reply("**Usage:** `!givecard @user <card name>`\n**Example:** `!givecard @Someone Card 1`")

    # ── !giveallcards (owner only) ───────────────────────────────────────
    # Grants one copy of every card in CARD_DEFS at once — handy for testing
    # /cards, /carddex, /cardsets, and trading without grinding pulls first.
    @commands.command(name="giveallcards")
    @commands.is_owner()
    async def prefix_giveallcards(self, ctx: commands.Context, user: discord.User = None):
        """!giveallcards [@user] — owner only, grants one copy of every card. Defaults to yourself."""
        target = user or ctx.author
        for card_id in CARD_DEFS:
            add_card(target.id, card_id)
        await ctx.reply(f"✅ Gave **{target.display_name}** one copy of all **{len(CARD_DEFS)}** cards.")

    @prefix_giveallcards.error
    async def giveallcards_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.NotOwner):
            await ctx.reply("🚫 Only the bot owner can use this command.")
        elif isinstance(error, (commands.UserNotFound, commands.BadArgument)):
            await ctx.reply("**Usage:** `!giveallcards [@user]`\n**Example:** `!giveallcards` or `!giveallcards @Someone`")


async def setup(bot: commands.Bot):
    await bot.add_cog(Cards(bot))