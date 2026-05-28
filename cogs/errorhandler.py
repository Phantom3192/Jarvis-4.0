import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import traceback
import sys
from cogs.http_session import get_session

ERROR_WEBHOOK_URL  = os.getenv("ERROR_WEBHOOK_URL", "")
MAX_TRACEBACK_LEN  = 1900


async def _send_error(title: str, description: str, extra_fields: list[tuple] = None):
    """Send an error embed to the configured webhook. Never raises."""
    if not ERROR_WEBHOOK_URL:
        return
    try:
        session = get_session()
        webhook = discord.Webhook.from_url(ERROR_WEBHOOK_URL, session=session)
        embed = discord.Embed(
            title=f"❌ {title}",
            description=f"```py\n{description[:MAX_TRACEBACK_LEN]}\n```",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        if extra_fields:
            for name, value in extra_fields:
                embed.add_field(name=name, value=str(value)[:1024], inline=False)
        embed.set_footer(text="Jarvis Error Logger")
        await webhook.send(embed=embed, username="Jarvis Errors")
    except Exception as e:
        print(f"❌ Error webhook failed: {e}")


class ErrorHandler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        bot.tree.on_error = self.on_app_command_error

        def _asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict):
            exception = context.get("exception")
            message   = context.get("message", "Unknown asyncio error")

            if exception is None:
                print(f"❌ Asyncio error (no exception): {message}")
                loop.create_task(_send_error("Asyncio Error (no exception)", message))
                return

            if isinstance(exception, (ConnectionResetError, asyncio.CancelledError)):
                return

            tb = "".join(
                traceback.format_exception(type(exception), exception, exception.__traceback__)
            )
            print(f"❌ Asyncio unhandled exception: {tb}")
            loop.create_task(_send_error("Asyncio Unhandled Exception", tb))

        asyncio.get_running_loop().set_exception_handler(_asyncio_exception_handler)

        original_excepthook = sys.excepthook

        def _custom_excepthook(exc_type, exc_value, exc_tb):
            tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            print(tb)
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(_send_error("Unhandled Exception", tb))
            except Exception:
                pass
            original_excepthook(exc_type, exc_value, exc_tb)

        sys.excepthook = _custom_excepthook

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, (commands.CheckFailure, commands.CommandNotFound)):
            return

        original = getattr(error, "original", error)
        tb = "".join(traceback.format_exception(type(original), original, original.__traceback__))
        print(f"❌ Command error in '{ctx.command}': {tb}")

        fields = [
            ("Command", f"`{ctx.command}`"),
            ("User",    f"{ctx.author} (`{ctx.author.id}`)"),
            ("Channel", f"{ctx.channel} (`{ctx.channel.id}`)"),
            ("Message", f"```{ctx.message.content[:500]}```"),
        ]
        if ctx.guild:
            fields.append(("Server", f"{ctx.guild.name} (`{ctx.guild.id}`)"))

        await _send_error("Prefix Command Error", tb, fields)

        try:
            await ctx.reply("⚠️ Something went wrong. The error has been reported to the developer.")
        except Exception:
            pass

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        if isinstance(error, app_commands.CheckFailure):
            return

        original = getattr(error, "original", error)
        tb = "".join(traceback.format_exception(type(original), original, original.__traceback__))
        print(f"❌ Slash command error in '{interaction.command}': {tb}")

        fields = [
            ("Command", f"`/{interaction.command.name if interaction.command else 'unknown'}`"),
            ("User",    f"{interaction.user} (`{interaction.user.id}`)"),
            ("Channel", f"{interaction.channel} (`{interaction.channel_id}`)"),
        ]
        if interaction.guild:
            fields.append(("Server", f"{interaction.guild.name} (`{interaction.guild.id}`)"))

        await _send_error("Slash Command Error", tb, fields)

        msg = "⚠️ Something went wrong. The error has been reported to the developer."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_error(self, event_method: str, *args, **kwargs):
        tb = traceback.format_exc()
        print(f"❌ Event error in '{event_method}':\n{tb}")
        await _send_error(f"Event Listener Error: `{event_method}`", tb)


async def setup(bot: commands.Bot):
    await bot.add_cog(ErrorHandler(bot))