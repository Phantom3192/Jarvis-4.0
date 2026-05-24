"""
System cog — host resource usage and bot reload.

Commands
--------
!usage / /usage
    Shows live CPU %, RAM used/total, disk used/total, bot uptime,
    and process-level memory. Admin-only.

!reload / /reload
    Runs gc.collect(), clears the internal Discord object cache, and
    reloads every cog in COGS. Useful after memory leaks or stale state.
    Admin-only.
"""
import gc
import os
import time
import asyncio
import discord
from discord.ext import commands
from discord import app_commands

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

from cogs.admin import is_admin

# Bot start time — set once when the cog is first loaded
_START_TIME = time.monotonic()

# Must stay in sync with the COGS list in main.py
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
    "cogs.system",   # reload ourselves too
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_bytes(n: int) -> str:
    """Human-readable bytes → 'X.X GB' / 'X.X MB'."""
    if n >= 1_073_741_824:
        return f"{n / 1_073_741_824:.1f} GB"
    return f"{n / 1_048_576:.1f} MB"


def _fmt_uptime(seconds: float) -> str:
    """Seconds → 'Xd Xh Xm Xs'."""
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s,  3600)
    m, s = divmod(s,    60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _build_usage_embed(bot: commands.Bot) -> discord.Embed:
    embed = discord.Embed(
        title="🖥️ Jarvis — System Usage",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )

    uptime = time.monotonic() - _START_TIME
    embed.add_field(name="⏱️ Uptime",   value=f"`{_fmt_uptime(uptime)}`", inline=True)
    embed.add_field(name="🌐 Guilds",   value=f"`{len(bot.guilds)}`",     inline=True)
    embed.add_field(name="👤 Users",    value=f"`{len(bot.users):,}`",    inline=True)

    if _PSUTIL:
        proc = psutil.Process(os.getpid())

        # ── Host metrics ──────────────────────────────────────────────────────
        cpu_pct  = psutil.cpu_percent(interval=None)    # non-blocking snapshot
        vm       = psutil.virtual_memory()
        disk     = psutil.disk_usage("/")

        embed.add_field(
            name="🔲 Host CPU",
            value=f"`{cpu_pct:.1f}%`",
            inline=True,
        )
        embed.add_field(
            name="💾 Host RAM",
            value=f"`{_fmt_bytes(vm.used)} / {_fmt_bytes(vm.total)}` ({vm.percent:.1f}%)",
            inline=True,
        )
        embed.add_field(
            name="💿 Disk",
            value=f"`{_fmt_bytes(disk.used)} / {_fmt_bytes(disk.total)}` ({disk.percent:.1f}%)",
            inline=True,
        )

        # ── Process metrics ───────────────────────────────────────────────────
        with proc.oneshot():
            proc_mem  = proc.memory_info().rss
            proc_cpu  = proc.cpu_percent(interval=None)
            threads   = proc.num_threads()

        embed.add_field(
            name="🤖 Bot RSS",
            value=f"`{_fmt_bytes(proc_mem)}`",
            inline=True,
        )
        embed.add_field(
            name="🤖 Bot CPU",
            value=f"`{proc_cpu:.1f}%`",
            inline=True,
        )
        embed.add_field(
            name="🧵 Threads",
            value=f"`{threads}`",
            inline=True,
        )
    else:
        embed.add_field(
            name="⚠️ psutil not installed",
            value="Run `pip install psutil` to enable CPU/RAM metrics.",
            inline=False,
        )

    embed.set_footer(text="Jarvis System  •  Admin only")
    return embed


# ── Cog ───────────────────────────────────────────────────────────────────────

class System(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── !usage ────────────────────────────────────────────────────────────────

    @commands.command(name="usage")
    async def prefix_usage(self, ctx: commands.Context):
        """Show host CPU, RAM, disk, and bot process stats. Admin only."""
        if not is_admin(ctx.author):
            await ctx.reply("🚫 You don't have permission to use this command.")
            return
        await ctx.reply(embed=_build_usage_embed(self.bot))

    @app_commands.command(name="usage", description="Show host CPU, RAM, disk and bot stats (admin only)")
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


# ── Reload logic (shared by prefix and slash) ─────────────────────────────────

async def _do_reload(bot: commands.Bot) -> str:
    """
    1. Run gc.collect() to free unreferenced objects.
    2. Clear Discord's internal member/message caches to free RAM.
    3. Reload every cog — picks up code changes and resets cog state.
    Returns a summary string.
    """
    # Step 1 — garbage collect
    collected = gc.collect()

    # Step 2 — clear Discord object caches
    bot._connection._messages.clear()          # cached Message objects
    # Note: clearing bot._connection._users/_guilds would drop member cache
    # and is usually too aggressive for a running bot, so we skip those.

    # Step 3 — reload cogs
    ok:   list[str] = []
    fail: list[str] = []

    for cog in COGS:
        try:
            await bot.reload_extension(cog)
            ok.append(cog.split(".")[-1])
        except commands.ExtensionNotLoaded:
            # Cog wasn't loaded yet — try loading it fresh
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


async def setup(bot: commands.Bot):
    await bot.add_cog(System(bot))