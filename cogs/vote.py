import os
import random

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.state import (
    get_vote_stats, bump_vote, claim_vote_box, VOTE_STREAK_MILESTONES,
    get_vote_reminder_enabled, set_vote_reminder_enabled, get_users_due_for_vote_reminder,
    mark_vote_reminder_sent,
)

# The bot's top.gg listing ID. Falls back to the bot's own Discord ID
# (which is what top.gg uses in its vote URLs) if not explicitly set.
TOPGG_BOT_ID = os.getenv("TOPGG_BOT_ID", "")

# Every real vote banks one free "Vote Mystery Box" — same random-payout
# idea as the Referral Mystery Box in economy.py, just with its own range
# so it can be tuned independently. Boosted by VIP/Elite's
# mystery_box_multiplier perk, same as every other box in the shop.
# The box is NOT opened automatically — the user opens it themselves with
# !voteclaim / /voteclaim.
VOTE_BOX_MIN = int(os.getenv("VOTE_BOX_MIN", "50"))
VOTE_BOX_MAX = int(os.getenv("VOTE_BOX_MAX", "200"))


def _vote_url(bot: commands.Bot) -> str:
    bot_id = TOPGG_BOT_ID or (bot.user.id if bot.user else "")
    return f"https://top.gg/bot/{bot_id}/vote"


def record_vote(user_id: int) -> dict:
    """Called by the top.gg webhook the moment a real vote lands. Banks one
    unclaimed Vote Mystery Box and updates streak/total — does NOT grant
    any JC or DM the user. Thin wrapper around state.bump_vote so the
    webhook only needs to import from this cog, not reach into state.py
    directly."""
    return bump_vote(user_id)


def _open_vote_box(user_id: int) -> tuple[int, int]:
    """Rolls a Vote Mystery Box payout and credits it. Caller must already
    have confirmed (via claim_vote_box) that a box was actually pending.
    Returns (reward_jc, new_balance)."""
    from cogs.state import add_credits
    from cogs.economy import get_active_perks

    reward = random.randint(VOTE_BOX_MIN, VOTE_BOX_MAX)
    perks = get_active_perks(user_id)
    reward = round(reward * perks.get("mystery_box_multiplier", 1.0))
    new_balance = add_credits(user_id, reward)
    return reward, new_balance


def _vote_box_embed(user: discord.abc.User, reward: int, new_balance: int) -> discord.Embed:
    """Result embed shown after !voteclaim opens a box — reuses economy.py's
    mystery_box_result_embed for the same jackpot/nice-pull/not-bad flavor
    text as every other box in the game."""
    from cogs.economy import mystery_box_result_embed

    return mystery_box_result_embed(
        user, reward, new_balance,
        box_name="Vote Mystery Box", box_emoji="🗳️",
    )


def _next_milestone(streak: int) -> tuple[int, int] | None:
    """Return (votes_needed, jc_reward) for the next streak milestone the
    user hasn't hit yet, or None if they're past the highest one."""
    for length in sorted(VOTE_STREAK_MILESTONES):
        if streak < length:
            return length, VOTE_STREAK_MILESTONES[length]
    return None


def _vote_embed(bot: commands.Bot, user: discord.abc.User) -> discord.Embed:
    stats = get_vote_stats(user.id)
    total = stats["total_votes"]
    streak = stats["streak"]
    pending = stats["pending_boxes"]
    can_vote_now = stats["can_vote_now"]
    next_vote_ts = stats["next_vote_ts"]

    embed = discord.Embed(
        title="🗳️ Vote for Jarvis!",
        description=(
            "Enjoying Jarvis? Voting on top.gg takes 5 seconds and directly "
            "helps Jarvis get discovered by more servers. Every vote counts!"
        ),
        color=discord.Color.gold() if streak > 0 else discord.Color.blurple(),
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url if bot.user else None)

    if can_vote_now:
        status = "✅ **You can vote right now!**"
    else:
        status = f"⏳ You can vote again <t:{int(next_vote_ts)}:R>"
    embed.add_field(name="Vote status", value=status, inline=False)

    embed.add_field(name="🗳️ Total Votes", value=f"**{total}**", inline=True)
    embed.add_field(
        name="🔥 Current Streak",
        value=f"**{streak}** vote{'s' if streak != 1 else ''} in a row" if streak else "No active streak",
        inline=True,
    )
    embed.add_field(
        name="🎁 Unclaimed Boxes",
        value=f"**{pending}** — use `!voteclaim` / `/voteclaim` to open" if pending else "None right now",
        inline=True,
    )

    nxt = _next_milestone(streak)
    if nxt:
        needed, reward = nxt
        remaining = needed - streak
        embed.add_field(
            name="🏆 Next streak milestone",
            value=f"{remaining} more vote{'s' if remaining != 1 else ''} in a row → **{needed}-streak** bonus of **{reward} JC**",
            inline=False,
        )
    else:
        embed.add_field(name="🏆 Streak milestones", value="You've hit every milestone — legendary! 👑", inline=False)

    if stats["last_vote_ts"]:
        embed.add_field(
            name="Last voted",
            value=f"<t:{int(stats['last_vote_ts'])}:R>",
            inline=False,
        )

    embed.add_field(
        name="🔔 Vote Reminders",
        value=(
            "**ON** — I'll DM you the moment your vote resets"
            if stats["reminder_enabled"]
            else "**OFF** — use the button below to get a DM when you can vote again"
        ),
        inline=False,
    )

    embed.set_footer(text=f"Each vote = a free Vote Mystery Box ({VOTE_BOX_MIN}–{VOTE_BOX_MAX} JC) • Vote within 24h of your last vote to keep your streak alive")
    return embed


class VoteView(discord.ui.View):
    def __init__(self, bot: commands.Bot, author_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.author_id = author_id
        self.add_item(discord.ui.Button(label="Vote on top.gg", style=discord.ButtonStyle.link, url=_vote_url(bot), emoji="🗳️"))
        self._sync_reminder_button()

    def _sync_reminder_button(self):
        enabled = get_vote_reminder_enabled(self.author_id)
        self.reminder_btn.label = "🔔 Reminders: ON" if enabled else "🔕 Reminders: OFF"
        self.reminder_btn.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "⚠️ Run your own `!vote` / `/vote` to toggle your reminders.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="🔔 Reminders", style=discord.ButtonStyle.secondary, row=1)
    async def reminder_btn(self, interaction: discord.Interaction, _):
        set_vote_reminder_enabled(self.author_id, not get_vote_reminder_enabled(self.author_id))
        self._sync_reminder_button()
        await interaction.response.edit_message(embed=_vote_embed(self.bot, interaction.user), view=self)


class Vote(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_vote_reminders.start()

    def cog_unload(self):
        self.check_vote_reminders.cancel()

    @tasks.loop(minutes=5)
    async def check_vote_reminders(self):
        """Polls for users whose vote cooldown just reset and who opted
        into a reminder — DMs each one once per cooldown cycle."""
        for user_id in get_users_due_for_vote_reminder():
            # Mark first so a slow/failed DM (e.g. closed DMs) can never
            # cause this to retry every 5 minutes forever.
            mark_vote_reminder_sent(user_id)
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                if user is None:
                    continue
                embed = discord.Embed(
                    title="🗳️ Your vote for Jarvis is ready again!",
                    description=(
                        "Your top.gg vote cooldown has reset — vote again to keep "
                        "your streak alive and earn another free Vote Mystery Box!"
                    ),
                    color=discord.Color.gold(),
                )
                await user.send(embed=embed, view=VoteView(self.bot, user_id))
            except Exception as e:
                print(f"⚠️ Vote reminder DM failed for {user_id}: {e}")

    @check_vote_reminders.before_loop
    async def _before_check_vote_reminders(self):
        await self.bot.wait_until_ready()

    # ── !vote ──────────────────────────────────────────────────────────────

    @commands.command(name="vote")
    async def prefix_vote(self, ctx: commands.Context):
        """!vote — see your vote streak/count and get the link to vote for Jarvis on top.gg."""
        await ctx.reply(embed=_vote_embed(self.bot, ctx.author), view=VoteView(self.bot, ctx.author.id))

    @app_commands.command(name="vote", description="See your vote streak/count and get the link to vote for Jarvis on top.gg")
    async def slash_vote(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=_vote_embed(self.bot, interaction.user), view=VoteView(self.bot, interaction.user.id))

    # ── !voteclaim ─────────────────────────────────────────────────────────

    @commands.command(name="voteclaim", aliases=["claimvote", "votebox"])
    async def prefix_voteclaim(self, ctx: commands.Context):
        """!voteclaim — open a Vote Mystery Box you earned from voting on top.gg."""
        if not claim_vote_box(ctx.author.id):
            await ctx.reply(
                "📭 You don't have any unclaimed Vote Mystery Boxes right now. "
                "Vote for Jarvis on top.gg to earn one — use `!vote` for the link!"
            )
            return
        reward, new_balance = _open_vote_box(ctx.author.id)
        await ctx.reply(embed=_vote_box_embed(ctx.author, reward, new_balance))

    @app_commands.command(name="voteclaim", description="Open a Vote Mystery Box you earned from voting on top.gg")
    async def slash_voteclaim(self, interaction: discord.Interaction):
        if not claim_vote_box(interaction.user.id):
            await interaction.response.send_message(
                "📭 You don't have any unclaimed Vote Mystery Boxes right now. "
                "Vote for Jarvis on top.gg to earn one — use `/vote` for the link!",
                ephemeral=True,
            )
            return
        reward, new_balance = _open_vote_box(interaction.user.id)
        await interaction.response.send_message(embed=_vote_box_embed(interaction.user, reward, new_balance))


async def setup(bot: commands.Bot):
    await bot.add_cog(Vote(bot))