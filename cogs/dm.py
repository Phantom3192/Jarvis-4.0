import discord
from discord.ext import commands
from discord import app_commands
from cogs.admin import is_admin

MAX_MSG_LEN = 2000

USAGE = (
    "**Usage:**\n"
    "`!announce @user <message>` — plain text DM\n"
    "`!announce @user --embed <message>` — embedded DM\n"
    "`!announce @user --embed --title \"Title\" --color #ff0000 <message>`"
)

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


# ── Embed builder ─────────────────────────────────────────────────────────────

def _build_embed(title: str | None, message: str, color: str | None) -> discord.Embed:
    try:
        colour = discord.Colour(int(color.lstrip("#"), 16)) if color else discord.Colour.blurple()
    except (ValueError, AttributeError):
        colour = discord.Colour.blurple()

    embed = discord.Embed(
        title=title or "📩 Message",
        description=message,
        color=colour,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="Jarvis")
    return embed


# ── Core send logic ───────────────────────────────────────────────────────────

async def _send_dm(user: discord.User, message: str | None, embed: discord.Embed | None) -> bool:
    try:
        if embed:
            await user.send(embed=embed)
        else:
            await user.send(content=message)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


# ── Flag parser ───────────────────────────────────────────────────────────────

def _parse_flags(args: str) -> tuple[bool, str | None, str | None, str]:
    use_embed = "--embed" in args
    args = args.replace("--embed", "").strip()

    title = None
    color = None

    if "--title" in args:
        try:
            idx = args.index("--title") + len("--title")
            rest = args[idx:].strip()
            if rest.startswith('"'):
                end = rest.index('"', 1)
                title = rest[1:end]
                args = args[:args.index("--title")] + rest[end + 1:]
            else:
                parts = rest.split(None, 1)
                title = parts[0]
                args = args[:args.index("--title")] + (parts[1] if len(parts) > 1 else "")
        except (ValueError, IndexError):
            pass

    if "--color" in args:
        try:
            idx = args.index("--color") + len("--color")
            rest = args[idx:].strip()
            color = rest.split()[0]
            args = args[:args.index("--color")] + rest[len(color):]
        except (ValueError, IndexError):
            pass

    return use_embed, title, color, args.strip()


# ── Cog ───────────────────────────────────────────────────────────────────────

class DM(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="announce")
    async def prefix_dm(self, ctx: commands.Context, *, args: str = None):
        if not is_admin(ctx.author):
            await ctx.reply("🚫 You don't have permission to use this command.")
            return
        if not args:
            await ctx.reply(USAGE)
            return

        tokens = args.split(None, 1)
        if len(tokens) < 2:
            await ctx.reply(USAGE)
            return

        user_token, rest = tokens

        try:
            user = await commands.UserConverter().convert(ctx, user_token)
        except commands.UserNotFound:
            try:
                user = await ctx.bot.fetch_user(int(user_token.strip("<@!>")))
            except (ValueError, discord.NotFound):
                await ctx.reply("❌ User not found. Please @mention them or provide their user ID.")
                return

        use_embed, title, color, message = _parse_flags(rest)

        if not message:
            await ctx.reply("❌ Message cannot be empty.")
            return
        if len(message) > MAX_MSG_LEN:
            await ctx.reply(f"❌ Message too long. Maximum is {MAX_MSG_LEN} characters.")
            return

        embed = _build_embed(title, message, color) if use_embed else None

        preview = discord.Embed(
            title="📋 DM Preview",
            color=discord.Color.yellow(),
            description=f"Sending to **{user}** (`{user.id}`)",
        )
        preview.add_field(name="Format", value="Embed" if use_embed else "Plain text", inline=True)

        view = ConfirmView()

        if use_embed:
            preview.add_field(name="Preview ↓", value="\u200b", inline=False)
            preview_msg = await ctx.reply(embeds=[preview, embed], view=view)
        else:
            preview.add_field(name="Message", value=f"```\n{message[:1000]}\n```", inline=False)
            preview_msg = await ctx.reply(embed=preview, view=view)

        await view.wait()

        if not view.confirmed:
            await preview_msg.edit(content="❌ DM cancelled.", embeds=[], view=None)
            return

        await preview_msg.edit(content=f"📤 Sending DM to **{user}**…", embeds=[], view=None)
        success = await _send_dm(user, message, embed)

        if success:
            await preview_msg.edit(content=f"✅ DM sent to **{user}** (`{user.id}`).")
        else:
            await preview_msg.edit(
                content=f"❌ Couldn't DM **{user}** — they may have DMs disabled or have blocked the bot."
            )

    @app_commands.command(name="announce", description="Send a DM to a specific user as Jarvis")
    @app_commands.describe(
        user="The user to DM",
        message="The message to send",
        format="Plain text or embedded message",
        title="Embed title (only used if format is Embed)",
        color="Embed color as hex, e.g. #ff0000 (only used if format is Embed)",
    )
    @app_commands.choices(format=[
        app_commands.Choice(name="Plain text", value="plain"),
        app_commands.Choice(name="Embed",      value="embed"),
    ])
    async def slash_dm(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        message: str,
        format: str = "plain",
        title: str | None = None,
        color: str | None = None,
    ):
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "🚫 You don't have permission to use this command.", ephemeral=True
            )
            return
        if len(message) > MAX_MSG_LEN:
            await interaction.response.send_message(
                f"❌ Message too long. Maximum is {MAX_MSG_LEN} characters.", ephemeral=True
            )
            return

        use_embed = format == "embed"
        embed = _build_embed(title, message, color) if use_embed else None

        preview = discord.Embed(
            title="📋 DM Preview",
            color=discord.Color.yellow(),
            description=f"Sending to **{user}** (`{user.id}`)",
        )
        preview.add_field(name="Format", value="Embed" if use_embed else "Plain text", inline=True)

        view = ConfirmView()

        if use_embed:
            preview.add_field(name="Preview ↓", value="\u200b", inline=False)
            await interaction.response.send_message(embeds=[preview, embed], view=view, ephemeral=True)
        else:
            preview.add_field(name="Message", value=f"```\n{message[:1000]}\n```", inline=False)
            await interaction.response.send_message(embed=preview, view=view, ephemeral=True)

        await view.wait()

        if not view.confirmed:
            await interaction.edit_original_response(content="❌ DM cancelled.", embeds=[], view=None)
            return

        await interaction.edit_original_response(
            content=f"📤 Sending DM to **{user}**…", embeds=[], view=None
        )
        success = await _send_dm(user, message, embed)

        if success:
            await interaction.edit_original_response(
                content=f"✅ DM sent to **{user}** (`{user.id}`)."
            )
        else:
            await interaction.edit_original_response(
                content=f"❌ Couldn't DM **{user}** — they may have DMs disabled or have blocked the bot."
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(DM(bot))