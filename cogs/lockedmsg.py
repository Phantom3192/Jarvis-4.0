"""
Locked message cog — `/lock` (preferred) or `!lock @user <message>` posts a
message in the channel that only the mentioned user can open. Anyone else who
presses the button gets told it isn't for them.

HOW IT WORKS:
- The message content is stored server-side (cogs.state, Turso-backed) keyed
  by a short random `lock_id`. Only that id — never the content — goes into
  the button's custom_id, so the content never round-trips through Discord
  in a way anyone but the recipient can read.
- Reveals happen via an ephemeral response, visible only to whoever clicked.
- The button is handled through a raw `on_interaction` listener matching the
  "lockmsg:reveal:" custom_id prefix (rather than a bound View callback), so
  it keeps working even after a bot restart — no persistent-view
  registration/bookkeeping needed, since discord.py routes every component
  interaction through on_interaction regardless of whether the original View
  object is still alive.

WHY /lock OPENS A POPUP:
Discord always shows the parameters someone typed for a slash command right
above the bot's response ("Phantom used /lock  user: @Lucky  message: Hi") —
that's Discord's own UI chrome, not something a bot can suppress. If the
message were a normal slash-command parameter, it would leak in that header
to everyone in the channel, defeating the whole point of it being "locked".
So `/lock` only takes `user` as a parameter, then opens a modal (a private
popup text box) to collect the actual message — modal input is never shown
in the channel or in that "used /lock" summary, only the finished locked
card is. `!lock` is kept for convenience, but text commands are typed
directly into the channel, so there's no way to make its message private —
see the note on prefix_lock below.
"""
import uuid

import discord
from discord.ext import commands
from discord import app_commands

from cogs import state

MAX_MSG_LEN = 1500
NOT_FOR_YOU = "🔒 This message isn't meant for you."

USAGE = (
    "**Usage:** `!lock @user <message>`\n"
    "Posts a locked message in this channel — only the mentioned user can open it.\n"
    "⚠️ Heads up: with `!lock`, your message is typed in the open channel first, "
    "so anyone watching at that moment could glimpse it. Use `/lock` instead for "
    "a private popup that never shows the text in the channel."
)


# ── Embed / view builders ───────────────────────────────────────────────────

def _closed_embed(author: discord.abc.User, target: discord.abc.User) -> discord.Embed:
    embed = discord.Embed(
        title="🔒 Locked Message",
        description=(
            f"**{author}** sent a locked message for {target.mention}.\n"
            f"Only they can open it."
        ),
        color=discord.Color.dark_purple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="Jarvis • Locked Message")
    return embed


def _opened_embed(author: discord.abc.User, target: discord.abc.User) -> discord.Embed:
    embed = discord.Embed(
        title="🔓 Locked Message — opened",
        description=f"**{author}**'s locked message for {target.mention} has been opened.",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="Jarvis • Locked Message")
    return embed


def _closed_view(lock_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="Reveal",
        emoji="🔓",
        style=discord.ButtonStyle.primary,
        custom_id=f"lockmsg:reveal:{lock_id}",
    ))
    return view


def _opened_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="Opened",
        emoji="✅",
        style=discord.ButtonStyle.secondary,
        disabled=True,
    ))
    return view


# ── Modal (private popup used by /lock) ─────────────────────────────────────

class _LockModal(discord.ui.Modal, title="Locked message"):
    """Collects the message text privately. discord.py never surfaces modal
    field values anywhere but this submit interaction — not in the channel,
    not in the "used /lock" summary — so this is the only path that keeps
    the content actually hidden from other people in the channel."""

    message_input = discord.ui.TextInput(
        label="Message",
        style=discord.TextStyle.paragraph,
        placeholder="Type the message only they should see...",
        max_length=MAX_MSG_LEN,
        required=True,
    )

    def __init__(self, cog: "LockedMessage", target: discord.abc.User):
        super().__init__()
        self.cog = cog
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):
        result = await self.cog._create(interaction.user, self.target, str(self.message_input.value))
        if isinstance(result, str):
            await interaction.response.send_message(result, ephemeral=True)
            return

        embed, view = result
        await interaction.response.send_message(embed=embed, view=view)


# ── Cog ──────────────────────────────────────────────────────────────────────

class LockedMessage(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -- shared creation logic, used by both the prefix and slash commands --
    async def _create(
        self,
        author: discord.abc.User,
        target: discord.abc.User,
        message: str,
    ) -> tuple[discord.Embed, discord.ui.View] | str:
        """Returns (embed, view) to post on success, or an error string."""
        if target.bot:
            return "❌ You can't send a locked message to a bot."
        if target.id == author.id:
            return "❌ You can't lock a message to yourself."
        if not message or not message.strip():
            return "❌ Message cannot be empty."
        if len(message) > MAX_MSG_LEN:
            return f"❌ Message too long. Maximum is {MAX_MSG_LEN} characters."

        lock_id = uuid.uuid4().hex[:16]
        state.create_locked_message(lock_id, author.id, target.id, message.strip())

        return _closed_embed(author, target), _closed_view(lock_id)

    # -- button handling, restart-safe (see module docstring) --
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return

        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith("lockmsg:reveal:"):
            return

        lock_id = custom_id.split(":", 2)[2]
        entry = state.get_locked_message(lock_id)

        if entry is None:
            await interaction.response.send_message(
                "❌ This locked message no longer exists.", ephemeral=True
            )
            return

        if interaction.user.id != entry["target"]:
            await interaction.response.send_message(NOT_FOR_YOU, ephemeral=True)
            return

        already_opened = entry["opened"]
        state.mark_locked_message_opened(lock_id)

        await interaction.response.send_message(
            f"🔓 **Locked message from <@{entry['author']}>:**\n\n{entry['content']}",
            ephemeral=True,
        )

        # Flip the public embed/button to "opened" the first time it's revealed.
        if not already_opened and interaction.message is not None:
            try:
                author_user = self.bot.get_user(entry["author"]) or await self.bot.fetch_user(entry["author"])
                await interaction.message.edit(
                    embed=_opened_embed(author_user, interaction.user),
                    view=_opened_view(),
                )
            except (discord.NotFound, discord.HTTPException):
                pass

    # -- prefix command --
    @commands.command(name="lock", aliases=["lockmsg"])
    async def prefix_lock(self, ctx: commands.Context, *, args: str = None):
        if not args:
            await ctx.reply(USAGE)
            return

        tokens = args.split(None, 1)
        if len(tokens) < 2:
            await ctx.reply(USAGE)
            return

        user_token, message = tokens
        try:
            target = await commands.MemberConverter().convert(ctx, user_token)
        except commands.MemberNotFound:
            try:
                target = await self.bot.fetch_user(int(user_token.strip("<@!>")))
            except (ValueError, discord.NotFound):
                await ctx.reply("❌ User not found. Please @mention them or provide their user ID.")
                return

        result = await self._create(ctx.author, target, message)

        # Best-effort: remove the original "!lock @user <message>" message so
        # the plaintext doesn't linger in the channel history. Silently
        # skipped if the bot lacks Manage Messages — the locked card still
        # posts either way, just with the note in USAGE about the typed
        # message having briefly been visible.
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass

        if isinstance(result, str):
            await ctx.send(result)
            return

        embed, view = result
        await ctx.send(embed=embed, view=view)

    # -- slash command: takes only `user`, then opens a private popup for the
    #    message text (see WHY /lock OPENS A POPUP in the module docstring) --
    @app_commands.command(name="lock", description="Send a locked message that only the chosen user can open")
    @app_commands.describe(user="Who this message is for — only they'll be able to open it")
    async def slash_lock(self, interaction: discord.Interaction, user: discord.User):
        if user.bot:
            await interaction.response.send_message(
                "❌ You can't send a locked message to a bot.", ephemeral=True
            )
            return
        if user.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ You can't lock a message to yourself.", ephemeral=True
            )
            return

        await interaction.response.send_modal(_LockModal(self, user))


async def setup(bot: commands.Bot):
    await bot.add_cog(LockedMessage(bot))