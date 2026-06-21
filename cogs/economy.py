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


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))