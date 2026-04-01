import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from cogs.state import seen_users
from cogs.admin import is_admin

DM_RATE_LIMIT = 0.04  # ~25 DMs per second — safe for Discord's rate limits

# ── Confirm view ──────────────────────────────────────────────────────────────

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


# ── Delivery ──────────────────────────────────────────────────────────────────

async def _deliver(
    bot: commands.Bot,
    text: str | None,
    embed: discord.Embed | None,
) -> tuple[int, int]:
    """
    DM all seen users. Returns (success_count, fail_count).
    Rate-limited to ~25 DMs/sec.
    """
    success = fail = 0
    for user_id in list(seen_users):
        try:
            user = await bot.fetch_user(user_id)
            if embed:
                await user.send(content=text, embed=embed)
            else:
                await user.send(content=text)
            success += 1
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            fail += 1
        await asyncio.sleep(DM_RATE_LIMIT)
    return success, fail


# ── Embed builder ─────────────────────────────────────────────────────────────

def _build_embed(title: str | None, message: str, color: str | None) -> discord.Embed:
    try:
        colour = discord.Colour(int(color.lstrip("#"), 16)) if color else discord.Colour.blurple()
    except (ValueError, AttributeError):
        colour = discord.Colour.blurple()

    embed = discord.Embed(
        title=title or "📢 Announcement",
        description=message,
        color=colour,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="Jarvis Announcement")
    return embed


# ── Cog ───────────────────────────────────────────────────────────────────────

class Announce(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Prefix command ────────────────────────────────────────────────────────
    # Usage:
    #   !announce <message>
    #   !announce --embed <message>
    #   !announce --embed --title "Title here" --color #ff0000 <message>

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
                "`!global-announce --embed --title \"Your Title\" --color #ff0000 <message>`"
            )
            return

        # Parse flags
        use_embed = "--embed" in args
        args = args.replace("--embed", "").strip()

        title = None
        color = None

        if "--title" in args:
            try:
                title_start = args.index("--title") + len("--title")
                rest = args[title_start:].strip()
                if rest.startswith('"'):
                    title_end = rest.index('"', 1)
                    title = rest[1:title_end]
                    args = args[:args.index("--title")] + rest[title_end + 1:]
                else:
                    parts = rest.split()
                    title = parts[0]
                    args = args[:args.index("--title")] + " ".join(parts[1:])
            except (ValueError, IndexError):
                pass

        if "--color" in args:
            try:
                color_start = args.index("--color") + len("--color")
                rest = args[color_start:].strip()
                color = rest.split()[0]
                args = args[:args.index("--color")] + rest[len(color):]
            except (ValueError, IndexError):
                pass

        message = args.strip()
        if not message:
            await ctx.reply("❌ Message cannot be empty.")
            return

        embed = _build_embed(title, message, color) if use_embed else None
        recipient_count = len(seen_users)

        # Preview
        preview_embed = discord.Embed(
            title="📋 Announcement Preview",
            color=discord.Color.yellow(),
            description=f"This will be sent to **{recipient_count} user(s)**.",
        )
        preview_embed.add_field(name="Format", value="Embed" if use_embed else "Plain text", inline=True)

        view = ConfirmView()

        if use_embed:
            preview_embed.add_field(name="Preview ↓", value="\u200b", inline=False)
            preview_msg = await ctx.reply(embeds=[preview_embed, embed], view=view)
        else:
            preview_embed.add_field(name="Message", value=f"```\n{message[:1000]}\n```", inline=False)
            preview_msg = await ctx.reply(embed=preview_embed, view=view)

        await view.wait()

        if not view.confirmed:
            await preview_msg.edit(
                content="❌ Announcement cancelled.",
                embeds=[],
                view=None,
            )
            return

        await preview_msg.edit(
            content=f"📤 Sending to {recipient_count} user(s)…",
            embeds=[],
            view=None,
        )

        success, fail = await _deliver(self.bot, message if not use_embed else None, embed)

        await preview_msg.edit(
            content=(
                f"📢 **Announcement sent!**\n"
                f"✅ Delivered: **{success}** | ❌ Failed (DMs closed): **{fail}**"
            )
        )

    # ── Slash command ─────────────────────────────────────────────────────────

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

        use_embed = format == "embed"
        embed = _build_embed(title, message, color) if use_embed else None
        recipient_count = len(seen_users)

        # Preview (ephemeral so only admin sees it)
        preview_embed = discord.Embed(
            title="📋 Announcement Preview",
            color=discord.Color.yellow(),
            description=f"This will be sent to **{recipient_count} user(s)**.",
        )
        preview_embed.add_field(name="Format", value="Embed" if use_embed else "Plain text", inline=True)

        view = ConfirmView()

        if use_embed:
            preview_embed.add_field(name="Preview ↓", value="\u200b", inline=False)
            await interaction.response.send_message(
                embeds=[preview_embed, embed], view=view, ephemeral=True
            )
        else:
            preview_embed.add_field(name="Message", value=f"```\n{message[:1000]}\n```", inline=False)
            await interaction.response.send_message(
                embed=preview_embed, view=view, ephemeral=True
            )

        await view.wait()

        if not view.confirmed:
            await interaction.edit_original_response(
                content="❌ Announcement cancelled.", embeds=[], view=None
            )
            return

        await interaction.edit_original_response(
            content=f"📤 Sending to {recipient_count} user(s)…", embeds=[], view=None
        )

        success, fail = await _deliver(
            self.bot,
            message if not use_embed else None,
            embed,
        )

        await interaction.edit_original_response(
            content=(
                f"📢 **Announcement sent!**\n"
                f"✅ Delivered: **{success}** | ❌ Failed (DMs closed): **{fail}**"
            ),
            embeds=[],
            view=None,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Announce(bot))