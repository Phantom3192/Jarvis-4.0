"""
Global-announce cog — DMs all seen users.

OPTIMISATIONS vs original:
- ConfirmView is identical in announce.py and dm.py. Extracted to a shared
  module (cogs/ui_components.py — see that file). Both cogs now import from
  there instead of maintaining duplicate classes.
- _build_embed is also duplicated between announce.py and dm.py. Same fix.
- _deliver: added explicit discord.NotFound to the except tuple (was already
  there implicitly via HTTPException but explicit is clearer).
- preview_embed construction: factored out repeated "Plain text / Embed"
  field into _build_preview_embed() to avoid minor duplication between
  prefix and slash paths.
"""
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from cogs.state import seen_users
from cogs.admin import is_admin
from cogs.ui_components import ConfirmView, build_dm_embed  # shared helpers

DM_RATE_LIMIT = 0.04  # ~25 DMs/sec — safe for Discord rate limits


# ── Delivery ──────────────────────────────────────────────────────────────────

async def _deliver(
    bot: commands.Bot,
    text: str | None,
    embed: discord.Embed | None,
) -> tuple[int, int]:
    """DM all seen users. Returns (success_count, fail_count)."""
    success = fail = 0
    for user_id in list(seen_users):
        try:
            user = await bot.fetch_user(user_id)
            await user.send(content=text, embed=embed)
            success += 1
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            fail += 1
        await asyncio.sleep(DM_RATE_LIMIT)
    return success, fail


# ── Preview embed builder ─────────────────────────────────────────────────────

def _build_preview_embed(
    recipient_count: int,
    use_embed: bool,
    message: str,
    announcement_embed: discord.Embed | None,
) -> tuple[discord.Embed, list[discord.Embed]]:
    """
    Returns (preview_embed, embeds_to_send).
    embeds_to_send is a list so it can be passed directly to send/reply.
    """
    preview = discord.Embed(
        title="📋 Announcement Preview",
        color=discord.Color.yellow(),
        description=f"This will be sent to **{recipient_count} user(s)**.",
    )
    preview.add_field(name="Format", value="Embed" if use_embed else "Plain text", inline=True)

    if use_embed and announcement_embed:
        preview.add_field(name="Preview ↓", value="\u200b", inline=False)
        return preview, [preview, announcement_embed]
    else:
        preview.add_field(name="Message", value=f"```\n{message[:1000]}\n```", inline=False)
        return preview, [preview]


# ── Cog ───────────────────────────────────────────────────────────────────────

class Announce(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="global-announce")
    async def prefix_announce(self, ctx: commands.Context, *, args: str = None):
        if not is_admin(ctx.author):
            await ctx.reply("🚫 You don't have permission to send announcements.")
            return

        if not args:
            await ctx.reply(
                "**Usage:**\n"
                "`!global-announce <message>` — plain text DM\n"
                "`!global-announce --embed <message>` — embedded DM\n"
                '`!global-announce --embed --title "Your Title" --color #ff0000 <message>`'
            )
            return

        use_embed = "--embed" in args
        args = args.replace("--embed", "").strip()
        title, color, message = _parse_announce_flags(args)

        if not message:
            await ctx.reply("❌ Message cannot be empty.")
            return

        embed            = build_dm_embed(title, message, color, default_title="📢 Announcement") if use_embed else None
        recipient_count  = len(seen_users)
        _, embeds        = _build_preview_embed(recipient_count, use_embed, message, embed)

        view        = ConfirmView()
        preview_msg = await ctx.reply(embeds=embeds, view=view)

        await view.wait()

        if not view.confirmed:
            await preview_msg.edit(content="❌ Announcement cancelled.", embeds=[], view=None)
            return

        await preview_msg.edit(content=f"📤 Sending to {recipient_count} user(s)…", embeds=[], view=None)
        success, fail = await _deliver(self.bot, message if not use_embed else None, embed)

        await preview_msg.edit(
            content=(
                f"📢 **Announcement sent!**\n"
                f"✅ Delivered: **{success}** | ❌ Failed (DMs closed): **{fail}**"
            )
        )

    @app_commands.command(name="global-announce", description="Send a DM announcement to all Jarvis users")
    @app_commands.describe(
        message="The announcement message",
        format="Plain text or embedded message",
        title="Embed title (only used if format is Embed)",
        color="Embed color as hex, e.g. #ff0000 (only used if format is Embed)",
    )
    @app_commands.choices(format=[
        app_commands.Choice(name="Plain text", value="plain"),
        app_commands.Choice(name="Embed",      value="embed"),
    ])
    async def slash_announce(
        self,
        interaction: discord.Interaction,
        message: str,
        format: str = "plain",
        title: str | None = None,
        color: str | None = None,
    ):
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "🚫 You don't have permission to send announcements.", ephemeral=True
            )
            return

        use_embed       = format == "embed"
        embed           = build_dm_embed(title, message, color, default_title="📢 Announcement") if use_embed else None
        recipient_count = len(seen_users)
        _, embeds       = _build_preview_embed(recipient_count, use_embed, message, embed)

        view = ConfirmView()
        await interaction.response.send_message(embeds=embeds, view=view, ephemeral=True)
        await view.wait()

        if not view.confirmed:
            await interaction.edit_original_response(content="❌ Announcement cancelled.", embeds=[], view=None)
            return

        await interaction.edit_original_response(
            content=f"📤 Sending to {recipient_count} user(s)…", embeds=[], view=None
        )
        success, fail = await _deliver(self.bot, message if not use_embed else None, embed)

        await interaction.edit_original_response(
            content=(
                f"📢 **Announcement sent!**\n"
                f"✅ Delivered: **{success}** | ❌ Failed (DMs closed): **{fail}**"
            ),
            embeds=[],
            view=None,
        )


def _parse_announce_flags(args: str) -> tuple[str | None, str | None, str]:
    """Extract --title and --color from an argument string. Returns (title, color, remaining)."""
    title = color = None

    if "--title" in args:
        try:
            idx  = args.index("--title") + len("--title")
            rest = args[idx:].strip()
            if rest.startswith('"'):
                end   = rest.index('"', 1)
                title = rest[1:end]
                args  = args[:args.index("--title")] + rest[end + 1:]
            else:
                parts = rest.split(None, 1)
                title = parts[0]
                args  = args[:args.index("--title")] + (parts[1] if len(parts) > 1 else "")
        except (ValueError, IndexError):
            pass

    if "--color" in args:
        try:
            idx   = args.index("--color") + len("--color")
            rest  = args[idx:].strip()
            color = rest.split()[0]
            args  = args[:args.index("--color")] + rest[len(color):]
        except (ValueError, IndexError):
            pass

    return title, color, args.strip()


async def setup(bot: commands.Bot):
    await bot.add_cog(Announce(bot))