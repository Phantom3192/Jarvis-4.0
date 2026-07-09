"""
/profile — a bot-rendered rank card (balance, streak, game records, favorite
song, level, badges) plus commands to equip earned/bought titles & banners.

Deliberately server-agnostic: nothing here ever creates, checks, or touches
a Discord role or permission, so it looks and behaves identically no matter
which of the bot's servers someone is in.
"""
import math

import discord
from discord.ext import commands
from discord import app_commands

from cogs.state import (
    get_credits, get_streak, get_game_stats, get_songs_played,
    get_favorite_song, get_stats, get_badges, get_titles, get_banners,
    equip_title, equip_banner,
)
from cogs.achievements import ACHIEVEMENTS
from cogs.economy import JC_EMOJI, JC_NAME, BANNER_COLORS, get_title_label

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


def _profile_embed(user: discord.User | discord.Member) -> discord.Embed:
    uid = user.id
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
    equipped_banner = get_banners(uid).get("equipped")
    color = discord.Color(BANNER_COLORS[equipped_banner][1]) if equipped_banner in BANNER_COLORS else discord.Color.blurple()

    display_name = f"{user.display_name}"
    if equipped_title:
        display_name = f"{display_name} — {equipped_title}"

    embed = discord.Embed(title=f"🪪 {display_name}", color=color)
    embed.set_thumbnail(url=user.display_avatar.url)

    embed.add_field(name=f"{JC_EMOJI} Balance", value=f"**{balance:,}** {JC_NAME}s", inline=True)
    embed.add_field(name="🔥 Streak", value=f"**{streak}** day{'s' if streak != 1 else ''}", inline=True)
    embed.add_field(
        name=f"📈 Level {level}",
        value=f"`{_progress_bar(into_level, per_level)}` {into_level}/{per_level}",
        inline=True,
    )

    embed.add_field(
        name="♟️ Chess",
        value=f"{stats['chess_wins']}W – {stats['chess_losses']}L",
        inline=True,
    )
    embed.add_field(
        name="🎭 Mafia",
        value=f"{stats['mafia_wins']}W – {stats['mafia_losses']}L",
        inline=True,
    )
    embed.add_field(
        name="🪢 Hangman",
        value=f"{stats['hangman_wins']} solved",
        inline=True,
    )

    embed.add_field(name="🎵 Songs Played", value=str(songs), inline=True)
    embed.add_field(name="🎧 Favorite Song", value=favorite or "—", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)  # spacer for a clean 3-column grid

    badge_ids = get_badges(uid)
    if badge_ids:
        badge_line = "  ".join(
            ACHIEVEMENTS[b]["emoji"] for b in badge_ids if b in ACHIEVEMENTS
        )
        embed.add_field(name=f"🏅 Badges ({len(badge_ids)})", value=badge_line, inline=False)
    else:
        embed.add_field(name="🏅 Badges", value="None yet — play some games or chat to unlock some!", inline=False)

    embed.set_footer(text="Use /titles and /banners to see what you own and can equip.")
    return embed


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
        label = BANNER_COLORS.get(b, (b, 0))[0]
        marker = "✅ (equipped)" if b == equipped else ""
        lines.append(f"{label} {marker}")
    embed.description = "\n".join(lines)
    embed.set_footer(text="Equip one with /banner <name>, or /banner none to unequip.")
    return embed


class Profile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── !profile / /profile ─────────────────────────────────────────────────

    @commands.command(name="profile", aliases=["rank", "rankcard"])
    async def prefix_profile(self, ctx: commands.Context, user: discord.User = None):
        """!profile — view your (or someone else's) rank card."""
        target = user or ctx.author
        await ctx.reply(embed=_profile_embed(target))

    @app_commands.command(name="profile", description="View your (or someone else's) Jarvis profile card")
    @app_commands.describe(user="User to look up (optional — leave empty for yourself)")
    async def slash_profile(self, interaction: discord.Interaction, user: discord.User = None):
        target = user or interaction.user
        await interaction.response.send_message(embed=_profile_embed(target))

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
        # Accept either the raw stored id (chess_master, title_vip) or its
        # display label (🏆 Chess Master, 💎 VIP) — matched case-insensitively
        # so a copy-pasted or retyped label always resolves correctly instead
        # of silently failing on an exact-character mismatch.
        match = None
        for owned_id in owned:
            label = get_title_label(owned_id)
            if name == owned_id or name.lower() == label.lower():
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
        # Accept either the raw item id (banner_gold) or its display label (🥇 Gold Banner)
        match = None
        for b in owned:
            label = BANNER_COLORS.get(b, (b, 0))[0]
            if name == b or name.lower() == label.lower():
                match = b
                break
        if match is None:
            return f"❌ You don't own a banner called **{name}**. Check `/banners` for your list."
        equip_banner(user.id, match)
        return f"✅ Equipped banner: {BANNER_COLORS.get(match, (match, 0))[0]}"


async def setup(bot: commands.Bot):
    await bot.add_cog(Profile(bot))