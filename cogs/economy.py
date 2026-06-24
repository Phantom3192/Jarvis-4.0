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
import discord

from cogs.state import spend_credits

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

from discord.ext import commands
from discord import app_commands
from cogs.state import get_credits, add_credits, get_all_credits


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


LEADERBOARD_SIZE = 10
_MEDALS = ["🥇", "🥈", "🥉"]


async def _leaderboard_embed(bot: commands.Bot) -> discord.Embed:
    """Build an embed showing the top JC holders across the whole bot."""
    balances = get_all_credits()
    ranked = sorted(
        ((uid, bal) for uid, bal in balances.items() if bal > 0),
        key=lambda kv: kv[1],
        reverse=True,
    )[:LEADERBOARD_SIZE]

    embed = discord.Embed(title=f"{JC_EMOJI} Jarvis Credit Leaderboard", color=discord.Color.gold())

    if not ranked:
        embed.description = "Nobody has earned any Jarvis Credits yet!"
        return embed

    lines = []
    for i, (uid, bal) in enumerate(ranked):
        user = bot.get_user(int(uid))
        if user is None:
            try:
                user = await bot.fetch_user(int(uid))
            except discord.HTTPException:
                user = None
        name = user.display_name if user else f"User {uid}"
        rank = _MEDALS[i] if i < len(_MEDALS) else f"**#{i + 1}**"
        lines.append(f"{rank} {name} — **{bal}** {JC_EMOJI}")

    embed.description = "\n".join(lines)
    return embed


class Economy(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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


class Economy(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))