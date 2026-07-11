"""
Terms & Conditions gate — no standalone !tos/!terms command. The prompt
only ever appears automatically, the first time someone who hasn't
accepted yet tries to use Jarvis (a command, or a chat trigger). The actual
Terms & Conditions TEXT lives on your own website (set JARVIS_TOS_URL
below) — this module just links to it and owns the Accept/Decline UI.

Two message variants:
  - Brand-new users (no footprint in Jarvis's data at all) get the normal
    "welcome, please accept our Terms & Conditions" framing.
  - Users who were already using Jarvis before this feature shipped get a
    "we've introduced new Terms & Conditions" framing instead, via
    state.has_used_bot_before().

Enforcement is wired in from two places so it covers every way someone can
talk to Jarvis:
  - main.py's global_ban_check (prefix commands) and slash_interaction_check
    (slash commands) call ensure_tos() before letting a command run.
  - cogs/ai.py's on_message calls ensure_tos() before generating a free-text
    chat reply (mention / reply / "jarvis" / DM / auto-respond channel).

Both call sites share this one ensure_tos() helper so the prompt and button
behaviour can never drift out of sync between them.
"""
import os

import discord

from cogs.state import has_accepted_tos, accept_tos, has_used_bot_before, TOS_VERSION


def _tos_url() -> str:
    """Read lazily (not as a module-level constant) so it never depends on
    whether load_dotenv() has already run by the time this module was first
    imported — set via the JARVIS_TOS_URL env var, or edit the fallback
    string below."""
    return os.getenv("JARVIS_TOS_URL", "https://example.com/jarvis-terms")


def build_tos_embed(existing_user: bool) -> discord.Embed:
    url = _tos_url()
    if existing_user:
        title = "📢 Jarvis — New Terms & Conditions"
        body = (
            "You've already been using Jarvis — thanks for that! We've now introduced "
            "official Terms & Conditions, and everyone needs to accept them once to "
            "keep using Jarvis:\n\n"
            f"🔗 **[Read the Terms & Conditions]({url})**\n\n"
            "Tap **Accept** below once you've read them to carry on as normal, or "
            "**Decline** if you don't agree — you won't be able to use Jarvis until "
            "you accept."
        )
    else:
        title = "📜 Welcome to Jarvis — Terms & Conditions"
        body = (
            "Before you can use Jarvis, please read our Terms & Conditions:\n\n"
            f"🔗 **[Read the Terms & Conditions]({url})**\n\n"
            "Tap **Accept** below once you've read them to continue, or **Decline** "
            "if you don't agree — you won't be able to use Jarvis until you accept."
        )

    embed = discord.Embed(title=title, description=body, color=discord.Color.blurple())
    embed.set_footer(text=f"Terms version {TOS_VERSION} • You only need to do this once.")
    return embed


class TOSView(discord.ui.View):
    """Persistent view (no timeout, fixed custom_ids) so Accept/Decline keep
    working even if the bot restarts between sending this and the user
    tapping a button."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Read the Terms", url=_tos_url(), style=discord.ButtonStyle.link, emoji="🔗"))

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅", custom_id="jarvis_tos_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        accept_tos(interaction.user.id)
        embed = discord.Embed(
            title="✅ Thanks!",
            description="You've accepted the Terms & Conditions — you're all set. Go ahead and use Jarvis!",
            color=discord.Color.green(),
        )
        try:
            await interaction.response.edit_message(embed=embed, view=None)
        except discord.HTTPException:
            # Original message may not be editable by this interaction
            # (e.g. it was a message.reply(), not an interaction response).
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="✖️", custom_id="jarvis_tos_decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="✖️ Terms declined",
            description=(
                "You've declined the Terms & Conditions, so Jarvis can't respond to you "
                "yet. Use any command or message Jarvis again whenever you change your "
                "mind — you'll see this prompt again."
            ),
            color=discord.Color.red(),
        )
        try:
            await interaction.response.edit_message(embed=embed, view=None)
        except discord.HTTPException:
            await interaction.response.send_message(embed=embed, ephemeral=True)


async def ensure_tos(user_id: int, send) -> bool:
    """Gate helper shared by every entry point (prefix commands, slash
    commands, free-text chat). `send` is an async callable accepting
    (embed=..., view=...) that posts the prompt however fits that context
    (ctx.reply, interaction.response.send_message, message.reply, ...).

    Returns True if the user has already accepted and the caller should
    proceed normally. Returns False if the prompt was just shown — the
    caller must stop and not do any further work for this call.
    """
    if has_accepted_tos(user_id):
        return True
    existing_user = has_used_bot_before(user_id)
    await send(embed=build_tos_embed(existing_user), view=TOSView())
    return False


async def setup(bot):
    bot.add_view(TOSView())  # register persistent custom_ids so old prompts still work after a restart