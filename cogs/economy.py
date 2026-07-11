"""
Jarvis Credits (JC) — shared economy constants and UI components.

This module has no commands of its own. It's a toolbox other cogs import
from to:
  - award JC for things users do (chatting, daily check-in, onboarding,
    accepted contributions)
  - prompt users with a Yes/No "spend JC to avoid a penalty / get a perk"
    button view (SpendCreditsView), used by the counting game and the AI
    daily-limit flow.
"""
import time
from datetime import datetime, timezone
import discord
from discord.ext import tasks

from cogs.state import (
    spend_credits, bump_streak, get_streak, STREAK_MILESTONES,
    get_or_create_referral_code, redeem_referral_code, is_new_user, mark_seen,
    get_equipped_title, grant_title, get_titles, grant_banner, get_banners,
    get_all_subscriptions, get_subscriptions, set_subscription, clear_subscription, revoke_title,
    revoke_banner, equip_title, get_auto_renew, set_auto_renew, clear_auto_renew,
)
from cogs.achievements import TITLE_LABELS

JC_NAME  = "Jarvis Credit"
JC_EMOJI = "🪙"

# ── Earning amounts ────────────────────────────────────────────────────────
DAILY_CHECKIN_REWARD   = 50   # granted once per UTC day, on a user's first message
ONBOARDING_BONUS       = 50   # one-time, granted to brand-new users
AI_CHAT_REWARD         = 5    # JC per AI reply
AI_CHAT_REWARD_DAILY_CAP = 100 # max JC/day earnable just from chatting
CONTRIBUTION_REWARD    = 50   # granted when a suggestion/bug report is accepted

# ── Spending costs ─────────────────────────────────────────────────────────
COUNT_SAVE_COST      = 25   # flat cost to save a counting-game streak
AI_LIMIT_RESET_COST  = 50   # cost to fully reset today's AI usage counter
EXTRA_HINT_COST      = 15   # cost for an additional chess hint beyond the free one per turn

# ── Streaks ─────────────────────────────────────────────────────────────────
# Milestone JC payouts live in cogs/state.py (STREAK_MILESTONES) to avoid a
# circular import — bump_streak() needs to read it without importing this
# module. Re-exported here so other cogs/UI code can import it from economy.
STREAK_MILESTONE_LABELS = {
    7:  "🔥 7-day streak bonus",
    30: "⭐ Monthly loyal user",
}

# ── Referrals ────────────────────────────────────────────────────────────────
REFERRER_BONUS  = 50   # JC paid to whoever's code was redeemed
REFERRED_BONUS  = 0    # JC paid to the new user who redeemed a code (none — they still
                        # get the standard ONBOARDING_BONUS separately, just no extra on top)
# (separate from, and stacks with, ONBOARDING_BONUS — the new user still
# gets both the normal onboarding bonus AND the referral bonus)

# ── JC Shop ──────────────────────────────────────────────────────────────────
MYSTERY_BOX_COST     = 200  # JC to open a Mystery Box
MYSTERY_BOX_MIN      = 100   # min payout
MYSTERY_BOX_MAX      = 300  # max payout — equal chance across the whole range

# Higher-tier Mystery Box: pricier, better base payouts, and the only box
# that VIP/Elite's mystery_box_multiplier perk applies to. The plain Mystery
# Box above always pays out its normal range, no title bonus involved.
DELUXE_MYSTERY_BOX_COST = 1000  # JC to open a Deluxe Mystery Box
DELUXE_MYSTERY_BOX_MIN  = 100   # min payout
DELUXE_MYSTERY_BOX_MAX  = 2000  # max payout — equal chance across the whole range

# Profile banner colors — shown as the /profile embed color once equipped.
# Keyed by the same id used in SHOP_ITEMS/grant_banner/equip_banner.
BANNER_COLORS: dict[str, tuple[str, int]] = {
    "banner_gold":     ("🥇 Gold Banner",     0xF1C40F),
    "banner_royal":    ("👑 Royal Banner",    0x9B59B6),
    "banner_midnight": ("🌌 Midnight Banner", 0x2C3E50),
    "banner_emerald":  ("🟢 Emerald Banner",  0x2ECC71),
    # Exclusive to VIP/Elite subscribers — auto-granted in _purchase_item on
    # subscribe, deliberately NOT in SHOP_ITEMS so it can't be bought directly.
    "banner_prestige": ("💠 Prestige Banner", 0x00CFFF),
}

# Weekly billing period for subscription-style shop items.
SUBSCRIPTION_PERIOD_SECONDS = 7 * 24 * 3600


def _until(date_str: str) -> float:
    """Parse a fixed 'YYYY-MM-DD' UTC date into an epoch timestamp, for use
    as a shop item's `available_until`. Deliberately a FIXED calendar date,
    not `time.time() + N days` — the latter would silently push the window
    back every time the bot restarts, so a "2-week" item could stay in the
    shop forever. Interpreted as end-of-day UTC on that date."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=timezone.utc
    )
    return dt.timestamp()


def _item_available(item: dict) -> bool:
    """True unless the item has an `available_until` that's already passed.
    Items with no `available_until` key are always available."""
    until = item.get("available_until")
    return until is None or time.time() < until


def _availability_note(item: dict) -> str:
    """A short '⏳ Available until ...' line for shop embeds, or '' for
    items with no expiry."""
    until = item.get("available_until")
    if until is None:
        return ""
    date = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%b %d, %Y")
    return f"\n⏳ **Limited time** — available until {date} UTC"


def _format_time_left(seconds: float) -> str:
    """'3d 4h', '5h 12m', or '<1m' style compact countdown string."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "<1m"


def _subscription_note(item_id: str, item: dict, user_id: int | None) -> str:
    """A '🔄 Renews in ...' / '⚠️ Renewal failing' line for shop embeds,
    shown only for `subscription_title` items the given user currently
    owns. Deliberately keyed off `item["kind"]` rather than a specific
    item id, so it applies uniformly to VIP, Elite, or any subscription
    title added later — not just the first one that happened to get it."""
    if item.get("kind") != "subscription_title" or user_id is None:
        return ""
    next_charge = get_subscriptions(user_id).get(item_id)
    if next_charge is None:
        return ""  # user doesn't currently own/subscribe to this title
    remaining = next_charge - time.time()
    if remaining <= 0:
        # Billing task hasn't swept yet, but the renewal is due/overdue.
        return "\n🔄 **Renews any moment now**"
    return f"\n🔄 **Renews in {_format_time_left(remaining)}** (next charge: {item['price']:,} {JC_EMOJI})"

# Gameplay perks tied to an EQUIPPED title id. Only applies while the title
# is actually equipped (not just owned) — gives equipping a premium title
# an actual reason beyond cosmetics.
TITLE_PERKS: dict[str, dict] = {
    "title_vip": {
        "chat_jc_multiplier": 1.25,       # +25% JC from AI chat
        "mystery_box_multiplier": 1.15,   # +15% bigger Mystery Box payouts
        "save_cost_multiplier": 0.5,      # 50% off counting-game streak saves
        "referral_bonus_multiplier": 1.25, # +25% JC per successful referral
        "weekly_loyalty_bonus": 100,      # free JC every week the sub renews
        "leaderboard_flair": "\u001b[1;36m",  # bold cyan name on !leaderboard
        "reset_cost_multiplier": 0.75,    # 25% off AI daily-limit resets
        "hint_cost_multiplier": 0.5,      # 50% off extra chess hints
        "daily_bonus_multiplier": 1.25,   # +25% JC from the daily check-in bonus
        "daily_ai_limit_bonus": 50,       # +50 AI messages/day (100 -> 150)
        "cooldown_multiplier": 0.5,       # half the normal command cooldown
    },
    "title_elite": {
        "chat_jc_multiplier": 1.5,        # +50% JC from AI chat
        "free_hints": True,               # unlimited free chess hints
        "mystery_box_multiplier": 1.3,    # +30% bigger Mystery Box payouts
        "save_cost_multiplier": 0.0,      # free counting-game streak saves
        "reset_cost_multiplier": 0.5,     # 50% off AI daily-limit resets
        "referral_bonus_multiplier": 1.5, # +50% JC per successful referral
        "weekly_loyalty_bonus": 250,      # free JC every week the sub renews
        "leaderboard_flair": "\u001b[1;33m",  # bold gold name on !leaderboard
        "daily_bonus_multiplier": 1.5,    # +50% JC from the daily check-in bonus
        "daily_ai_limit_bonus": 150,      # +150 AI messages/day (100 -> 250)
        "cooldown_multiplier": 0.2,       # nearly instant command cooldown
    },
}

# Human-readable descriptions for each perk key, used to build the perk
# breakdown shown in the shop's "View Perks" button and in item descriptions.
# Each value is a callable that takes the perk's value and returns a display
# string (or None to skip it, e.g. a falsy free_hints flag).
_PERK_LABELS = {
    "chat_jc_multiplier":     lambda v: f"+{round((v - 1) * 100)}% {JC_NAME}s earned from chatting with Jarvis",
    "mystery_box_multiplier": lambda v: f"+{round((v - 1) * 100)}% bigger Deluxe Mystery Box payouts",
    "free_hints":             lambda v: "Unlimited free chess hints" if v else None,
    "save_cost_multiplier":   lambda v: (
        "Free counting-game streak saves" if v <= 0
        else f"{round((1 - v) * 100)}% off counting-game streak saves"
    ),
    "reset_cost_multiplier":  lambda v: f"{round((1 - v) * 100)}% off AI daily-limit resets",
    "referral_bonus_multiplier": lambda v: f"+{round((v - 1) * 100)}% {JC_NAME}s per successful referral",
    "weekly_loyalty_bonus":   lambda v: f"+{v} {JC_NAME}s free every week your subscription renews",
    "leaderboard_flair":      lambda v: "✨ Highlighted name on !leaderboard" if v else None,
    "hint_cost_multiplier":   lambda v: (
        "Free extra chess hints" if v <= 0
        else f"{round((1 - v) * 100)}% off extra chess hints"
    ),
    "daily_bonus_multiplier": lambda v: f"+{round((v - 1) * 100)}% {JC_NAME}s from your daily check-in bonus",
    "daily_ai_limit_bonus":   lambda v: f"+{v} AI messages per day",
    "cooldown_multiplier":    lambda v: f"{round((1 - v) * 100)}% shorter command cooldown",
}


def describe_perks(title_id: str) -> list[str]:
    """Return a list of human-readable perk bullet strings for `title_id`,
    or an empty list if it grants no gameplay perks."""
    lines = []
    for key, value in TITLE_PERKS.get(title_id, {}).items():
        label_fn = _PERK_LABELS.get(key)
        if not label_fn:
            continue
        label = label_fn(value)
        if label:
            lines.append(f"• {label}")
    return lines


def get_active_perks(user_id: int) -> dict:
    """Return the perk dict for whatever title the user currently has
    equipped, or {} if none/not a perk-granting title."""
    equipped = get_equipped_title(user_id)
    return TITLE_PERKS.get(equipped, {})


# Labels for titles that used to be purchasable in SHOP_ITEMS but have since
# been pulled (event ended, retired, etc). Anyone who already owns one keeps
# it — this just keeps it displaying nicely on /profile, /titles, and the
# leaderboard instead of falling back to the raw item id. Move an entry here
# whenever you remove something from SHOP_ITEMS that people may already own.
_RETIRED_TITLE_LABELS: dict[str, str] = {
    "title_founder": "🎗️ Founder",
    "title_legend": "🔥 Legend",
}


def get_title_label(title_id: str) -> str:
    """Resolve a stored title id back to its display label — checks shop
    items first, then achievement-granted titles, then retired shop titles.
    Falls back to the raw id if somehow none of those have it (shouldn't
    normally happen)."""
    if title_id in SHOP_ITEMS:
        return SHOP_ITEMS[title_id]["name"]
    if title_id in TITLE_LABELS:
        return TITLE_LABELS[title_id]
    return _RETIRED_TITLE_LABELS.get(title_id, title_id)


# Shop catalog. Keys are stable item ids (used by !shop buy <id>).
# Add more items here later — each needs name/price/description/kind/announce.
SHOP_ITEMS: dict[str, dict] = {
    "mystery_box": {
        "name": "🎰 Mystery Box",
        "price": MYSTERY_BOX_COST,
        "description": f"Random reward: anywhere from {MYSTERY_BOX_MIN}–{MYSTERY_BOX_MAX} JC, equal chance.",
        "kind": "mystery_box",
        "announce": True,
    },
    "mystery_box_deluxe": {
        "name": "🎆 Deluxe Mystery Box",
        "price": DELUXE_MYSTERY_BOX_COST,
        "description": (
            f"A pricier box with better odds: anywhere from {DELUXE_MYSTERY_BOX_MIN}–{DELUXE_MYSTERY_BOX_MAX} JC. "
            "VIP/Elite titles boost these payouts even further — see **🔍 View Perks**."
        ),
        "kind": "mystery_box_deluxe",
        "announce": True,
    },
    "title_vip": {
        "name": "💎 VIP",
        "price": 2000,
        "description": (
            "Billed **2,000 JC/week**. An equippable VIP title with gameplay perks — "
            "tap **🔍 View Perks** below to see what it grants."
        ),
        "kind": "subscription_title",
        "announce": True,
    },
    "title_elite": {
        "name": "🌟 Elite",
        "price": 3500,
        "description": (
            "Billed **3,500 JC/week**. An equippable Elite title with gameplay perks — "
            "tap **🔍 View Perks** below to see what it grants."
        ),
        "kind": "subscription_title",
        "announce": True,
    },
    "banner_gold": {
        "name": "🥇 Gold Banner",
        "price": 300,
        "description": "Colors your /profile card gold.",
        "kind": "banner",
        "announce": False,
    },
    "banner_royal": {
        "name": "👑 Royal Banner",
        "price": 300,
        "description": "Colors your /profile card royal purple.",
        "kind": "banner",
        "announce": False,
    },
    "banner_midnight": {
        "name": "🌌 Midnight Banner",
        "price": 300,
        "description": "Colors your /profile card midnight blue.",
        "kind": "banner",
        "announce": False,
    },
    "banner_emerald": {
        "name": "🟢 Emerald Banner",
        "price": 300,
        "description": "Colors your /profile card emerald green.",
        "kind": "banner",
        "announce": False,
    },
}
SHOP_PAGE_SIZE = 3


class SpendCreditsView(discord.ui.View):
    """
    Generic "Yes / No — spend JC?" button prompt.

    on_confirm(interaction, view)
        Called after JC was successfully deducted. Should edit the message
        to reflect the new state (e.g. "streak saved!").

    on_decline(interaction, view, reason)
        Called when the user presses "No", or pressed "Yes" without enough
        JC. `reason` is "declined" or "insufficient". Should edit the
        message to apply the default penalty (e.g. reset the count).

    on_timeout_action()
        Called (no interaction available) if the user never responds.
        Should apply the default penalty. The view's buttons are already
        disabled on the original message by the time this runs.
    """

    def __init__(
        self,
        user_id: int,
        cost: int,
        on_confirm,
        on_decline=None,
        on_timeout_action=None,
        *,
        timeout: float = 30,
    ):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.cost = cost
        self.on_confirm = on_confirm
        self.on_decline = on_decline
        self.on_timeout_action = on_timeout_action
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This prompt isn't for you!", ephemeral=True
            )
            return False
        return True

    def _disable(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success, emoji="✅")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable()
        if not spend_credits(self.user_id, self.cost):
            if self.on_decline:
                await self.on_decline(interaction, self, "insufficient")
            else:
                await interaction.response.edit_message(view=self)
            self.stop()
            return
        await self.on_confirm(interaction, self)
        self.stop()

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary, emoji="❌")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable()
        if self.on_decline:
            await self.on_decline(interaction, self, "declined")
        else:
            await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self) -> None:
        self._disable()
        if self.on_timeout_action:
            await self.on_timeout_action(self)
        elif self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def balance_line(user_id: int) -> str:
    """Small helper for embedding a balance into messages."""
    from cogs.state import get_credits
    return f"{JC_EMOJI} **{get_credits(user_id)}** {JC_NAME}s"


# ── Balance command ─────────────────────────────────────────────────────────

import random
from discord.ext import commands
from discord import app_commands
from cogs.state import get_credits, add_credits, get_all_credits, grant_onboarding_bonus


def _balance_embed(user: discord.User | discord.Member) -> discord.Embed:
    embed = discord.Embed(
        title=f"{JC_EMOJI} Jarvis Credit Balance",
        description=f"**{user.display_name}** has **{get_credits(user.id)}** {JC_NAME}s.",
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Earn JC by chatting, daily check-ins, and as a new-user bonus.")
    return embed


def daily_bonus_embed(user: discord.User | discord.Member, amount: int) -> discord.Embed:
    """Compact embed announcing a user's daily JC check-in bonus."""
    embed = discord.Embed(
        description=f"{JC_EMOJI} **{user.display_name}** claimed their daily check-in bonus: **+{amount} {JC_NAME}**!",
        color=discord.Color.gold(),
    )
    return embed


def _streak_embed(user: discord.User | discord.Member, streak: int) -> discord.Embed:
    """Build the !streak / /streak status embed."""
    next_goal = next((m for m in sorted(STREAK_MILESTONES) if m > streak), None)
    embed = discord.Embed(
        title="🔥 Daily Streak",
        description=f"**{user.display_name}** has chatted **{streak}** day{'s' if streak != 1 else ''} in a row.",
        color=discord.Color.orange(),
    )
    progress_lines = []
    for length in sorted(STREAK_MILESTONES):
        reward = STREAK_MILESTONES[length]
        label = STREAK_MILESTONE_LABELS.get(length, f"{length}-day streak")
        check = "✅" if streak >= length else "▫️"
        progress_lines.append(f"{check} {label} — **+{reward} JC** at {length} days")
    embed.add_field(name="Milestones", value="\n".join(progress_lines), inline=False)
    if next_goal:
        remaining = next_goal - streak
        embed.set_footer(text=f"{remaining} more day{'s' if remaining != 1 else ''} to your next bonus. Send a message daily to keep it alive!")
    else:
        embed.set_footer(text="All milestones reached — keep the streak going for bragging rights! 🏆")
    return embed


def streak_milestone_announcement(user: discord.User | discord.Member, length: int, reward: int) -> str:
    """Public chat message for a newly-hit streak milestone."""
    label = STREAK_MILESTONE_LABELS.get(length, f"{length}-day streak")
    return f"{label}: **{user.display_name}** kept it going for **{length}** days — **+{reward} {JC_NAME}**! {JC_EMOJI}"


def mystery_box_result_embed(
    user: discord.User | discord.Member,
    reward: int,
    new_balance: int,
    *,
    box_name: str = "Mystery Box",
    box_emoji: str = "🎰",
    jackpot_threshold: int = 250,
    nice_threshold: int = 100,
) -> discord.Embed:
    """Result embed shown after opening a Mystery Box (or a higher shop tier
    of it, like the Deluxe Mystery Box, via the box_name/threshold kwargs)."""
    if reward >= jackpot_threshold:
        flavor = "🤯 JACKPOT!"
        color = discord.Color.gold()
    elif reward >= nice_threshold:
        flavor = "🎉 Nice pull!"
        color = discord.Color.green()
    else:
        flavor = "📦 Not bad."
        color = discord.Color.blurple()
    embed = discord.Embed(
        title=f"{box_emoji} {box_name}",
        description=(
            f"{flavor} **{user.display_name}** opened a {box_name} and got "
            f"**+{reward} {JC_NAME}**!\n\nNew balance: **{new_balance}** {JC_EMOJI}"
        ),
        color=color,
    )
    return embed


def _invite_embed(user: discord.User | discord.Member, code: str) -> discord.Embed:
    """!invite / /invite — show a user their referral code."""
    embed = discord.Embed(
        title="🎟️ Your Referral Code",
        description=(
            f"**{user.display_name}**'s referral code:\n"
            f"## `{code}`\n\n"
            f"Share it with a friend who's never used Jarvis before. "
            f"The **first thing** they need to do is run:\n"
            f"`!redeem {code}` or `/redeem {code}`\n\n"
            f"If they chat with Jarvis first and redeem afterwards, it **won't count** — "
            f"redeeming has to be their very first interaction."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="You get",
        value=(
            f"**+{REFERRER_BONUS} {JC_NAME}** per successful referral "
            f"(more with an equipped VIP/Elite title — see `!titleperks`)"
        ),
        inline=True,
    )
    # embed.add_field(name="They get", value="Their usual new-user bonus, no extra on top", inline=True)
    return embed


def referral_success_announcement(referred_user: discord.User | discord.Member, referrer_id: int, bonus: int) -> str:
    """Public chat message announcing a successful referral redemption."""
    return (
        f"🎟️ **{referred_user.display_name}** joined Jarvis via referral! "
        f"<@{referrer_id}> earned **+{bonus} {JC_NAME}**! {JC_EMOJI}"
    )


_REDEEM_FAILURE_MESSAGES = {
    "invalid_code": "❌ That referral code doesn't exist. Double-check it and try again.",
    "self_referral": "❌ You can't redeem your own referral code.",
    "already_seen": (
        "❌ Referral codes only count as your **first ever** interaction with Jarvis. "
        "Since you've already chatted with Jarvis before, this redemption can't be counted."
    ),
    "already_referred": "❌ You've already redeemed a referral code before — it only works once per person.",
}


LEADERBOARD_SIZE = 10
_MEDALS = ["🥇", "🥈", "🥉"]
_LB_NAME_WIDTH = 24  # longer display names get truncated so the layout stays tidy
_LB_BAR_LENGTH = 8


def _lb_bar(value: int, max_value: int, length: int = _LB_BAR_LENGTH) -> str:
    filled = round((value / max_value) * length) if max_value else 0
    return "▰" * filled + "▱" * (length - filled)


async def _leaderboard_embed(bot: commands.Bot) -> discord.Embed:
    """Build an embed showing the top JC holders across the whole bot.

    Top 3 get a "podium" treatment (medal, bold name, a relative balance
    bar) so they stand out at a glance; ranks 4-10 are a compact list
    underneath. No monospace/ANSI code-block — those escape codes don't
    render as color on every client, so keeping the layout to plain
    embed formatting looks consistent everywhere.
    """
    balances = get_all_credits()
    ranked = sorted(
        ((uid, bal) for uid, bal in balances.items() if bal > 0),
        key=lambda kv: kv[1],
        reverse=True,
    )[:LEADERBOARD_SIZE]

    embed = discord.Embed(title="🏆 Jarvis Credit Leaderboard", color=discord.Color.gold())

    if not ranked:
        embed.description = "Nobody has earned any Jarvis Credits yet!"
        return embed

    max_bal = ranked[0][1]
    resolved: list[tuple[str, int, discord.User | None]] = []
    for uid, bal in ranked:
        user = bot.get_user(int(uid))
        if user is None:
            try:
                user = await bot.fetch_user(int(uid))
            except discord.HTTPException:
                user = None
        resolved.append((uid, bal, user))

    def _display(uid: str, bal: int, user: discord.User | None) -> tuple[str, str]:
        name = user.display_name if user else f"User {uid}"
        if len(name) > _LB_NAME_WIDTH:
            name = name[: _LB_NAME_WIDTH - 1] + "…"
        title_id = get_equipped_title(int(uid))
        title_suffix = f" · {get_title_label(title_id)}" if title_id else ""
        # A perk-granting title gets a small sparkle instead of the old
        # ANSI color codes, which don't render as color on every client.
        flair = "✨ " if title_id and TITLE_PERKS.get(title_id, {}).get("leaderboard_flair") else ""
        return f"{flair}**{name}**{title_suffix}", bal

    podium_lines = []
    for i in range(min(3, len(resolved))):
        uid, bal, user = resolved[i]
        name_display, bal = _display(uid, bal, user)
        bar = _lb_bar(bal, max_bal)
        podium_lines.append(f"{_MEDALS[i]} {name_display}\n`{bar}` **{bal:,}** {JC_EMOJI}")

    embed.description = "\n\n".join(podium_lines)

    if len(resolved) > 3:
        rest_lines = []
        for i in range(3, len(resolved)):
            uid, bal, user = resolved[i]
            name_display, bal = _display(uid, bal, user)
            rest_lines.append(f"`#{i + 1:>2}` {name_display} — **{bal:,}** {JC_EMOJI}")
        embed.add_field(name=f"Ranks 4–{len(resolved)}", value="\n".join(rest_lines), inline=False)

    top_user = resolved[0][2]
    if top_user:
        embed.set_thumbnail(url=top_user.display_avatar.url)

    embed.set_footer(text=f"Balances shown in {JC_NAME}s • Top {len(resolved)} of all Jarvis users")
    return embed


# ── JC Shop ──────────────────────────────────────────────────────────────────

def _main_shop_items() -> list[tuple[str, dict]]:
    """Every shop item except banners — banners live in their own sub-menu
    (see _banner_shop_items / BannerShopView). Excludes limited-time items
    whose available_until has passed."""
    return [
        (iid, item) for iid, item in SHOP_ITEMS.items()
        if item["kind"] != "banner" and _item_available(item)
    ]


def _banner_shop_items() -> list[tuple[str, dict]]:
    return [
        (iid, item) for iid, item in SHOP_ITEMS.items()
        if item["kind"] == "banner" and _item_available(item)
    ]


def _shop_page_embed(page: int, user_id: int | None = None) -> discord.Embed:
    """Build one page of the paginated shop embed. `page` is 0-indexed.
    Banners are excluded here — they have their own sub-menu, opened via
    the 🎨 Banners button. When `user_id` is given, subscription titles
    the user already owns (VIP, Elite, or any future subscription_title
    item) show a live 'Renews in ...' countdown."""
    items = _main_shop_items()
    total_pages = max(1, (len(items) + SHOP_PAGE_SIZE - 1) // SHOP_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * SHOP_PAGE_SIZE
    chunk = items[start:start + SHOP_PAGE_SIZE]

    embed = discord.Embed(
        title=f"{JC_EMOJI} Jarvis Credit Shop",
        description="Spend your JC on perks, boosts, and a little gambling. Buy with the dropdown below.",
        color=discord.Color.gold(),
    )
    for item_id, item in chunk:
        embed.add_field(
            name=f"{item['name']} — {item['price']} {JC_EMOJI}",
            value=(
                f"{item['description']}"
                f"{_availability_note(item)}"
                f"{_subscription_note(item_id, item, user_id)}"
                f"\n`!shop buy {item_id}`"
            ),
            inline=False,
        )
    embed.set_footer(text=f"Page {page + 1}/{total_pages} • Buy with !shop buy <item_id> or the dropdown below")
    return embed


def _banner_shop_embed() -> discord.Embed:
    """Standalone banner sub-menu, opened via the shop's 🎨 Banners button."""
    embed = discord.Embed(
        title="🎨 Profile Banners",
        description="Cosmetic colors for your /profile card. Buy with the dropdown below.",
        color=discord.Color.purple(),
    )
    for item_id, item in _banner_shop_items():
        embed.add_field(
            name=f"{item['name']} — {item['price']} {JC_EMOJI}",
            value=f"{item['description']}{_availability_note(item)}\n`!shop buy {item_id}`",
            inline=False,
        )
    embed.set_footer(text="Buy with !shop buy <item_id> or the dropdown below • ◀ Back to Shop returns you here")
    return embed


def _title_perks_embed() -> discord.Embed:
    """Standalone embed listing every purchasable title and the perks it
    grants while equipped — shown via the shop's "View Perks" button and
    the !titleperks / /titleperks commands."""
    embed = discord.Embed(
        title="🔍 Title Perks",
        description=(
            "Perks only apply while a title is **equipped** — see `!titles` and "
            "`!title <name>` to check or switch."
        ),
        color=discord.Color.blurple(),
    )
    for item_id, item in SHOP_ITEMS.items():
        if item["kind"] not in ("title", "subscription_title"):
            continue
        lines = describe_perks(item_id)
        value = "\n".join(lines) if lines else "Cosmetic only — no gameplay perks."
        embed.add_field(name=item["name"], value=value, inline=False)
    embed.set_footer(text="Buy a title in the shop, then equip it with !title <name> to activate its perks.")
    return embed


async def _purchase_item(
    user: discord.User | discord.Member,
    item_id: str,
    channel: discord.abc.Messageable | None = None,
    *,
    auto_renew: bool = True,
) -> tuple[bool, str | None]:
    """
    Attempt to purchase `item_id` for `user`. Returns (success, fallback_message).

    `auto_renew` only matters for `subscription_title` items — whether the
    weekly billing should keep renewing automatically once this period ends,
    or let it lapse gracefully on its own. Ignored for every other kind.

    If the item is meant to announce publicly and `channel` is provided, the
    public embed/message is sent directly to `channel` and fallback_message
    is None (caller shouldn't send anything else — avoids double-posting).
    If there's no channel, or the item doesn't announce, fallback_message
    carries the result text the caller should reply with instead.
    On failure, fallback_message always carries the error to show the user.
    """
    item = SHOP_ITEMS.get(item_id)
    if item is None:
        return False, f"❌ Unknown item `{item_id}`. Use `!shop` to see what's available."

    if not _item_available(item):
        return False, f"❌ **{item['name']}** was a limited-time item and is no longer available."

    kind = item["kind"]

    # Cosmetics are one-time (or already-subscribed) purchases — don't let
    # someone spend JC twice on the same title/banner they already own.
    if kind in ("title", "subscription_title") and item_id in get_titles(user.id)["owned"]:
        return False, f"❌ You already own **{item['name']}**."
    if kind == "banner" and item_id in get_banners(user.id)["owned"]:
        return False, f"❌ You already own **{item['name']}**."

    if not spend_credits(user.id, item["price"]):
        bal = get_credits(user.id)
        return False, (
            f"❌ Insufficient JC for **{item['name']}** (costs **{item['price']}**). "
            f"You have **{bal}** {JC_EMOJI}."
        )

    fallback_msg = f"✅ **{user.display_name}** bought **{item['name']}**!"
    public_msg = None

    if kind == "mystery_box":
        reward = random.randint(MYSTERY_BOX_MIN, MYSTERY_BOX_MAX)
        new_balance = add_credits(user.id, reward)
        fallback_msg = f"🎰 You opened the Mystery Box and won **+{reward} {JC_NAME}**! New balance: **{new_balance}** {JC_EMOJI}"
        public_msg = mystery_box_result_embed(user, reward, new_balance)

    elif kind == "mystery_box_deluxe":
        reward = random.randint(DELUXE_MYSTERY_BOX_MIN, DELUXE_MYSTERY_BOX_MAX)
        perks = get_active_perks(user.id)
        reward = round(reward * perks.get("mystery_box_multiplier", 1.0))
        new_balance = add_credits(user.id, reward)
        fallback_msg = f"🎆 You opened the Deluxe Mystery Box and won **+{reward} {JC_NAME}**! New balance: **{new_balance}** {JC_EMOJI}"
        public_msg = mystery_box_result_embed(
            user, reward, new_balance,
            box_name="Deluxe Mystery Box", box_emoji="🎆",
            jackpot_threshold=500, nice_threshold=350,
        )

    elif kind == "title":
        grant_title(user.id, item_id)
        fallback_msg = (
            f"✅ **{user.display_name}** bought the **{item['name']}** title! "
            f"Equip it with `/title {item['name']}`."
        )
        public_msg = f"🛍️ **{user.display_name}** just bought the **{item['name']}** title!"

    elif kind == "subscription_title":
        grant_title(user.id, item_id)
        grant_banner(user.id, "banner_prestige")  # exclusive VIP/Elite perk, not shop-buyable
        set_subscription(user.id, item_id, time.time() + SUBSCRIPTION_PERIOD_SECONDS)
        set_auto_renew(user.id, item_id, auto_renew)
        previously_equipped = get_equipped_title(user.id)
        equip_title(user.id, item_id)  # subscribing activates it immediately — no separate equip step
        swap_note = (
            f" (swapped out of **{get_title_label(previously_equipped)}**)"
            if previously_equipped and previously_equipped != item_id else ""
        )
        renew_note = (
            f"🔁 Auto-renew is **ON** — it'll keep billing **{item['price']:,} {JC_NAME}s/week** "
            f"until you turn it off with `!autorenew {item_id.split('_')[1]} off`."
            if auto_renew else
            f"🔂 Auto-renew is **OFF** — this runs for exactly one week and then ends on its own, "
            f"no further charges. Turn it back on anytime with `!autorenew {item_id.split('_')[1]} on`."
        )
        fallback_msg = (
            f"✅ **{user.display_name}** subscribed to **{item['name']}** and it's now active{swap_note}! "
            f"First payment of **{item['price']:,} {JC_NAME}s** taken now, perks are live immediately. "
            f"You've also unlocked the exclusive 💠 **Prestige Banner** — equip it with `/banner Prestige`. "
            f"{renew_note} "
            f"Heads up: perks only apply while **{item['name']}** stays equipped — switching to another "
            f"title with `/title` turns them off again."
        )
        public_msg = f"🛍️ **{user.display_name}** just subscribed to **{item['name']}**!"

    elif kind == "banner":
        grant_banner(user.id, item_id)
        fallback_msg = (
            f"✅ **{user.display_name}** bought the **{item['name']}**! "
            f"Equip it with `/banner {item['name']}`."
        )

    if channel is not None and item.get("announce") and public_msg is not None:
        try:
            if isinstance(public_msg, discord.Embed):
                await channel.send(embed=public_msg)
            else:
                await channel.send(public_msg)
            return True, None  # already posted publicly — caller shouldn't double-send
        except discord.HTTPException:
            pass  # fall through and use fallback_msg instead

    return True, fallback_msg


async def _handle_redeem(
    user: discord.User | discord.Member,
    code: str,
    channel: discord.abc.Messageable | None,
) -> str:
    """
    Shared logic for !redeem and /redeem. Returns the message to show the user.

    This is deliberately the ONLY place that grants the onboarding bonus for
    users arriving via a `!`/`/` command as their first interaction — the
    AI cog's normal on_message onboarding path never runs for messages that
    start with "!" (it returns early), and slash commands each handle their
    own onboarding independently. So a brand-new user whose first-ever
    action is `!redeem CODE` must still get their normal onboarding bonus
    from right here, or they'd get it nowhere else. The referral itself only
    pays the referrer (REFERRED_BONUS is 0 by design) — redeeming is what
    gets the new user *counted*, not an extra payout for them.
    """
    if not code or not code.strip():
        return "**Usage:** `!redeem <code>` — get a code from a friend's `!invite`."

    # Snapshot "new" status before redeem_referral_code, since mark_seen
    # (called below on success) would otherwise make this check moot.
    was_new = is_new_user(user.id)

    success, reason, referrer_id = redeem_referral_code(user.id, code)

    if not success:
        return _REDEEM_FAILURE_MESSAGES.get(reason, "❌ Couldn't redeem that code.")

    # Mark seen now — redemption is this user's first counted interaction.
    mark_seen(user.id)

    # Onboarding bonus still applies normally (this is their first interaction
    # and the usual on_message path never ran for a "!"/"/" command).
    if was_new:
        grant_onboarding_bonus(user.id, ONBOARDING_BONUS)
    if REFERRED_BONUS:
        add_credits(user.id, REFERRED_BONUS)
    referrer_bonus = round(REFERRER_BONUS * get_active_perks(referrer_id).get("referral_bonus_multiplier", 1.0))
    add_credits(referrer_id, referrer_bonus)
    new_user_balance = get_credits(user.id)

    if channel is not None:
        try:
            await channel.send(referral_success_announcement(user, referrer_id, referrer_bonus))
        except discord.HTTPException:
            pass

    return (
        f"✅ Referral redeemed! Your new-user bonus has been applied. "
        f"New balance: **{new_user_balance}** {JC_EMOJI}"
    )


class SubscribeChoiceView(discord.ui.View):
    """
    Shown before finalizing a `subscription_title` purchase (VIP/Elite) — asks
    whether it should auto-renew weekly or run for exactly one week and stop.
    Nothing is charged until one of these buttons is pressed.
    """

    def __init__(self, user_id: int, item_id: str, item: dict, channel, *, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.item_id = item_id
        self.item = item
        self.channel = channel
        self.message: discord.Message | None = None
        self._resolved = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This prompt isn't for you!", ephemeral=True
            )
            return False
        return True

    def _disable(self) -> None:
        for child in self.children:
            child.disabled = True

    async def _finalize(self, interaction: discord.Interaction, auto_renew: bool) -> None:
        if self._resolved:
            return
        self._resolved = True
        self._disable()
        success, msg = await _purchase_item(
            interaction.user, self.item_id, channel=self.channel, auto_renew=auto_renew
        )
        if msg is None:
            await interaction.response.edit_message(
                content="✅ Purchased! Check the channel for the announcement.", embed=None, view=self
            )
        else:
            await interaction.response.edit_message(content=msg, embed=None, view=self)
        self.stop()

    @discord.ui.button(label="Auto-renew ON", style=discord.ButtonStyle.success, emoji="🔁")
    async def renew_on(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finalize(interaction, auto_renew=True)

    @discord.ui.button(label="Auto-renew OFF (one week only)", style=discord.ButtonStyle.secondary, emoji="🔂")
    async def renew_off(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finalize(interaction, auto_renew=False)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._resolved:
            return
        self._resolved = True
        self._disable()
        await interaction.response.edit_message(
            content="❌ Cancelled — nothing was charged.", embed=None, view=self
        )
        self.stop()

    async def on_timeout(self) -> None:
        if self._resolved:
            return
        self._resolved = True
        self._disable()
        if self.message:
            try:
                await self.message.edit(
                    content="⏰ This prompt expired — nothing was charged. Run `!shop` again to subscribe.",
                    embed=None, view=self,
                )
            except discord.HTTPException:
                pass


def _subscribe_choice_embed(item_id: str, item: dict, user_id: int) -> discord.Embed:
    swap_note = ""
    currently_equipped = get_equipped_title(user_id)
    if currently_equipped and currently_equipped != item_id:
        swap_note = (
            f"\n\n⚠️ You currently have **{get_title_label(currently_equipped)}** equipped — "
            f"subscribing will swap you into **{item['name']}** immediately."
        )
    return discord.Embed(
        title=f"{item['name']} — {item['price']:,} {JC_NAME}s/week",
        description=(
            f"Should this **auto-renew** every week, or run for **one week only** and then end "
            f"on its own with no further charges?\n\nYou can change this anytime later with `!autorenew`."
            f"{swap_note}"
        ),
        color=discord.Color.blurple(),
    )


class _BuySelect(discord.ui.Select):
    """One dropdown covering every item passed in, used instead of a Buy
    button per item — keeps the shop tidy no matter how many items a
    category ends up with. Buying is just picking an option; the person
    who opened the menu is re-checked in the callback (interaction_check
    on the parent view already gates page/back navigation, but a fresh
    Select interaction needs its own check too)."""

    def __init__(self, items: list[tuple[str, dict]], *, placeholder: str, owner_id: int):
        self.owner_id = owner_id
        self._items = items  # kept so this select can rebuild an identical fresh copy of itself
        self._placeholder = placeholder
        options = [
            discord.SelectOption(
                label=(
                    f"Subscribe & Activate {item['name'].split(' ', 1)[-1]} — {item['price']} {JC_NAME}s/wk"
                    if item["kind"] == "subscription_title"
                    else f"Buy {item['name'].split(' ', 1)[-1]} — {item['price']} {JC_NAME}s"
                ),
                emoji=item["name"].split(" ", 1)[0],
                value=item_id,
            )
            for item_id, item in items
        ] or [discord.SelectOption(label="Nothing here yet", value="__none__")]
        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1, row=1)

    def _reset_in_view(self) -> None:
        """Swap this select out for a fresh, identical copy of itself in its
        parent view. Needed because Discord keeps showing the just-picked
        option as the select's displayed label until the message's
        components are re-sent by the bot — left alone, this both looks
        stuck and, on some clients, blocks picking that same option again.

        Returns the parent view. Important: we capture it BEFORE removing
        self — `View.remove_item()` detaches the item (self.view becomes
        None afterward), so reading self.view again after this runs would
        silently edit the message with a `None` view, wiping every
        component (buttons included), not just resetting the select."""
        view: discord.ui.View = self.view
        fresh = _BuySelect(self._items, placeholder=self._placeholder, owner_id=self.owner_id)
        view.remove_item(self)
        view.add_item(fresh)
        return view

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Open your own shop with `!shop` to buy something!", ephemeral=True
            )
            return
        item_id = self.values[0]
        if item_id == "__none__":
            await interaction.response.defer()
            return

        # Always reset the dropdown first, before anything else — this is
        # the interaction's primary response; everything below uses a
        # followup message instead.
        parent_view = self._reset_in_view()
        await interaction.response.edit_message(view=parent_view)

        item = SHOP_ITEMS.get(item_id)
        if item is not None and item["kind"] == "subscription_title":
            view = SubscribeChoiceView(interaction.user.id, item_id, item, interaction.channel)
            msg = await interaction.followup.send(
                embed=_subscribe_choice_embed(item_id, item, interaction.user.id), view=view, ephemeral=True
            )
            view.message = msg
            return
        success, msg = await _purchase_item(interaction.user, item_id, channel=interaction.channel)
        if msg is None:
            # Public announcement already posted to the channel — just
            # quietly confirm to the buyer so we don't double-post.
            await interaction.followup.send("✅ Purchased!", ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=not success)


class ShopView(discord.ui.View):
    """Paginated shop embed: ◀ Prev / Next ▶ / 🔍 View Perks / 🎨 Banners
    buttons up top, and a single Buy dropdown instead of one button per
    item."""

    def __init__(self, user_id: int, *, timeout: float = 90):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.page = 0
        self.items = _main_shop_items()
        self.total_pages = max(1, (len(self.items) + SHOP_PAGE_SIZE - 1) // SHOP_PAGE_SIZE)
        self.message: discord.Message | None = None
        self.add_item(self._make_select())

    def _make_select(self) -> _BuySelect:
        start = self.page * SHOP_PAGE_SIZE
        chunk = self.items[start:start + SHOP_PAGE_SIZE]
        return _BuySelect(chunk, placeholder="🛒 Choose an item to buy…", owner_id=self.user_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Open your own shop with `!shop` to buy something!", ephemeral=True
            )
            return False
        return True

    def _rebuild_select(self) -> None:
        for child in list(self.children):
            if isinstance(child, discord.ui.Select):
                self.remove_item(child)
        self.add_item(self._make_select())

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._rebuild_select()
        await interaction.response.edit_message(embed=_shop_page_embed(self.page, self.user_id), view=self)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=0)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = (self.page - 1) % self.total_pages
        await self._refresh(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = (self.page + 1) % self.total_pages
        await self._refresh(interaction)

    @discord.ui.button(label="🔍 View Perks", style=discord.ButtonStyle.primary, row=0)
    async def view_perks(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=_title_perks_embed(), ephemeral=True)

    @discord.ui.button(label="🎨 Banners", style=discord.ButtonStyle.secondary, row=0)
    async def banners_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = BannerShopView(self.user_id)
        await interaction.response.edit_message(embed=_banner_shop_embed(), view=view)
        view.message = interaction.message

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class BannerShopView(discord.ui.View):
    """Separate banner sub-menu: a Buy dropdown for banners only, plus a
    Back button that returns to the main shop's first page."""

    def __init__(self, user_id: int, *, timeout: float = 90):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.message: discord.Message | None = None
        self.add_item(_BuySelect(_banner_shop_items(), placeholder="🎨 Choose a banner to buy…", owner_id=user_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Open your own shop with `!shop` to buy something!", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="◀ Back to Shop", style=discord.ButtonStyle.secondary, row=0)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ShopView(self.user_id)
        await interaction.response.edit_message(embed=_shop_page_embed(0, self.user_id), view=view)
        view.message = interaction.message

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ── Transfer JC ─────────────────────────────────────────────────────────────

import asyncio

# Pending transfers: keyed by (sender_id, recipient_id) → asyncio.Event
# We store the transfer details separately so the view can resolve them.
_pending_transfers: dict[tuple[int, int], dict] = {}


class TransferRequestView(discord.ui.View):
    """
    Shown to the *recipient* of a JC transfer request.
    Two buttons: Accept ✅  |  Decline ❌
    Auto-times out after 60 seconds.
    """

    def __init__(
        self,
        sender: discord.User | discord.Member,
        recipient: discord.User | discord.Member,
        amount: int,
        *,
        timeout: float = 60,
    ):
        super().__init__(timeout=timeout)
        self.sender = sender
        self.recipient = recipient
        self.amount = amount
        self.message: discord.Message | None = None
        self._resolved = False

    # Only the recipient may interact
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.recipient.id:
            await interaction.response.send_message(
                "This transfer request isn't for you!", ephemeral=True
            )
            return False
        return True

    def _disable(self) -> None:
        for child in self.children:
            child.disabled = True

    async def _resolve(
        self,
        interaction: discord.Interaction,
        accepted: bool,
    ) -> None:
        if self._resolved:
            return
        self._resolved = True
        self._disable()
        key = (self.sender.id, self.recipient.id)
        _pending_transfers.pop(key, None)

        if accepted:
            # Deduct from sender — check they still have enough
            if not spend_credits(self.sender.id, self.amount):
                embed = discord.Embed(
                    description=(
                        f"❌ Transfer failed — **{self.sender.display_name}** no longer has "
                        f"enough {JC_EMOJI} to cover this transfer."
                    ),
                    color=discord.Color.red(),
                )
                await interaction.response.edit_message(embed=embed, view=self)
                return

            # Credit the recipient
            add_credits(self.recipient.id, self.amount)
            new_sender_bal = get_credits(self.sender.id)
            new_recip_bal  = get_credits(self.recipient.id)

            embed = discord.Embed(
                title=f"{JC_EMOJI} Transfer Complete",
                description=(
                    f"✅ **{self.recipient.display_name}** accepted the transfer!\n\n"
                    f"**{self.sender.display_name}** sent **{self.amount}** {JC_NAME}s.\n"
                    f"└ New balance: **{new_sender_bal}** {JC_EMOJI}\n\n"
                    f"**{self.recipient.display_name}** received **{self.amount}** {JC_NAME}s.\n"
                    f"└ New balance: **{new_recip_bal}** {JC_EMOJI}"
                ),
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                title=f"{JC_EMOJI} Transfer Declined",
                description=(
                    f"❌ **{self.recipient.display_name}** declined the transfer request "
                    f"from **{self.sender.display_name}** for **{self.amount}** {JC_NAME}s."
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
        _pending_transfers.pop((self.sender.id, self.recipient.id), None)
        self._disable()
        if self.message:
            try:
                embed = discord.Embed(
                    title=f"{JC_EMOJI} Transfer Expired",
                    description=(
                        f"⏰ The transfer request of **{self.amount}** {JC_NAME}s "
                        f"from **{self.sender.display_name}** to **{self.recipient.display_name}** "
                        f"expired with no response."
                    ),
                    color=discord.Color.dark_gray(),
                )
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass


_pending_sub_gifts: dict[tuple[int, int, str], dict] = {}  # (sender_id, recipient_id, item_id) -> {}


class GiftSubRequestView(discord.ui.View):
    """
    Shown to the *recipient* of a gifted VIP/Elite subscription.
    Two buttons: Accept ✅  |  Decline ❌.  Auto-times out after 60 seconds.

    Mirrors TransferRequestView's key property: the sender's JC is only
    deducted at Accept time, not when the gift is sent — so a decline or
    timeout never needs a refund because nothing was ever charged.
    """

    def __init__(
        self,
        sender: discord.User | discord.Member,
        recipient: discord.User | discord.Member,
        item_id: str,
        item: dict,
        *,
        timeout: float = 60,
    ):
        super().__init__(timeout=timeout)
        self.sender = sender
        self.recipient = recipient
        self.item_id = item_id
        self.item = item
        self.message: discord.Message | None = None
        self._resolved = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.recipient.id:
            await interaction.response.send_message(
                "This gift isn't for you!", ephemeral=True
            )
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
        key = (self.sender.id, self.recipient.id, self.item_id)
        _pending_sub_gifts.pop(key, None)

        if accepted:
            if not spend_credits(self.sender.id, self.item["price"]):
                embed = discord.Embed(
                    description=(
                        f"❌ Gift failed — **{self.sender.display_name}** no longer has enough "
                        f"{JC_EMOJI} to cover this gift."
                    ),
                    color=discord.Color.red(),
                )
                await interaction.response.edit_message(embed=embed, view=self)
                return

            grant_title(self.recipient.id, self.item_id)
            grant_banner(self.recipient.id, "banner_prestige")
            # Extend from their current expiry if they already have this
            # subscription active, otherwise start a fresh week from now.
            existing = get_subscriptions(self.recipient.id).get(self.item_id)
            base = existing if existing and existing > time.time() else time.time()
            new_next_charge = base + SUBSCRIPTION_PERIOD_SECONDS
            set_subscription(self.recipient.id, self.item_id, new_next_charge)
            new_sender_bal = get_credits(self.sender.id)

            embed = discord.Embed(
                title=f"{JC_EMOJI} Gift Accepted!",
                description=(
                    f"🎁 **{self.recipient.display_name}** accepted **{self.item['name']}** "
                    f"from **{self.sender.display_name}**!\n\n"
                    f"**{self.sender.display_name}**'s new balance: **{new_sender_bal}** {JC_EMOJI}\n\n"
                    f"It's active for the next 7 days, and the exclusive 💠 **Prestige Banner** "
                    f"has been unlocked too. Run `/title {self.item['name']}` to turn its perks on.\n\n"
                    f"Heads up: this gift only covers the current week — future weekly renewals "
                    f"bill **{self.recipient.display_name}**'s own {JC_EMOJI} balance, not the gifter's."
                ),
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                title=f"{JC_EMOJI} Gift Declined",
                description=(
                    f"❌ **{self.recipient.display_name}** declined the **{self.item['name']}** gift "
                    f"from **{self.sender.display_name}**. Nothing was charged."
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
        _pending_sub_gifts.pop((self.sender.id, self.recipient.id, self.item_id), None)
        self._disable()
        if self.message:
            try:
                embed = discord.Embed(
                    title=f"{JC_EMOJI} Gift Expired",
                    description=(
                        f"⏰ The **{self.item['name']}** gift from **{self.sender.display_name}** to "
                        f"**{self.recipient.display_name}** expired with no response. Nothing was charged."
                    ),
                    color=discord.Color.dark_gray(),
                )
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass


class Economy(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bill_subscriptions.start()

    def cog_unload(self):
        self.bill_subscriptions.cancel()

    @tasks.loop(hours=1)
    async def bill_subscriptions(self):
        """Charge the weekly fee for every active title subscription. If a
        user can't cover the renewal, the title is revoked entirely (and
        unequipped if it was equipped) rather than left in limbo. Subscriptions
        with auto-renew turned off skip the charge entirely and just expire
        on schedule instead."""
        now = time.time()

        async def _end_subscription(uid: int, title_id: str, item: dict, *, reason: str) -> None:
            """Shared cleanup for a subscription that's ending (whether from
            a failed charge or auto-renew being off) — revoke the title,
            clear the subscription + auto-renew flag, and strip the shared
            Prestige banner only if no other subscription_title remains."""
            revoke_title(uid, title_id)
            clear_subscription(uid, title_id)
            clear_auto_renew(uid, title_id)
            remaining_subs = get_subscriptions(uid)
            still_has_sub_title = any(
                SHOP_ITEMS.get(tid, {}).get("kind") == "subscription_title"
                for tid in remaining_subs
            )
            if not still_has_sub_title:
                revoke_banner(uid, "banner_prestige")
            user = self.bot.get_user(uid)
            if user:
                try:
                    await user.send(reason)
                except discord.HTTPException:
                    pass

        for uid_str, subs in get_all_subscriptions().items():
            uid = int(uid_str)
            for title_id, next_charge in subs.items():
                if now < next_charge:
                    continue
                item = SHOP_ITEMS.get(title_id)
                if item is None:
                    clear_subscription(uid, title_id)
                    clear_auto_renew(uid, title_id)
                    continue

                if not get_auto_renew(uid, title_id):
                    await _end_subscription(
                        uid, title_id, item,
                        reason=(
                            f"🔂 Your **{item['name']}** subscription has ended (auto-renew was off) — "
                            f"no charge was made. You can subscribe again anytime from `/shop`."
                        ),
                    )
                    continue

                if spend_credits(uid, item["price"]):
                    set_subscription(uid, title_id, now + SUBSCRIPTION_PERIOD_SECONDS)
                    bonus = TITLE_PERKS.get(title_id, {}).get("weekly_loyalty_bonus")
                    if bonus:
                        add_credits(uid, bonus)
                        user = self.bot.get_user(uid)
                        if user:
                            try:
                                await user.send(
                                    f"🎁 Your **{item['name']}** subscription renewed — "
                                    f"here's your weekly loyalty bonus: **+{bonus} {JC_NAME}** {JC_EMOJI}!"
                                )
                            except discord.HTTPException:
                                pass
                else:
                    await _end_subscription(
                        uid, title_id, item,
                        reason=(
                            f"⚠️ Your **{item['name']}** subscription lapsed — you didn't have "
                            f"enough {JC_EMOJI} for the **{item['price']:,} JC/week** renewal, "
                            f"so it's been removed. You can buy it again anytime from `/shop`."
                        ),
                    )

    @bill_subscriptions.before_loop
    async def _before_bill_subscriptions(self):
        await self.bot.wait_until_ready()

    @commands.command(name="balance", aliases=["jc", "credits"])
    async def prefix_balance(self, ctx: commands.Context, user: discord.User = None):
        """!balance / !jc / !credits — check your (or someone else's) JC balance."""
        target = user or ctx.author
        await ctx.reply(embed=_balance_embed(target))

    @app_commands.command(name="balance", description="Check your Jarvis Credit (JC) balance")
    @app_commands.describe(user="User to look up (optional — leave empty for yourself)")
    async def slash_balance(self, interaction: discord.Interaction, user: discord.User = None):
        target = user or interaction.user
        await interaction.response.send_message(embed=_balance_embed(target))

    @commands.command(name="givecredits", aliases=["givejc", "addcredits"])
    @commands.is_owner()
    async def prefix_givecredits(
        self,
        ctx: commands.Context,
        users: commands.Greedy[discord.User],
        amount: int,
    ):
        """!givecredits @user1 @user2 ... <amount> — owner-only. Grants (or removes, if negative) JC to one or more users."""
        if not users:
            await ctx.reply("**Usage:** `!givecredits @user1 [@user2 ...] <amount>`\n**Example:** `!givecredits @Phantom @Someone 100`")
            return
        if amount == 0:
            await ctx.reply("⚠️ Amount must be non-zero.")
            return

        verb = "Granted" if amount > 0 else "Removed"
        lines = []
        for user in users:
            new_balance = add_credits(user.id, amount)
            lines.append(f"**{user.display_name}** → new balance: **{new_balance}** {JC_NAME}s")

        embed = discord.Embed(
            description=(
                f"{JC_EMOJI} **{verb} {abs(amount)} {JC_NAME}** "
                f"{'to' if amount > 0 else 'from'} **{len(users)}** user(s):\n\n"
                + "\n".join(lines)
            ),
            color=discord.Color.gold(),
        )
        await ctx.reply(embed=embed)

    @prefix_givecredits.error
    async def givecredits_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.NotOwner):
            await ctx.reply("🚫 Only the bot owner can use this command.")
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.reply("**Usage:** `!givecredits @user1 [@user2 ...] <amount>`\n**Example:** `!givecredits @Phantom @Someone 100`")

    @commands.command(name="removejc", aliases=["removecredits", "deductjc"])
    @commands.is_owner()
    async def prefix_removejc(
        self,
        ctx: commands.Context,
        users: commands.Greedy[discord.User],
        amount: int,
    ):
        """!removejc @user <amount> — owner-only. Deducts JC from one or more users. Balance won't go below 0."""
        if not users:
            await ctx.reply("**Usage:** `!removejc @user1 [@user2 ...] <amount>`\n**Example:** `!removejc @Phantom 100`")
            return
        if amount <= 0:
            await ctx.reply("⚠️ Amount must be a positive number.")
            return

        lines = []
        for user in users:
            new_balance = add_credits(user.id, -amount)
            lines.append(f"**{user.display_name}** → new balance: **{new_balance}** {JC_NAME}s")

        embed = discord.Embed(
            description=(
                f"{JC_EMOJI} **Removed {amount} {JC_NAME}** "
                f"from **{len(users)}** user(s):\n\n"
                + "\n".join(lines)
            ),
            color=discord.Color.red(),
        )
        await ctx.reply(embed=embed)

    @prefix_removejc.error
    async def removejc_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.NotOwner):
            await ctx.reply("🚫 Only the bot owner can use this command.")
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.reply("**Usage:** `!removejc @user1 [@user2 ...] <amount>`\n**Example:** `!removejc @Phantom 100`")

    @app_commands.command(name="givecredits", description="(Owner only) Grant or remove JC from a user")
    @app_commands.describe(user="User to give/remove JC", amount="Amount of JC (negative to remove)")
    async def slash_givecredits(self, interaction: discord.Interaction, user: discord.User, amount: int):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("🚫 Only the bot owner can use this command.", ephemeral=True)
            return
        if amount == 0:
            await interaction.response.send_message("⚠️ Amount must be non-zero.", ephemeral=True)
            return
        new_balance = add_credits(user.id, amount)
        verb = "Granted" if amount > 0 else "Removed"
        embed = discord.Embed(
            description=(
                f"{JC_EMOJI} **{verb} {abs(amount)} {JC_NAME}** "
                f"{'to' if amount > 0 else 'from'} **{user.display_name}**.\n"
                f"New balance: **{new_balance}** {JC_NAME}s."
            ),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    @commands.command(name="leaderboard", aliases=["jcleaderboard", "jctop"])
    async def prefix_leaderboard(self, ctx: commands.Context):
        """!leaderboard — top Jarvis Credit holders."""
        async with ctx.typing():
            embed = await _leaderboard_embed(self.bot)
        await ctx.reply(embed=embed)

    @app_commands.command(name="leaderboard", description="View the top Jarvis Credit holders")
    async def slash_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await _leaderboard_embed(self.bot)
        await interaction.followup.send(embed=embed)

    # ── !streak ──────────────────────────────────────────────────────────────

    @commands.command(name="streak", aliases=["jcstreak"])
    async def prefix_streak(self, ctx: commands.Context, user: discord.User = None):
        """!streak — check your (or someone else's) daily chat streak and milestone progress."""
        target = user or ctx.author
        await ctx.reply(embed=_streak_embed(target, get_streak(target.id)))

    @app_commands.command(name="streak", description="Check your daily chat streak and milestone progress")
    @app_commands.describe(user="User to look up (optional — leave empty for yourself)")
    async def slash_streak(self, interaction: discord.Interaction, user: discord.User = None):
        target = user or interaction.user
        await interaction.response.send_message(embed=_streak_embed(target, get_streak(target.id)))

    # ── !shop ────────────────────────────────────────────────────────────────

    @commands.command(name="shop", aliases=["jcshop"])
    async def prefix_shop(self, ctx: commands.Context, action: str = None, item_id: str = None):
        """!shop — browse the JC shop. !shop buy <item_id> — buy directly."""
        if action and action.lower() == "buy":
            if not item_id:
                await ctx.reply("**Usage:** `!shop buy <item_id>` — see `!shop` for item ids.")
                return
            item_id = item_id.lower()
            item = SHOP_ITEMS.get(item_id)
            if item is not None and item["kind"] == "subscription_title":
                view = SubscribeChoiceView(ctx.author.id, item_id, item, ctx.channel)
                msg = await ctx.reply(embed=_subscribe_choice_embed(item_id, item, ctx.author.id), view=view)
                view.message = msg
                return
            success, msg = await _purchase_item(ctx.author, item_id, channel=ctx.channel)
            if msg is None:
                await ctx.message.add_reaction("✅")  # public announcement already posted above
            else:
                await ctx.reply(msg)
            return

        view = ShopView(ctx.author.id)
        msg = await ctx.reply(embed=_shop_page_embed(0, ctx.author.id), view=view)
        view.message = msg

    @app_commands.command(name="shop", description="Browse the Jarvis Credit shop")
    async def slash_shop(self, interaction: discord.Interaction):
        view = ShopView(interaction.user.id)
        await interaction.response.send_message(embed=_shop_page_embed(0, interaction.user.id), view=view)
        view.message = await interaction.original_response()

    @prefix_shop.error
    async def shop_error(self, ctx: commands.Context, error):
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.reply("**Usage:** `!shop` or `!shop buy <item_id>`")

    # ── !titleperks ──────────────────────────────────────────────────────────

    @commands.command(name="titleperks", aliases=["perks"])
    async def prefix_titleperks(self, ctx: commands.Context):
        """!titleperks — see what gameplay perks each shop title grants."""
        await ctx.reply(embed=_title_perks_embed())

    @app_commands.command(name="titleperks", description="See what gameplay perks each shop title grants")
    async def slash_titleperks(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=_title_perks_embed())

    # ── !mysterybox (shortcut straight to the shop's signature item) ─────────

    @commands.command(name="mysterybox", aliases=["mbox", "jcbox"])
    async def prefix_mysterybox(self, ctx: commands.Context):
        """!mysterybox — open a Mystery Box for 150 JC. Random reward 10–300 JC."""
        success, msg = await _purchase_item(ctx.author, "mystery_box", channel=ctx.channel)
        if msg is None:
            await ctx.message.add_reaction("🎰")  # result already posted publicly above
        else:
            await ctx.reply(msg)

    @app_commands.command(name="mysterybox", description="Open a Mystery Box for 150 JC — random reward 10–300 JC")
    async def slash_mysterybox(self, interaction: discord.Interaction):
        success, msg = await _purchase_item(interaction.user, "mystery_box", channel=interaction.channel)
        if msg is None:
            await interaction.response.send_message("🎰 Box opened — see above!", ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=not success)

    # ── !deluxebox (higher-tier box — this is the one title perks boost) ─────

    @commands.command(name="deluxebox", aliases=["dbox"])
    async def prefix_deluxebox(self, ctx: commands.Context):
        """!deluxebox — open a Deluxe Mystery Box. Bigger cost, bigger reward,
        and VIP/Elite titles boost the payout further."""
        success, msg = await _purchase_item(ctx.author, "mystery_box_deluxe", channel=ctx.channel)
        if msg is None:
            await ctx.message.add_reaction("🎆")  # result already posted publicly above
        else:
            await ctx.reply(msg)

    @app_commands.command(
        name="deluxebox",
        description=f"Open a Deluxe Mystery Box for {DELUXE_MYSTERY_BOX_COST} JC — bigger reward, boosted by VIP/Elite",
    )
    async def slash_deluxebox(self, interaction: discord.Interaction):
        success, msg = await _purchase_item(interaction.user, "mystery_box_deluxe", channel=interaction.channel)
        if msg is None:
            await interaction.response.send_message("🎆 Box opened — see above!", ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=not success)

    # ── !invite ──────────────────────────────────────────────────────────────

    @commands.command(name="invite", aliases=["refer", "myinvite"])
    async def prefix_invite(self, ctx: commands.Context):
        """!invite — get your personal referral code to share with friends."""
        code = get_or_create_referral_code(ctx.author.id)
        await ctx.reply(embed=_invite_embed(ctx.author, code))

    @app_commands.command(name="invite", description="Get your personal referral code to share with friends")
    async def slash_invite(self, interaction: discord.Interaction):
        code = get_or_create_referral_code(interaction.user.id)
        await interaction.response.send_message(embed=_invite_embed(interaction.user, code), ephemeral=True)

    # ── !redeem ──────────────────────────────────────────────────────────────
    # Must be the redeemer's FIRST-EVER interaction with Jarvis to count.
    # See _handle_redeem / redeem_referral_code for the enforcement.

    @commands.command(name="redeem", aliases=["referral", "useinvite"])
    async def prefix_redeem(self, ctx: commands.Context, code: str = None):
        """!redeem <code> — redeem a friend's referral code. Must be your first-ever interaction with Jarvis."""
        msg = await _handle_redeem(ctx.author, code, ctx.channel)
        await ctx.reply(msg)

    @app_commands.command(name="redeem", description="Redeem a friend's referral code (must be your first-ever interaction with Jarvis)")
    @app_commands.describe(code="The referral code your friend shared with you")
    async def slash_redeem(self, interaction: discord.Interaction, code: str):
        msg = await _handle_redeem(interaction.user, code, interaction.channel)
        await interaction.response.send_message(msg)

    @prefix_redeem.error
    async def redeem_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply("**Usage:** `!redeem <code>` — get a code from a friend's `!invite`.")

    # ── !transferjc ──────────────────────────────────────────────────────────

    @commands.command(name="transferjc", aliases=["sendjc", "transfer"])
    async def prefix_transferjc(
        self,
        ctx: commands.Context,
        recipient: discord.User,
        amount: int,
    ):
        """!transferjc @user <amount> — Send JC to another user. They must accept first."""
        sender = ctx.author

        # ── Validation ────────────────────────────────────────────────────
        if recipient.id == sender.id:
            await ctx.reply("❌ You can't transfer JC to yourself.")
            return

        if recipient.bot:
            await ctx.reply("❌ You can't transfer JC to a bot.")
            return

        if amount <= 0:
            await ctx.reply("❌ Amount must be a positive number.")
            return

        sender_balance = get_credits(sender.id)
        if sender_balance < amount:
            await ctx.reply(
                f"❌ Insufficient balance. You have **{sender_balance}** {JC_EMOJI} "
                f"but tried to send **{amount}**."
            )
            return

        # ── Duplicate-request guard ───────────────────────────────────────
        key = (sender.id, recipient.id)
        if key in _pending_transfers:
            await ctx.reply(
                f"⚠️ You already have a pending transfer request to **{recipient.display_name}**. "
                f"Wait for them to respond first."
            )
            return

        _pending_transfers[key] = {"amount": amount}

        # ── Build the request embed ───────────────────────────────────────
        request_embed = discord.Embed(
            title=f"{JC_EMOJI} Incoming Transfer Request",
            description=(
                f"**{sender.display_name}** wants to send you **{amount}** {JC_NAME}s.\n\n"
                f"Do you accept?"
            ),
            color=discord.Color.blurple(),
        )
        request_embed.set_footer(text="This request expires in 60 seconds.")
        request_embed.set_thumbnail(url=sender.display_avatar.url)

        view = TransferRequestView(sender=sender, recipient=recipient, amount=amount)

        # Mention the recipient so they get pinged
        msg = await ctx.send(
            content=f"{recipient.mention}, you have a transfer request!",
            embed=request_embed,
            view=view,
        )
        view.message = msg

        # Confirm to sender with a reaction
        await ctx.message.add_reaction("📨")

    @prefix_transferjc.error
    async def transferjc_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(
                "**Usage:** `!transferjc @user <amount>`\n"
                "**Example:** `!transferjc @user 50`"
            )
        elif isinstance(error, commands.BadArgument):
            await ctx.reply(
                "❌ Invalid arguments. Make sure you @mention a valid user and provide a whole number.\n"
                "**Usage:** `!transferjc @user <amount>`"
            )

    # ── !giftsub ─────────────────────────────────────────────────────────────

    @commands.command(name="giftsub", aliases=["giftvip", "giftelite", "gift"])
    async def prefix_giftsub(
        self,
        ctx: commands.Context,
        recipient: discord.User = None,
        tier: str = None,
    ):
        """!giftsub @user <vip|elite> — pay for someone else's VIP/Elite subscription
        (or extend theirs by a week) using your own JC. They must accept first —
        nothing is charged unless they do."""
        sender = ctx.author

        if recipient is None or tier is None:
            await ctx.reply("**Usage:** `!giftsub @user <vip|elite>`\n**Example:** `!giftsub @user vip`")
            return

        if recipient.id == sender.id:
            await ctx.reply("❌ You can't gift a subscription to yourself — just buy it with `!shop`.")
            return
        if recipient.bot:
            await ctx.reply("❌ You can't gift a subscription to a bot.")
            return

        item_id = {"vip": "title_vip", "elite": "title_elite"}.get(tier.strip().lower().lstrip("!/"))
        if item_id is None:
            await ctx.reply("❌ Unknown tier — use `vip` or `elite`. **Example:** `!giftsub @user elite`")
            return

        item = SHOP_ITEMS[item_id]
        if not _item_available(item):
            await ctx.reply(f"❌ **{item['name']}** isn't currently available.")
            return

        sender_balance = get_credits(sender.id)
        if sender_balance < item["price"]:
            await ctx.reply(
                f"❌ Insufficient balance. You have **{sender_balance}** {JC_EMOJI} but "
                f"**{item['name']}** costs **{item['price']:,}** {JC_NAME}s/week."
            )
            return

        key = (sender.id, recipient.id, item_id)
        if key in _pending_sub_gifts:
            await ctx.reply(
                f"⚠️ You already have a pending **{item['name']}** gift to **{recipient.display_name}**. "
                f"Wait for them to respond first."
            )
            return

        _pending_sub_gifts[key] = {}

        request_embed = discord.Embed(
            title=f"{JC_EMOJI} Incoming Subscription Gift",
            description=(
                f"**{sender.display_name}** wants to gift you **{item['name']}** "
                f"(**{item['price']:,} {JC_NAME}s**, one week) — use `/titleperks` to see what it grants.\n\n"
                f"Note: only this first week is a gift — if you keep it past that, future weekly "
                f"renewals come out of **your own** {JC_EMOJI} balance.\n\nDo you accept?"
            ),
            color=discord.Color.blurple(),
        )
        request_embed.set_footer(text="This request expires in 60 seconds. Nothing is charged unless you accept.")
        request_embed.set_thumbnail(url=sender.display_avatar.url)

        view = GiftSubRequestView(sender=sender, recipient=recipient, item_id=item_id, item=item)
        msg = await ctx.send(
            content=f"{recipient.mention}, you've got a gift!",
            embed=request_embed,
            view=view,
        )
        view.message = msg
        await ctx.message.add_reaction("🎁")

    @prefix_giftsub.error
    async def giftsub_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply("**Usage:** `!giftsub @user <vip|elite>`\n**Example:** `!giftsub @user vip`")
        elif isinstance(error, commands.BadArgument):
            await ctx.reply(
                "❌ Invalid arguments. Make sure you @mention a valid user.\n"
                "**Usage:** `!giftsub @user <vip|elite>`"
            )

    @app_commands.command(name="giftsub", description="Gift someone a week of VIP/Elite using your own JC (they must accept)")
    @app_commands.describe(recipient="Who to gift the subscription to", tier="vip or elite")
    async def slash_giftsub(self, interaction: discord.Interaction, recipient: discord.User, tier: str):
        sender = interaction.user

        if recipient.id == sender.id:
            await interaction.response.send_message(
                "❌ You can't gift a subscription to yourself — just buy it with `/shop`.", ephemeral=True
            )
            return
        if recipient.bot:
            await interaction.response.send_message("❌ You can't gift a subscription to a bot.", ephemeral=True)
            return

        item_id = {"vip": "title_vip", "elite": "title_elite"}.get(tier.strip().lower())
        if item_id is None:
            await interaction.response.send_message("❌ Unknown tier — use `vip` or `elite`.", ephemeral=True)
            return

        item = SHOP_ITEMS[item_id]
        if not _item_available(item):
            await interaction.response.send_message(f"❌ **{item['name']}** isn't currently available.", ephemeral=True)
            return

        sender_balance = get_credits(sender.id)
        if sender_balance < item["price"]:
            await interaction.response.send_message(
                f"❌ Insufficient balance. You have **{sender_balance}** {JC_EMOJI} but "
                f"**{item['name']}** costs **{item['price']:,}** {JC_NAME}s/week.",
                ephemeral=True,
            )
            return

        key = (sender.id, recipient.id, item_id)
        if key in _pending_sub_gifts:
            await interaction.response.send_message(
                f"⚠️ You already have a pending **{item['name']}** gift to **{recipient.display_name}**. "
                f"Wait for them to respond first.",
                ephemeral=True,
            )
            return

        _pending_sub_gifts[key] = {}

        request_embed = discord.Embed(
            title=f"{JC_EMOJI} Incoming Subscription Gift",
            description=(
                f"**{sender.display_name}** wants to gift you **{item['name']}** "
                f"(**{item['price']:,} {JC_NAME}s**, one week) — use `/titleperks` to see what it grants.\n\n"
                f"Note: only this first week is a gift — if you keep it past that, future weekly "
                f"renewals come out of **your own** {JC_EMOJI} balance.\n\nDo you accept?"
            ),
            color=discord.Color.blurple(),
        )
        request_embed.set_footer(text="This request expires in 60 seconds. Nothing is charged unless you accept.")
        request_embed.set_thumbnail(url=sender.display_avatar.url)

        view = GiftSubRequestView(sender=sender, recipient=recipient, item_id=item_id, item=item)
        # Deliberately NOT ephemeral — the recipient needs to see and click
        # these buttons too, and an ephemeral response is only visible to
        # the person who ran the command (the sender), not the recipient.
        await interaction.response.send_message(
            content=f"{recipient.mention}, you've got a gift!",
            embed=request_embed,
            view=view,
        )
        view.message = await interaction.original_response()

    @slash_giftsub.autocomplete("tier")
    async def giftsub_tier_autocomplete(self, interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=label, value=value)
            for value, label in (("vip", "VIP"), ("elite", "Elite"))
            if current.lower() in value
        ]

    # ── !autorenew ───────────────────────────────────────────────────────────

    @commands.command(name="autorenew", aliases=["renew"])
    async def prefix_autorenew(
        self,
        ctx: commands.Context,
        tier: str = None,
        state_arg: str = None,
    ):
        """!autorenew <vip|elite> <on|off> — toggle auto-renew for your own
        active subscription. With no on/off given, shows its current state."""
        item_id = {"vip": "title_vip", "elite": "title_elite"}.get(
            (tier or "").strip().lower().lstrip("!/")
        )
        if item_id is None:
            await ctx.reply(
                "**Usage:** `!autorenew <vip|elite> [on|off]`\n"
                "**Example:** `!autorenew vip off`"
            )
            return

        item = SHOP_ITEMS[item_id]
        next_charge = get_subscriptions(ctx.author.id).get(item_id)
        if not next_charge or next_charge <= time.time():
            await ctx.reply(f"❌ You don't have an active **{item['name']}** subscription right now.")
            return

        if state_arg is None:
            current = get_auto_renew(ctx.author.id, item_id)
            ts = int(next_charge)
            await ctx.reply(
                f"🔁 Auto-renew for **{item['name']}** is currently **{'ON' if current else 'OFF'}**.\n"
                f"{'Next renewal' if current else 'Ends'}: <t:{ts}:f> (<t:{ts}:R>)\n"
                f"Use `!autorenew {tier} on` or `!autorenew {tier} off` to change it."
            )
            return

        state_key = state_arg.strip().lower()
        if state_key not in ("on", "off"):
            await ctx.reply("❌ Use `on` or `off` — **Example:** `!autorenew vip off`")
            return

        new_value = state_key == "on"
        set_auto_renew(ctx.author.id, item_id, new_value)
        ts = int(next_charge)
        if new_value:
            await ctx.reply(
                f"✅ Auto-renew turned **ON** for **{item['name']}** — next renewal <t:{ts}:f> "
                f"(<t:{ts}:R>) will bill **{item['price']:,} {JC_NAME}s** as usual."
            )
        else:
            await ctx.reply(
                f"✅ Auto-renew turned **OFF** for **{item['name']}** — it'll run until <t:{ts}:f> "
                f"(<t:{ts}:R>) and then end on its own, no further charges."
            )

    @app_commands.command(name="autorenew", description="Check or toggle auto-renew for your VIP/Elite subscription")
    @app_commands.describe(tier="vip or elite", state="on or off — leave empty to just check the current setting")
    async def slash_autorenew(self, interaction: discord.Interaction, tier: str, state: str = None):
        item_id = {"vip": "title_vip", "elite": "title_elite"}.get(tier.strip().lower())
        if item_id is None:
            await interaction.response.send_message("❌ Unknown tier — use `vip` or `elite`.", ephemeral=True)
            return
        item = SHOP_ITEMS[item_id]
        next_charge = get_subscriptions(interaction.user.id).get(item_id)
        if not next_charge or next_charge <= time.time():
            await interaction.response.send_message(
                f"❌ You don't have an active **{item['name']}** subscription right now.", ephemeral=True
            )
            return

        ts = int(next_charge)
        if state is None:
            current = get_auto_renew(interaction.user.id, item_id)
            await interaction.response.send_message(
                f"🔁 Auto-renew for **{item['name']}** is currently **{'ON' if current else 'OFF'}**.\n"
                f"{'Next renewal' if current else 'Ends'}: <t:{ts}:f> (<t:{ts}:R>)",
                ephemeral=True,
            )
            return

        state_key = state.strip().lower()
        if state_key not in ("on", "off"):
            await interaction.response.send_message("❌ Use `on` or `off`.", ephemeral=True)
            return

        new_value = state_key == "on"
        set_auto_renew(interaction.user.id, item_id, new_value)
        if new_value:
            await interaction.response.send_message(
                f"✅ Auto-renew turned **ON** for **{item['name']}** — next renewal <t:{ts}:f> (<t:{ts}:R>)."
            )
        else:
            await interaction.response.send_message(
                f"✅ Auto-renew turned **OFF** for **{item['name']}** — ends <t:{ts}:f> (<t:{ts}:R>), no further charges."
            )

    @slash_autorenew.autocomplete("tier")
    async def autorenew_tier_autocomplete(self, interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=label, value=value)
            for value, label in (("vip", "VIP"), ("elite", "Elite"))
            if current.lower() in value
        ]

    @slash_autorenew.autocomplete("state")
    async def autorenew_state_autocomplete(self, interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=label, value=value)
            for value, label in (("on", "On"), ("off", "Off"))
            if current.lower() in value
        ]

    # ── !grantsub (owner only) ──────────────────────────────────────────────

    def _resolve_sub_tier(self, tier: str) -> tuple[str, dict] | tuple[None, None]:
        item_id = {"vip": "title_vip", "elite": "title_elite"}.get(tier.strip().lower().lstrip("!/"))
        if item_id is None:
            return None, None
        return item_id, SHOP_ITEMS[item_id]

    def _grant_or_extend_sub(self, user_id: int, item_id: str, days: float) -> tuple[float, bool]:
        """Shared logic for owner grants: extend from current expiry if the
        subscription is already active, otherwise start fresh from now.
        Returns (new_next_charge_epoch, was_extended)."""
        existing = get_subscriptions(user_id).get(item_id)
        extended = bool(existing and existing > time.time())
        base = existing if extended else time.time()
        new_next_charge = base + days * 86400
        grant_title(user_id, item_id)
        grant_banner(user_id, "banner_prestige")
        set_subscription(user_id, item_id, new_next_charge)
        return new_next_charge, extended

    @commands.command(name="grantsub", aliases=["givesub", "grantvip"])
    @commands.is_owner()
    async def prefix_grantsub(
        self,
        ctx: commands.Context,
        user: discord.User = None,
        tier: str = None,
        days: str = "7",
    ):
        """Bot owner only: grant or extend a VIP/Elite subscription for free,
        no JC charged to anyone. Extends from the current expiry if the user
        already has it active, otherwise starts fresh from now.
        Usage: !grantsub @user <vip|elite> [days]   (days default: 7)"""
        if user is None or tier is None:
            await ctx.reply("**Usage:** `!grantsub @user <vip|elite> [days]`\n**Example:** `!grantsub @user elite 30`")
            return

        item_id, item = self._resolve_sub_tier(tier)
        if item_id is None:
            await ctx.reply("❌ Unknown tier — use `vip` or `elite`.")
            return

        try:
            days_f = float(days)
            if days_f <= 0:
                raise ValueError
        except ValueError:
            await ctx.reply("❌ `days` must be a positive number.")
            return

        new_next_charge, extended = self._grant_or_extend_sub(user.id, item_id, days_f)
        verb = "Extended" if extended else "Granted"
        ts = int(new_next_charge)

        await ctx.reply(
            f"✅ {verb} **{item['name']}** for **{user}** — free of charge, no JC deducted from anyone. "
            f"Active until <t:{ts}:f> (<t:{ts}:R>). "
            f"They'll still need to run `/title {item['name']}` to turn its perks on."
        )
        try:
            await user.send(
                f"🎁 The bot owner {verb.lower()} you **{item['name']}** — it's good until "
                f"<t:{ts}:f>. Run `/title {item['name']}` to activate its perks!"
            )
        except discord.HTTPException:
            pass

    @prefix_grantsub.error
    async def grantsub_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.NotOwner):
            await ctx.reply("🚫 Only the bot owner can use this command.")
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.reply("**Usage:** `!grantsub @user <vip|elite> [days]`\n**Example:** `!grantsub @user vip 14`")

    @app_commands.command(name="grantsub", description="(Owner only) Grant or extend a VIP/Elite subscription for free")
    @app_commands.describe(user="User to grant/extend", tier="vip or elite", days="Number of days to grant/extend (default 7)")
    async def slash_grantsub(self, interaction: discord.Interaction, user: discord.User, tier: str, days: int = 7):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("🚫 Only the bot owner can use this command.", ephemeral=True)
            return
        item_id, item = self._resolve_sub_tier(tier)
        if item_id is None:
            await interaction.response.send_message("❌ Unknown tier — use `vip` or `elite`.", ephemeral=True)
            return
        if days <= 0:
            await interaction.response.send_message("❌ `days` must be a positive number.", ephemeral=True)
            return

        new_next_charge, extended = self._grant_or_extend_sub(user.id, item_id, days)
        verb = "Extended" if extended else "Granted"
        ts = int(new_next_charge)
        embed = discord.Embed(
            description=(
                f"✅ {verb} **{item['name']}** for **{user.display_name}** — free of charge. "
                f"Active until <t:{ts}:f> (<t:{ts}:R>). "
                f"They'll still need to run `/title {item['name']}` to turn its perks on."
            ),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)
        try:
            await user.send(
                f"🎁 The bot owner {verb.lower()} you **{item['name']}** — it's good until "
                f"<t:{ts}:f>. Run `/title {item['name']}` to activate its perks!"
            )
        except discord.HTTPException:
            pass

    @slash_grantsub.autocomplete("tier")
    async def grantsub_tier_autocomplete(self, interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=label, value=value)
            for value, label in (("vip", "VIP"), ("elite", "Elite"))
            if current.lower() in value
        ]


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))