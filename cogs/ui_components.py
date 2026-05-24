"""
Shared UI components used by announce.py and dm.py.

WHY THIS FILE EXISTS:
Both announce.py and dm.py defined identical ConfirmView and _build_embed()
functions.  Any bug fix or style change had to be made in two places.
This module is the single source of truth for both.
"""
import discord


# ── Shared confirm view ───────────────────────────────────────────────────────

class ConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.confirmed: bool | None = None

    @discord.ui.button(label="Send", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stop()
        await interaction.response.defer()


# ── Shared embed builder ──────────────────────────────────────────────────────

def build_dm_embed(
    title: str | None,
    message: str,
    color: str | None,
    *,
    default_title: str = "📩 Message",
) -> discord.Embed:
    """
    Build a Discord embed for DM / announcement use.

    Args:
        title:         Optional custom title. Falls back to default_title.
        message:       The embed description / body.
        color:         Hex colour string like '#ff0000'. Falls back to blurple.
        default_title: The fallback title if title is None.
    """
    try:
        colour = discord.Colour(int(color.lstrip("#"), 16)) if color else discord.Colour.blurple()
    except (ValueError, AttributeError):
        colour = discord.Colour.blurple()

    embed = discord.Embed(
        title=title or default_title,
        description=message,
        color=colour,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="Jarvis")
    return embed