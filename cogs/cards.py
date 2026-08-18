"""
Beast Cards — collectible card feature.

Cards are NOT purchasable/pullable yet — acquisition method is still being
decided. For now the only way a card enters circulation is the owner-only
!givecard command. Duplicates stack as a quantity rather than separate
items (see state.py's user_cards store), and cards can be traded directly
between two users via a mutual accept/decline flow.

Commands:
  /cardinfo    — view full details on one specific card
  /cards       — view your (or someone else's) collection
  /carddex     — browse the full set of collectible cards and their rarity
  /cardtrade   — offer one of your cards to another user in exchange for JC
                 (or a straight gift if asking_price is 0)
  !givecard    — owner only, grants a card to a user (prefix-only, placeholder
                 acquisition path until packs/drops/quests are designed)

Follows the same patterns as cogs/economy.py: state.py owns persistence,
this cog owns commands/UI, and pending trade offers are tracked in an
in-memory dict keyed by (sender_id, recipient_id) the same way
_pending_sub_gifts tracks subscription gifts.
"""
import time

import discord
from discord import app_commands
from discord.ext import commands

from cogs.state import (
    spend_credits, add_credits,
    get_user_cards, get_card_quantity, add_card, remove_card, transfer_card,
)

JC_EMOJI = "🪙"
JC_NAME = "Jarvis Credit"

# ── Rarity tiers ─────────────────────────────────────────────────────────────
# weight = relative pull chance (higher = more common). Color used for embeds.
RARITIES = {
    "Common":    {"weight": 60, "color": discord.Color.light_gray()},
    "Rare":      {"weight": 25, "color": discord.Color.blue()},
    "Epic":      {"weight": 12, "color": discord.Color.purple()},
    "Legendary": {"weight": 3,  "color": discord.Color.gold()},
}

# ── Card set ───────────────────────────────────────────────────────────────
# id is stable and used as the storage key — never rename an existing id,
# add a new one instead, or every user's existing inventory breaks.
CARD_DEFS: dict[str, dict] = {
    # Common
    "emberfox":     {"name": "Emberfox",     "rarity": "Common", "power": 24, "emoji": "🦊", "flavor": "A small fox wreathed in low flame. Curls up in warm places."},
    "mossback":     {"name": "Mossback",     "rarity": "Common", "power": 20, "emoji": "🐢", "flavor": "Moves slow. Plants grow on its shell."},
    "duskwing":     {"name": "Duskwing",     "rarity": "Common", "power": 22, "emoji": "🦋", "flavor": "A moth-guardian that watches over sleeping things."},
    "puddlejack":   {"name": "Puddlejack",   "rarity": "Common", "power": 18, "emoji": "🐸", "flavor": "Mischievous frog-thing that lives in rain puddles."},
    "thistlepup":   {"name": "Thistlepup",   "rarity": "Common", "power": 21, "emoji": "🐕", "flavor": "Small thorny dog-beast, prickly but loyal."},
    "driftmoth":    {"name": "Driftmoth",    "rarity": "Common", "power": 19, "emoji": "🕯️", "flavor": "Pale moth that follows lanterns and campfires."},
    # Rare
    "tideling":     {"name": "Tideling",     "rarity": "Rare", "power": 51, "emoji": "💧", "flavor": "A water sprite that only appears during storms."},
    "grumblehorn":  {"name": "Grumblehorn",  "rarity": "Rare", "power": 48, "emoji": "🐐", "flavor": "Grumpy horned beast, surprisingly loyal once tamed."},
    "ashclaw":      {"name": "Ashclaw",      "rarity": "Rare", "power": 53, "emoji": "🐻", "flavor": "Leaves scorch-mark pawprints wherever it walks."},
    "whistlefawn":  {"name": "Whistlefawn",  "rarity": "Rare", "power": 46, "emoji": "🦌", "flavor": "Its call sounds like a half-remembered song."},
    "barkhollow":   {"name": "Barkhollow",   "rarity": "Rare", "power": 44, "emoji": "🌳", "flavor": "A hollow trunk hides small creatures inside."},
    # Epic
    "stormhide":    {"name": "Stormhide",    "rarity": "Epic", "power": 76, "emoji": "🐺", "flavor": "A wolf wrapped in constant thunder. Rarely seen up close."},
    "glasswing":    {"name": "Glasswing",    "rarity": "Epic", "power": 71, "emoji": "🪰", "flavor": "Crystalline wings that chime in the wind."},
    "hollowmaw":    {"name": "Hollowmaw",    "rarity": "Epic", "power": 79, "emoji": "🕳️", "flavor": "Said to eat sound itself."},
    "emberdrake":   {"name": "Emberdrake",   "rarity": "Epic", "power": 74, "emoji": "🐉", "flavor": "A small dragon with embers instead of scales."},
    # Legendary
    "hollowking":   {"name": "The Hollow King", "rarity": "Legendary", "power": 96, "emoji": "🦌", "flavor": "An ancient stag — its antlers host an entire ecosystem."},
    "wyrmshade":    {"name": "Wyrmshade",       "rarity": "Legendary", "power": 94, "emoji": "🐍", "flavor": "As old as the mountains. Rarely wakes."},
    "sunmother":    {"name": "Sunmother",       "rarity": "Legendary", "power": 98, "emoji": "🔥", "flavor": "A phoenix-like beast, said to be reborn with every dawn."},
}

_RARITY_ORDER = ["Common", "Rare", "Epic", "Legendary"]


def _card_line(card_id: str, qty: int) -> str:
    card = CARD_DEFS[card_id]
    return f"{card['emoji']} **{card['name']}** ({card['rarity']}) — x{qty}"


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


def _card_info_embed(card_query: str, *, viewer_id: int) -> discord.Embed:
    """Look up a single card by name (case-insensitive) and build a detail
    embed for it, including how many copies the viewer personally owns."""
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
        return embed

    card_id, card = match
    owned = get_card_quantity(viewer_id, card_id)

    embed = discord.Embed(
        title=f"{card['emoji']} {card['name']}",
        description=f"*{card['flavor']}*",
        color=RARITIES[card["rarity"]]["color"],
    )
    embed.add_field(name="Rarity", value=card["rarity"], inline=True)
    embed.add_field(name="Power", value=str(card["power"]), inline=True)
    embed.add_field(name="You own", value=f"x{owned}", inline=True)
    return embed


def _dex_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📖 Beast Card Dex",
        description=f"All {len(CARD_DEFS)} collectible cards. Use `/cardinfo <name>` for details on one.",
        color=discord.Color.blurple(),
    )
    for rarity in _RARITY_ORDER:
        cards_in_tier = [c for c in CARD_DEFS.values() if c["rarity"] == rarity]
        weight = RARITIES[rarity]["weight"]
        total_weight = sum(r["weight"] for r in RARITIES.values())
        pct = weight / total_weight * 100
        lines = [f"{c['emoji']} **{c['name']}** — {c['flavor']}" for c in cards_in_tier]
        embed.add_field(name=f"{rarity} (~{pct:.0f}% pull chance)", value="\n".join(lines), inline=False)
    return embed


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
                    f"**{self.recipient.display_name}** received {card['emoji']} **{card['name']}** "
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


class Cards(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── !cardinfo / /cardinfo ────────────────────────────────────────────
    @commands.command(name="cardinfo", aliases=["cinfo"])
    async def prefix_cardinfo(self, ctx: commands.Context, *, card: str):
        """!cardinfo <card name> — view full details on one specific card."""
        embed = _card_info_embed(card, viewer_id=ctx.author.id)
        await ctx.reply(embed=embed)

    @app_commands.command(name="cardinfo", description="View full details on one specific card")
    @app_commands.describe(card="The card's name, e.g. 'Emberfox'")
    async def slash_cardinfo(self, interaction: discord.Interaction, card: str):
        embed = _card_info_embed(card, viewer_id=interaction.user.id)
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

    # ── !carddex / /carddex ──────────────────────────────────────────────
    @commands.command(name="carddex", aliases=["dex"])
    async def prefix_carddex(self, ctx: commands.Context):
        """!carddex / !dex — browse the full set of collectible beast cards."""
        await ctx.reply(embed=_dex_embed())

    @app_commands.command(name="carddex", description="Browse the full set of collectible beast cards")
    async def slash_carddex(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=_dex_embed())

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
                f"**{sender.display_name}** wants to send you {card_def['emoji']} "
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
        Example: !cardtrade @Someone Emberfox 2 50"""
        amount = max(1, min(amount, 99))
        asking_price = max(0, asking_price)

        async def respond(content=None, embed=None, view=None, ephemeral=False):
            return await ctx.reply(content=content, embed=embed, view=view)

        await self._start_trade(respond, ctx.author, recipient, card, amount, asking_price)

    @app_commands.command(name="cardtrade", description="Offer a card to another user, optionally for a JC price")
    @app_commands.describe(
        recipient="Who to send the trade offer to",
        card="Which card to offer (use its name, e.g. 'Emberfox')",
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
    # Acquisition method (packs, drops, quests, etc.) isn't decided yet —
    # this is the placeholder way cards enter circulation until that's
    # designed. Prefix-only and owner-gated on purpose.
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
        await ctx.reply(
            f"✅ Gave {card_def['emoji']} **{card_def['name']}** ({card_def['rarity']}) to "
            f"**{user.display_name}** — they now own **x{new_qty}**."
        )

    @prefix_givecard.error
    async def givecard_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.NotOwner):
            await ctx.reply("🚫 Only the bot owner can use this command.")
        elif isinstance(error, (commands.MissingRequiredArgument, commands.UserNotFound, commands.BadArgument)):
            await ctx.reply("**Usage:** `!givecard @user <card name>`\n**Example:** `!givecard @Someone Emberfox`")


async def setup(bot: commands.Bot):
    await bot.add_cog(Cards(bot))