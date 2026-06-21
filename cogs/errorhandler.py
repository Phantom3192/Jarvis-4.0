import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import re
import traceback
import sys
from cogs.http_session import get_session

def _webhook_url() -> str:
    """Read fresh each time instead of caching at import time, so it
    doesn't matter whether this module is imported before or after
    load_dotenv() runs."""
    return os.getenv("ERROR_WEBHOOK_URL", "")


MAX_TRACEBACK_LEN = 1900

# Lines containing any of these are treated as "error-looking" and forwarded
# to the webhook. Routine info/success logs (✅, reconnect confirmations,
# etc.) are left in the terminal only — no need to ping Discord for those.
_ERROR_MARKERS = ("❌", "error", "Error", "ERROR", "traceback", "Traceback")
_ERROR_RE = re.compile("|".join(re.escape(m) for m in _ERROR_MARKERS))


async def _send_error(title: str, description: str, extra_fields: list[tuple] = None):
    """Send an error embed to the error webhook. Never raises, never prints."""
    url = _webhook_url()
    if not url:
        return
    try:
        session = get_session()
        webhook = discord.Webhook.from_url(url, session=session)
        embed = discord.Embed(
            title       = f"🐛 {title}",
            description = f"```py\n{description[:MAX_TRACEBACK_LEN]}\n```",
            color       = discord.Color.red(),
            timestamp   = discord.utils.utcnow(),
        )
        if extra_fields:
            for name, value in extra_fields:
                embed.add_field(name=name, value=str(value)[:1024], inline=False)
        embed.set_footer(text="Jarvis Error Logger")
        await webhook.send(embed=embed, username="Jarvis Bug Reports")
    except Exception:
        pass  # webhook itself failed — nowhere left to report, stay silent


class _StdoutErrorTee:
    """Wraps stdout so every print() still reaches the terminal as normal,
    but lines that look like errors (❌ / 'error' / 'traceback') are ALSO
    forwarded to the error webhook. Buffers partial writes until a full
    line (ending in \\n) is seen, since print() can call write() more than
    once per statement.
    """

    def __init__(self, real_stdout):
        self._real = real_stdout
        self._buf = ""

    def write(self, text: str):
        self._real.write(text)  # terminal output is never suppressed
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._handle_line(line)

    def flush(self):
        self._real.flush()

    def _handle_line(self, line: str):
        if not _webhook_url():
            return
        if not _ERROR_RE.search(line):
            return
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_send_error("Console Error Log", line))
        except Exception:
            pass  # no running loop yet (e.g. before the bot starts) — terminal still has it

    # Pass through anything else code might expect from a stream object
    def __getattr__(self, name):
        return getattr(self._real, name)


def install_stdout_error_forwarding():
    """Call once, as early as possible in main.py, before any cogs print
    anything. Safe to call even if ERROR_WEBHOOK_URL isn't set — becomes a
    harmless no-op tee in that case (terminal output is untouched either way)."""
    if not isinstance(sys.stdout, _StdoutErrorTee):
        sys.stdout = _StdoutErrorTee(sys.stdout)


class ErrorHandler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        bot.tree.on_error = self.on_app_command_error

        def _asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict):
            exception = context.get("exception")
            message   = context.get("message", "Unknown asyncio error")

            # Ignore routine network noise
            if exception is None:
                loop.create_task(_send_error("Asyncio Error (no exception)", message))
                return

            if isinstance(exception, (ConnectionResetError, asyncio.CancelledError,
                                      discord.Forbidden)):
                return

            tb = "".join(
                traceback.format_exception(type(exception), exception, exception.__traceback__)
            )
            loop.create_task(_send_error("Asyncio Unhandled Exception", tb))

        asyncio.get_running_loop().set_exception_handler(_asyncio_exception_handler)

        original_excepthook = sys.excepthook

        def _custom_excepthook(exc_type, exc_value, exc_tb):
            tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(_send_error("Unhandled Exception", tb))
            except Exception:
                pass
            original_excepthook(exc_type, exc_value, exc_tb)

        sys.excepthook = _custom_excepthook

    # ── Prefix command errors ─────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, (commands.CheckFailure, commands.CommandNotFound)):
            return

        original = getattr(error, "original", error)

        if isinstance(original, discord.Forbidden):
            return  # missing permissions — silent

        tb = "".join(traceback.format_exception(type(original), original, original.__traceback__))

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

    # ── Slash command errors ──────────────────────────────────────────────────

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        if isinstance(error, app_commands.CheckFailure):
            return

        original = getattr(error, "original", error)

        if isinstance(original, discord.Forbidden):
            return  # missing permissions — silent

        tb = "".join(traceback.format_exception(type(original), original, original.__traceback__))

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

    # ── Event listener errors ─────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_error(self, event_method: str, *args, **kwargs):
        tb = traceback.format_exc()
        await _send_error(f"Event Listener Error: `{event_method}`", tb)


async def setup(bot: commands.Bot):
    await bot.add_cog(ErrorHandler(bot))