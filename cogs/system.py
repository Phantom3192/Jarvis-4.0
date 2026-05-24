"""
System cog — host resource usage, bot reload, ping, and uptime.

Commands
--------
!ping / /ping        — Latency + Discord API round-trip. Available to all users.
!uptime / /uptime    — How long the bot has been running. Available to all users.
!usage / /usage      — Full CPU/RAM/disk/process stats. Admin only.
!reload / /reload    — Reload all cogs + GC. Admin only.
"""
import gc
import os
import time
import discord
from discord.ext import commands
from discord import app_commands

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

from cogs.admin import is_admin
from cogs.state import seen_users

# Set once at import — survives cog reloads because the module stays in sys.modules
_START_TIME = time.monotonic()

# Must stay in sync with COGS in main.py
COGS = [
    "cogs.ai",
    "cogs.admin",
    "cogs.stats",
    "cogs.prompts",
    "cogs.announce",
    "cogs.dm",
    "cogs.suggestions",
    "cogs.bugreport",
    "cogs.errorhandler",
    "cogs.help",
    "cogs.fun",
    "cogs.imagine",
    "cogs.system",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_bytes(n: int) -> str:
    if n >= 1_073_741_824:
        return f"{n / 1_073_741_824:.1f} GB"
    return f"{n / 1_048_576:.1f} MB"


def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _container_memory() -> tuple[int, int] | None:
    """
    Read cgroup memory limits (Pterodactyl / Docker).
    Returns (used_bytes, limit_bytes) or None if not containerised.
    Tries cgroup v2 first, then v1.
    """
    try:
        used  = int(open("/sys/fs/cgroup/memory.current").read().strip())
        limit = open("/sys/fs/cgroup/memory.max").read().strip()
        if limit != "max":
            return used, int(limit)
    except (OSError, ValueError):
        pass
    try:
        used  = int(open("/sys/fs/cgroup/memory/memory.usage_in_bytes").read().strip())
        limit = int(open("/sys/fs/cgroup/memory/memory.limit_in_bytes").read().strip())
        if limit < (1 << 62):
            return used, limit
    except (OSError, ValueError):
        pass
    return None


def _ping_colour(ms: float) -> discord.Color:
    if ms < 100:
        return discord.Color.green()
    if ms < 200:
        return discord.Color.yellow()
    return discord.Color.red()


def _build_usage_embed(bot: commands.Bot) -> discord.Embed:
    embed = discord.Embed(
        title="🖥️ Jarvis — System Usage",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    uptime = time.monotonic() - _START_TIME
    embed.add_field(name="⏱️ Uptime",     value=f"`{_fmt_uptime(uptime)}`", inline=True)
    embed.add_field(name="🌐 Guilds",     value=f"`{len(bot.guilds)}`",     inline=True)
    embed.add_field(name="👤 Seen Users", value=f"`{len(seen_users):,}`",   inline=True)

    if _PSUTIL:
        proc    = psutil.Process(os.getpid())
        cpu_pct = psutil.cpu_percent(interval=None)
        vm      = psutil.virtual_memory()
        disk    = psutil.disk_usage("/")

        cgroup = _container_memory()
        if cgroup:
            ram_used, ram_total = cgroup
            ram_pct   = ram_used / ram_total * 100
            ram_label = "💾 Container RAM"
        else:
            ram_used, ram_total, ram_pct = vm.used, vm.total, vm.percent
            ram_label = "💾 Host RAM"

        embed.add_field(name="🔲 CPU",   value=f"`{cpu_pct:.1f}%`", inline=True)
        embed.add_field(
            name=ram_label,
            value=f"`{_fmt_bytes(ram_used)} / {_fmt_bytes(ram_total)}` ({ram_pct:.1f}%)",
            inline=True,
        )
        embed.add_field(
            name="💿 Disk",
            value=f"`{_fmt_bytes(disk.used)} / {_fmt_bytes(disk.total)}` ({disk.percent:.1f}%)",
            inline=True,
        )

        with proc.oneshot():
            proc_mem = proc.memory_info().rss
            proc_cpu = proc.cpu_percent(interval=None)
            threads  = proc.num_threads()

        embed.add_field(name="🤖 Bot RSS", value=f"`{_fmt_bytes(proc_mem)}`", inline=True)
        embed.add_field(name="🤖 Bot CPU", value=f"`{proc_cpu:.1f}%`",        inline=True)
        embed.add_field(name="🧵 Threads", value=f"`{threads}`",               inline=True)
    else:
        embed.add_field(
            name="⚠️ psutil not installed",
            value="Run `pip install psutil` to enable CPU/RAM metrics.",
            inline=False,
        )

    embed.set_footer(text="Jarvis System  •  Admin only")
    return embed


# ── Reload logic ──────────────────────────────────────────────────────────────

async def _do_reload(bot: commands.Bot) -> str:
    collected = gc.collect()
    bot._connection._messages.clear()

    ok: list[str] = []
    fail: list[str] = []

    for cog in COGS:
        try:
            await bot.reload_extension(cog)
            ok.append(cog.split(".")[-1])
        except commands.ExtensionNotLoaded:
            try:
                await bot.load_extension(cog)
                ok.append(cog.split(".")[-1])
            except Exception as e:
                fail.append(f"`{cog.split('.')[-1]}` — {e}")
        except Exception as e:
            fail.append(f"`{cog.split('.')[-1]}` — {e}")

    lines = [
        f"✅ Reloaded **{len(ok)}/{len(COGS)}** cog(s)",
        f"🗑️ GC collected **{collected}** object(s)",
    ]
    if fail:
        lines.append("❌ Failed:\n" + "\n".join(f"  • {f}" for f in fail))
    return "\n".join(lines)


# ── Cog ───────────────────────────────────────────────────────────────────────

class System(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── !ping ─────────────────────────────────────────────────────────────────

    @commands.command(name="ping")
    async def prefix_ping(self, ctx: commands.Context):
        """Check Jarvis latency."""
        ws_ms  = round(self.bot.latency * 1000)
        before = time.monotonic()
        msg    = await ctx.reply("🏓 Pinging…")
        api_ms = round((time.monotonic() - before) * 1000)

        embed = discord.Embed(title="🏓 Pong!", color=_ping_colour(ws_ms))
        embed.add_field(name="WebSocket",    value=f"`{ws_ms} ms`",  inline=True)
        embed.add_field(name="API Round-trip", value=f"`{api_ms} ms`", inline=True)
        await msg.edit(content=None, embed=embed)

    @app_commands.command(name="ping", description="Check Jarvis latency")
    async def slash_ping(self, interaction: discord.Interaction):
        ws_ms  = round(self.bot.latency * 1000)
        before = time.monotonic()
        await interaction.response.send_message("🏓 Pinging…")
        api_ms = round((time.monotonic() - before) * 1000)

        embed = discord.Embed(title="🏓 Pong!", color=_ping_colour(ws_ms))
        embed.add_field(name="WebSocket",      value=f"`{ws_ms} ms`",  inline=True)
        embed.add_field(name="API Round-trip", value=f"`{api_ms} ms`", inline=True)
        await interaction.edit_original_response(content=None, embed=embed)

    # ── !uptime ───────────────────────────────────────────────────────────────

    @commands.command(name="uptime")
    async def prefix_uptime(self, ctx: commands.Context):
        """Check how long Jarvis has been running."""
        uptime = time.monotonic() - _START_TIME
        embed  = discord.Embed(
            title="⏱️ Jarvis Uptime",
            description=f"Online for **{_fmt_uptime(uptime)}**",
            color=discord.Color.green(),
        )
        embed.set_footer(text="Jarvis")
        await ctx.reply(embed=embed)

    @app_commands.command(name="uptime", description="Check how long Jarvis has been online")
    async def slash_uptime(self, interaction: discord.Interaction):
        uptime = time.monotonic() - _START_TIME
        embed  = discord.Embed(
            title="⏱️ Jarvis Uptime",
            description=f"Online for **{_fmt_uptime(uptime)}**",
            color=discord.Color.green(),
        )
        embed.set_footer(text="Jarvis")
        await interaction.response.send_message(embed=embed)

    # ── !usage ────────────────────────────────────────────────────────────────

    @commands.command(name="usage")
    async def prefix_usage(self, ctx: commands.Context):
        """Show host CPU, RAM, disk, and bot process stats. Admin only."""
        if not is_admin(ctx.author):
            await ctx.reply("🚫 You don't have permission to use this command.")
            return
        await ctx.reply(embed=_build_usage_embed(self.bot))

    @app_commands.command(name="usage", description="Show CPU, RAM, disk and bot stats (admin only)")
    async def slash_usage(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "🚫 You don't have permission to use this command.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=_build_usage_embed(self.bot), ephemeral=True
        )

    # ── !reload ───────────────────────────────────────────────────────────────

    @commands.command(name="reload")
    async def prefix_reload(self, ctx: commands.Context):
        """Reload all cogs and run garbage collection. Admin only."""
        if not is_admin(ctx.author):
            await ctx.reply("🚫 You don't have permission to use this command.")
            return
        msg = await ctx.reply("🔄 Reloading…")
        result = await _do_reload(self.bot)
        await msg.edit(content=result)

    @app_commands.command(name="reload", description="Reload all cogs and clear memory cache (admin only)")
    async def slash_reload(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "🚫 You don't have permission to use this command.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        result = await _do_reload(self.bot)
        await interaction.followup.send(result)


async def setup(bot: commands.Bot):
    await bot.add_cog(System(bot))