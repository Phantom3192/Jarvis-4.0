"""
/profile — a bot-rendered rank card (balance, streak, game records, favorite
song, level, badges) plus commands to equip earned/bought titles & banners.

Deliberately server-agnostic: nothing here ever creates, checks, or touches
a Discord role or permission, so it looks and behaves identically no matter
which of the bot's servers someone is in.
"""
import math
import time

import discord
from discord.ext import commands
from discord import app_commands

from cogs.state import (
    get_credits, get_streak, get_game_stats, get_songs_played,
    get_favorite_song, get_stats, get_badges, get_titles, get_banners,
    equip_title, equip_banner, PROFILE_FIELDS, get_hidden_fields,
    set_field_hidden, get_referral_count, get_lifetime_earned, get_ai_usage,
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


def _normalize_text(s: str) -> str:
    """Strip emoji/punctuation and collapse whitespace so item names can be
    matched regardless of whether the user included the leading emoji —
    '💎 VIP', 'VIP', and 'vip' all normalize to the same string. Used for
    both title and banner name matching so neither requires typing the
    emoji back."""
    return " ".join("".join(ch for ch in s if ch.isalnum() or ch.isspace()).lower().split())


def _format_duration(seconds: float) -> str:
    """Render a duration as a friendly 'N days' / 'N months' string for the
    account-age field. Kept coarse (days → months → years) since profile
    flex doesn't need hour/minute precision."""
    days = int(seconds // 86400)
    if days < 1:
        return "Less than a day"
    if days < 60:
        return f"{days} day{'s' if days != 1 else ''}"
    if days < 365:
        months = days // 30
        return f"{months} month{'s' if months != 1 else ''}"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''}"


def _profile_embed(user: discord.User | discord.Member, viewer: discord.User | discord.Member = None) -> discord.Embed:
    """Build the profile card for `user`. `viewer` is whoever is looking at
    it — defaults to `user` themself (e.g. internal calls, DMs). Fields the
    owner has hidden via /profile hide are skipped for everyone except the
    owner, so you always see your own full card regardless of your settings."""
    uid = user.id
    is_owner = viewer is None or viewer.id == uid
    hidden = set() if is_owner else set(get_hidden_fields(uid))

    balance = get_credits(uid)
    streak = get_streak(uid)
    message_stats = get_stats(uid) or {}
    messages = message_stats.get("messages", 0)
    level, into_level, per_level = _level_for_messages(messages)

    first_seen = message_stats.get("first_seen")

    equipped_title_id = get_titles(uid).get("equipped")
    equipped_title = get_title_label(equipped_title_id) if equipped_title_id else None
    equipped_banner = get_banners(uid).get("equipped")
    color = discord.Color(BANNER_COLORS[equipped_banner][1]) if equipped_banner in BANNER_COLORS else discord.Color.blurple()

    display_name = f"{user.display_name}"
    if equipped_title:
        display_name = f"{display_name} — {equipped_title}"

    embed = discord.Embed(title=f"🪪 {display_name}", color=color)
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

    if "joined" not in hidden:
        joined_value = f"<t:{int(first_seen)}:D>" if first_seen else "Not yet interacted"
        embed.add_field(name="📅 First Interaction", value=joined_value, inline=True)

    if "account_age" not in hidden:
        age_value = _format_duration(time.time() - first_seen) if first_seen else "Not yet interacted"
        embed.add_field(name="🧓 Account Age", value=age_value, inline=True)

    if "referrals" not in hidden:
        referral_count = get_referral_count(uid)
        embed.add_field(
            name="🎟️ Referred",
            value=f"**{referral_count}** friend{'s' if referral_count != 1 else ''}",
            inline=True,
        )

    # AI Messages + Lifetime Earned share one full-width field instead of
    # each being their own inline column — with an odd number of inline
    # fields above, two more inline fields here would leave a lone
    # half-empty row in Discord's 3-per-row grid. A merged full-width field
    # sidesteps that regardless of how many fields end up hidden.
    activity_lines = []
    if "ai_messages" not in hidden:
        today_count, _ = get_ai_usage(uid)
        lifetime_count = message_stats.get("messages", 0)
        activity_lines.append(f"🧠 **AI Messages:** {lifetime_count:,} total • {today_count} today")
    if "lifetime_earned" not in hidden:
        earned = get_lifetime_earned(uid)
        activity_lines.append(f"💰 **Lifetime {JC_NAME}s Earned:** {earned:,}")
    if activity_lines:
        embed.add_field(name="📊 Activity", value="\n".join(activity_lines), inline=False)

    if "badges" not in hidden:
        badge_ids = get_badges(uid)
        if badge_ids:
            badge_line = "  ".join(
                ACHIEVEMENTS[b]["emoji"] for b in badge_ids if b in ACHIEVEMENTS
            )
            embed.add_field(name=f"🏅 Badges ({len(badge_ids)})", value=badge_line, inline=False)
        else:
            embed.add_field(name="🏅 Badges", value="None yet — play some games or chat to unlock some!", inline=False)

    footer = None
    if is_owner and get_hidden_fields(uid):
        footer = "Some fields are hidden from other viewers. "
    if footer:
        embed.set_footer(text=footer)
    return embed


def _game_stats_embed(user: discord.User | discord.Member, viewer: discord.User | discord.Member = None) -> discord.Embed:
    """Build the standalone game-stats card for `user` (chess/mafia/hangman
    records, songs played, favorite song) — shown via the 🎮 Game Stats
    button on /profile instead of cluttering the main rank card. Respects
    the same per-field privacy settings as the main profile embed."""
    uid = user.id
    is_owner = viewer is None or viewer.id == uid
    hidden = set() if is_owner else set(get_hidden_fields(uid))

    stats = get_game_stats(uid)
    songs = get_songs_played(uid)
    favorite = get_favorite_song(uid)

    embed = discord.Embed(title=f"🎮 {user.display_name}'s Game Stats", color=discord.Color.blurple())
    embed.set_thumbnail(url=user.display_avatar.url)

    any_shown = False
    if "chess" not in hidden:
        embed.add_field(
            name="♟️ Chess",
            value=f"{stats['chess_wins']}W – {stats['chess_losses']}L",
            inline=True,
        )
        any_shown = True
    if "mafia" not in hidden:
        embed.add_field(
            name="🎭 Mafia",
            value=f"{stats['mafia_wins']}W – {stats['mafia_losses']}L",
            inline=True,
        )
        any_shown = True
    if "hangman" not in hidden:
        embed.add_field(
            name="🪢 Hangman",
            value=f"{stats['hangman_wins']} solved",
            inline=True,
        )
        any_shown = True
    if "songs" not in hidden:
        embed.add_field(name="🎵 Songs Played", value=str(songs), inline=True)
        any_shown = True
    if "favorite" not in hidden:
        embed.add_field(name="🎧 Favorite Song", value=favorite or "—", inline=True)
        any_shown = True

    if not any_shown:
        embed.description = "This user has hidden all of their game stats."

    return embed


class GameStatsButton(discord.ui.View):
    """Attached to /profile — lets the viewer pull up the profile owner's
    game stats (chess/mafia/hangman/songs/favorite) in a separate ephemeral
    card instead of always showing them on the main rank card."""

    def __init__(self, target: discord.User | discord.Member, *, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.target = target

    @discord.ui.button(label="Game Stats", style=discord.ButtonStyle.secondary, emoji="🎮")
    async def game_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = _game_stats_embed(self.target, viewer=interaction.user)
        await interaction.response.send_message(embed=embed, ephemeral=True)


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
    embed.set_footer(text="Equip one with /title <name>. Unequip your current one with /title none before switching.")
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
        await ctx.reply(embed=_profile_embed(target, viewer=ctx.author), view=GameStatsButton(target))

    @app_commands.command(name="profile", description="View your (or someone else's) Jarvis profile card")
    @app_commands.describe(user="User to look up (optional — leave empty for yourself)")
    async def slash_profile(self, interaction: discord.Interaction, user: discord.User = None):
        target = user or interaction.user
        await interaction.response.send_message(embed=_profile_embed(target, viewer=interaction.user), view=GameStatsButton(target))

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

        current = get_titles(user.id).get("equipped")
        if current == match:
            return f"**{get_title_label(match)}** is already equipped."
        if current:
            return (
                f"❌ You already have **{get_title_label(current)}** equipped. "
                "Unequip it first with `/title none` (or `/unequip title`) before equipping another."
            )

        equip_title(user.id, match)
        return f"✅ Equipped title: {get_title_label(match)}"

    # ── !unequip / /unequip ──────────────────────────────────────────────────
    # Equivalent to /title none or /banner none, but discoverable on its own —
    # users shouldn't have to know "none" is the magic unequip keyword.

    @commands.command(name="unequip")
    async def prefix_unequip(self, ctx: commands.Context, what: str = None):
        """!unequip title|banner|all — clear an equipped cosmetic."""
        msg = self._handle_unequip(ctx.author, what)
        await ctx.reply(msg)

    @app_commands.command(name="unequip", description="Unequip your current title and/or banner")
    @app_commands.describe(what="What to unequip")
    @app_commands.choices(what=[
        app_commands.Choice(name="Title", value="title"),
        app_commands.Choice(name="Banner", value="banner"),
        app_commands.Choice(name="Both", value="all"),
    ])
    async def slash_unequip(self, interaction: discord.Interaction, what: app_commands.Choice[str]):
        msg = self._handle_unequip(interaction.user, what.value)
        await interaction.response.send_message(msg, ephemeral=True)

    def _handle_unequip(self, user: discord.User, what: str | None) -> str:
        what = (what or "").strip().lower()
        if what not in ("title", "banner", "all"):
            return "**Usage:** `/unequip title`, `/unequip banner`, or `/unequip all`."
        did = []
        if what in ("title", "all") and get_titles(user.id).get("equipped"):
            equip_title(user.id, None)
            did.append("title")
        if what in ("banner", "all") and get_banners(user.id).get("equipped"):
            equip_banner(user.id, None)
            did.append("banner")
        if not did:
            return "Nothing to unequip — you don't have a title or banner equipped."
        return f"✅ Unequipped: {', '.join(did)}."

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
        # Accept the raw item id (banner_gold), the full display label
        # (🥇 Gold Banner), or the label with its leading emoji left off
        # (Gold Banner) — matched case-insensitively, same as /title, so
        # typing the plain name works without hunting down the emoji.
        target = _normalize_text(name)
        match = None
        for b in owned:
            label = BANNER_COLORS.get(b, (b, 0))[0]
            if name == b or target == _normalize_text(label):
                match = b
                break
        if match is None:
            return f"❌ You don't own a banner called **{name}**. Check `/banners` for your list."
        equip_banner(user.id, match)
        return f"✅ Equipped banner: {BANNER_COLORS.get(match, (match, 0))[0]}"


    # ── !profilehide / !profileshow / /profile-privacy ──────────────────────
    # Per-field visibility. Hiding a field only affects what OTHER people see
    # on /profile — the owner always sees their own full card.

    _PRIVACY_CHOICES = [
        app_commands.Choice(name=label, value=key) for key, label in PROFILE_FIELDS.items()
    ]

    @commands.command(name="profilehide")
    async def prefix_profile_hide(self, ctx: commands.Context, *, field: str = None):
        """!profilehide <field> — hide a stat from other people's view of your profile."""
        msg = self._handle_set_hidden(ctx.author, field, True)
        await ctx.reply(msg)

    @commands.command(name="profileshow")
    async def prefix_profile_show(self, ctx: commands.Context, *, field: str = None):
        """!profileshow <field> — make a previously hidden stat visible again."""
        msg = self._handle_set_hidden(ctx.author, field, False)
        await ctx.reply(msg)

    @commands.command(name="profilesettings", aliases=["profileprivacy"])
    async def prefix_profile_settings(self, ctx: commands.Context):
        """!profilesettings — see which fields are currently hidden from others."""
        await ctx.reply(self._privacy_status(ctx.author))

    @app_commands.command(name="profile-hide", description="Hide a stat from other people's view of your profile")
    @app_commands.describe(field="Which field to hide")
    @app_commands.choices(field=_PRIVACY_CHOICES)
    async def slash_profile_hide(self, interaction: discord.Interaction, field: app_commands.Choice[str]):
        msg = self._handle_set_hidden(interaction.user, field.value, True)
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="profile-show", description="Make a previously hidden profile stat visible again")
    @app_commands.describe(field="Which field to show")
    @app_commands.choices(field=_PRIVACY_CHOICES)
    async def slash_profile_show(self, interaction: discord.Interaction, field: app_commands.Choice[str]):
        msg = self._handle_set_hidden(interaction.user, field.value, False)
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="profile-settings", description="See which of your profile fields are hidden from others")
    async def slash_profile_settings(self, interaction: discord.Interaction):
        await interaction.response.send_message(self._privacy_status(interaction.user), ephemeral=True)

    def _handle_set_hidden(self, user: discord.User, field: str | None, hidden: bool) -> str:
        if not field:
            valid = ", ".join(PROFILE_FIELDS)
            return f"**Usage:** `/profile-hide <field>` or `/profile-show <field>`. Valid fields: {valid}"
        key = field.strip().lower()
        if key not in PROFILE_FIELDS:
            valid = ", ".join(PROFILE_FIELDS)
            return f"❌ Unknown field **{field}**. Valid fields: {valid}"
        set_field_hidden(user.id, key, hidden)
        verb = "hidden from" if hidden else "visible to"
        return f"✅ **{PROFILE_FIELDS[key]}** is now {verb} other people viewing your profile."

    def _privacy_status(self, user: discord.User) -> str:
        hidden = get_hidden_fields(user.id)
        if not hidden:
            return "Nothing is hidden — your whole profile is visible to everyone. Use `/profile-hide` to hide a field."
        labels = ", ".join(PROFILE_FIELDS.get(f, f) for f in hidden)
        return f"🙈 Hidden from other viewers: **{labels}**. Use `/profile-show` to unhide any of them."


async def setup(bot: commands.Bot):
    await bot.add_cog(Profile(bot))