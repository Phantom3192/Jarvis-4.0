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
from discord.ext import commands, tasks
from discord import app_commands
from typing import Optional

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

from cogs.admin import is_admin
from cogs.state import seen_users

# Set once at import — survives cog reloads because the module stays in sys.modules
_START_TIME = time.monotonic()

# psutil.cpu_percent()/Process.cpu_percent() need a "warm-up" call before they
# return meaningful numbers — the first call on a given object just establishes
# a baseline and returns 0.0. Keep ONE persistent Process instance at module
# level (instead of creating a fresh psutil.Process() on every command
# invocation) and prime both here, so real deltas are available afterward.
if _PSUTIL:
    _PROC = psutil.Process(os.getpid())
    _PROC.cpu_percent(interval=None)
    psutil.cpu_percent(interval=None)

# ── Live background sampling ──────────────────────────────────────────────────
# CPU% via psutil is a *delta since the last call to this same function*, not
# a point-in-time read. Every consumer that used to call psutil directly
# (Discord !usage, /status, the web API) was resetting that delta window on
# each other — whichever call landed second after a short gap saw an almost-
# zero elapsed time and reported a near-0% or noisy spike. A single
# background loop samples psutil on ONE fixed, fast cadence and every
# consumer just reads the latest snapshot — accurate, consistent, and
# updated every _SAMPLE_INTERVAL seconds no matter who's asking or how often.
_SAMPLE_INTERVAL = 2.0    # seconds — cheap psutil reads (CPU/RAM/threads),
                          # fast enough to feel real-time
_STORAGE_INTERVAL = 30.0  # seconds — os.walk() over the whole project dir
                          # is real IO/CPU work, unlike the /proc reads
                          # above, and disk usage from logs/caches simply
                          # doesn't need sub-minute freshness. Doing this
                          # every 2s like the rest would be 15x more disk
                          # scanning than needed for zero user-visible
                          # benefit.
_PING_INTERVAL = 5.0      # seconds — the REST probe below is a real
                          # network call to Discord; every 2s (1,800/hr)
                          # was more than needed for a number a human is
                          # glancing at, so this backs it off ~60% while
                          # still feeling live on the page.
_usage_cache: dict = {"data": {"available": False}, "ts": 0.0}
_ping_cache: dict = {"ws_ms": None, "api_ms": None, "ts": 0.0}
_storage_cache: dict = {"used_bytes": 0, "ts": 0.0}


# Railway (and most container platforms) don't expose a per-service disk
# *quota* anywhere the app can read it — psutil.disk_usage("/") just reports
# the shared host node's total disk, which is meaningless here. Instead we
# measure the bot's OWN footprint (its project folder — code, logs, caches,
# downloaded files, etc.) against a limit YOU set to match your actual plan.
# Check your real limit in the Railway dashboard (Settings → Usage / plan
# page) and override it with the RAILWAY_DISK_LIMIT_GB env var if it's not 5.
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root
_DISK_LIMIT_BYTES = float(os.environ.get("RAILWAY_DISK_LIMIT_GB", "1")) * 1_073_741_824

# Must stay in sync with COGS in main.py
COGS = [
    "cogs.ai",
    "cogs.admin",
    "cogs.panel",
    "cogs.stats",
    "cogs.prompts",
    "cogs.announce",
    "cogs.dm",
    "cogs.suggestions",
    "cogs.bugreport",
    "cogs.errorhandler",
    "cogs.help",
    "cogs.game",
    "cogs.system",
    "cogs.image_search",
    "cogs.summary",
    "cogs.presence",
    "cogs.youtube",
    "cogs.music",
    "cogs.economy",
    "cogs.status",
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


def _bot_storage_used() -> int:
    """
    Total size of the bot's own project directory (code, logs, caches,
    downloads, etc.) — this is what's actually consuming your disk
    allotment, as opposed to the shared host's total disk.
    """
    total = 0
    for dirpath, dirnames, filenames in os.walk(_BASE_DIR):
        # Skip .git — it's not part of the running app's footprint.
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def _ping_colour(ms: float) -> discord.Color:
    if ms < 100:
        return discord.Color.green()
    if ms < 200:
        return discord.Color.yellow()
    return discord.Color.red()


def _compute_usage_stats() -> dict:
    """One live psutil sample. Called ONLY by the background sampler loop
    below — never call this directly from a command or route handler, or
    you're back to the race condition this whole cache exists to avoid.
    """
    if not _PSUTIL:
        return {"available": False}

    proc    = _PROC
    cpu_pct = psutil.cpu_percent(interval=None)
    vm      = psutil.virtual_memory()

    cgroup = _container_memory()
    if cgroup:
        ram_used, ram_total = cgroup
        ram_pct    = ram_used / ram_total * 100
        ram_source = "container"
    else:
        ram_used, ram_total, ram_pct = vm.used, vm.total, vm.percent
        ram_source = "host"

    storage_used  = _storage_cache["used_bytes"]
    storage_limit = int(_DISK_LIMIT_BYTES)
    storage_pct   = storage_used / _DISK_LIMIT_BYTES * 100

    with proc.oneshot():
        proc_mem = proc.memory_info().rss
        proc_cpu = proc.cpu_percent(interval=None)
        threads  = proc.num_threads()

    return {
        "available":            True,
        "cpu_percent":          round(cpu_pct, 1),
        "ram_used_bytes":       ram_used,
        "ram_total_bytes":      ram_total,
        "ram_percent":          round(ram_pct, 1),
        "ram_source":           ram_source,
        "storage_used_bytes":   storage_used,
        "storage_limit_bytes":  storage_limit,
        "storage_percent":      round(storage_pct, 1),
        "process_rss_bytes":    proc_mem,
        "process_cpu_percent":  round(proc_cpu, 1),
        "threads":              threads,
        "sampled_at":           time.time(),
    }


def get_usage_stats() -> dict:
    """Live, race-free resource numbers — the numeric core shared by the
    !status "Usage" tab (embed) and the public web API. This is a pure
    cache read (updated every _SAMPLE_INTERVAL seconds by the background
    loop in System.cog); it never touches psutil itself, so no matter how
    many places ask for this at once, they all see the exact same
    consistent, current snapshot.
    """
    return _usage_cache["data"]


def get_ping_stats() -> dict:
    """Live latency numbers, refreshed every _SAMPLE_INTERVAL seconds by an
    active REST probe — NOT just the gateway heartbeat.

    bot.latency only changes when Discord ACKs a heartbeat (roughly every
    ~40s by default), so relying on it alone means the ping shown can look
    "stuck" between heartbeats even though it's technically correct. The
    background sampler also times a real, cheap REST round-trip
    (fetching the bot's own user) every _SAMPLE_INTERVAL seconds, giving a
    genuinely live "is Discord responding right now" number in between
    heartbeats.
    """
    return {
        "ws_ms":  _ping_cache["ws_ms"],
        "api_ms": _ping_cache["api_ms"],
    }



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

    # Reads the same background-sampled snapshot the web API serves — this
    # embed used to take its own separate live psutil reading here, which
    # raced against the sampler loop and the website's requests, each one
    # resetting the CPU delta window the others depended on. One source of
    # truth means the Discord embed and the website can never drift or show
    # a bogus 0%/spiked reading from a too-short measurement window.
    usage = get_usage_stats()
    if usage.get("available"):
        ram_label = "💾 Container RAM" if usage["ram_source"] == "container" else "💾 Host RAM"

        embed.add_field(name="🔲 CPU", value=f"`{usage['cpu_percent']:.1f}%`", inline=True)
        embed.add_field(
            name=ram_label,
            value=f"`{_fmt_bytes(usage['ram_used_bytes'])} / {_fmt_bytes(usage['ram_total_bytes'])}` ({usage['ram_percent']:.1f}%)",
            inline=True,
        )
        embed.add_field(
            name="💿 Bot Storage",
            value=f"`{_fmt_bytes(usage['storage_used_bytes'])} / {_fmt_bytes(usage['storage_limit_bytes'])}` ({usage['storage_percent']:.1f}%)",
            inline=True,
        )
        embed.add_field(name="🤖 Bot RSS", value=f"`{_fmt_bytes(usage['process_rss_bytes'])}`", inline=True)
        embed.add_field(name="🤖 Bot CPU", value=f"`{usage['process_cpu_percent']:.1f}%`",       inline=True)
        embed.add_field(name="🧵 Threads", value=f"`{usage['threads']}`",                         inline=True)

        age = time.time() - usage.get("sampled_at", time.time())
        embed.set_footer(text=f"Jarvis System  •  Admin only  •  Sampled {age:.1f}s ago")
    else:
        embed.add_field(
            name="⚠️ psutil not installed",
            value="Run `pip install psutil` to enable CPU/RAM metrics.",
            inline=False,
        )
        embed.set_footer(text="Jarvis System  •  Admin only")

    return embed


# ── Guild helpers ─────────────────────────────────────────────────────────────

def _build_guild_embed(guild: discord.Guild) -> discord.Embed:
    """Build a detailed info embed for a single guild."""
    embed = discord.Embed(
        title=f"🏰 {guild.name}",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    # Owner
    owner = guild.owner
    embed.add_field(name="👑 Owner",      value=f"{owner} (`{owner.id}`)" if owner else "Unknown", inline=True)
    embed.add_field(name="🆔 Guild ID",   value=f"`{guild.id}`",                                   inline=True)
    embed.add_field(name="📅 Created",    value=discord.utils.format_dt(guild.created_at, style="D"), inline=True)

    # Members
    total    = guild.member_count or 0
    bots     = sum(1 for m in guild.members if m.bot) if guild.members else "?"
    humans   = (total - bots) if isinstance(bots, int) else "?"
    embed.add_field(name="👥 Members",   value=f"`{total}` total  •  `{humans}` humans  •  `{bots}` bots", inline=False)

    # Channels
    text_ch  = len(guild.text_channels)
    voice_ch = len(guild.voice_channels)
    cats     = len(guild.categories)
    embed.add_field(name="💬 Channels",  value=f"`{text_ch}` text  •  `{voice_ch}` voice  •  `{cats}` categories", inline=False)

    # Roles & emojis
    embed.add_field(name="🎭 Roles",     value=f"`{len(guild.roles)}`",  inline=True)
    embed.add_field(name="😄 Emojis",    value=f"`{len(guild.emojis)}`", inline=True)
    embed.add_field(name="🔖 Stickers",  value=f"`{len(guild.stickers)}`", inline=True)

    # Boost
    embed.add_field(
        name="🚀 Boost",
        value=f"Level `{guild.premium_tier}`  •  `{guild.premium_subscription_count}` boosts",
        inline=True,
    )

    # Verification
    embed.add_field(name="🔒 Verification", value=f"`{guild.verification_level}`", inline=True)

    # Preferred locale
    embed.add_field(name="🌐 Locale", value=f"`{guild.preferred_locale}`", inline=True)

    embed.set_footer(text="Jarvis  •  Guild Info")
    return embed


def _build_server_list_embeds(bot: commands.Bot) -> list[discord.Embed]:
    """Build paginated embeds listing every server Jarvis is in."""
    guilds   = sorted(bot.guilds, key=lambda g: g.member_count or 0, reverse=True)
    per_page = 10
    pages    = [guilds[i:i + per_page] for i in range(0, len(guilds), per_page)]
    embeds   = []

    for idx, page in enumerate(pages, start=1):
        embed = discord.Embed(
            title=f"🌐 Servers Jarvis is in — {len(guilds)} total",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        for g in page:
            owner_name = str(g.owner) if g.owner else "Unknown"
            embed.add_field(
                name=f"{g.name}",
                value=(
                    f"ID: `{g.id}`\n"
                    f"Members: `{g.member_count or '?'}`  •  Owner: {owner_name}\n"
                    f"Created: {discord.utils.format_dt(g.created_at, style='d')}"
                ),
                inline=False,
            )
        embed.set_footer(text=f"Page {idx}/{len(pages)}  •  Jarvis Admin")
        embeds.append(embed)

    return embeds or [discord.Embed(title="No guilds found.", color=discord.Color.red())]


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
        if _PSUTIL and not _storage_cache["ts"]:
            # Prime once synchronously at startup so the first /api/stats
            # response has a real number instead of 0 for the ~30s until
            # _sample_storage_loop's first tick.
            _storage_cache["used_bytes"] = _bot_storage_used()
            _storage_cache["ts"] = time.time()
        self._sample_loop.start()
        self._sample_storage_loop.start()
        self._sample_ping_loop.start()

    def cog_unload(self):
        # Critical for !reload: without this, every reload_extension() call
        # would start a NEW copy of each loop on top of the old one (which
        # keeps running, since nothing else stops it), stacking up
        # duplicate samplers over time.
        self._sample_loop.cancel()
        self._sample_storage_loop.cancel()
        self._sample_ping_loop.cancel()

    @tasks.loop(seconds=_SAMPLE_INTERVAL)
    async def _sample_loop(self):
        """Cheap CPU/RAM/thread reads — just a couple of /proc file reads,
        negligible cost even at this cadence."""
        _usage_cache["data"] = _compute_usage_stats()
        _usage_cache["ts"]   = time.time()

    @tasks.loop(seconds=_STORAGE_INTERVAL)
    async def _sample_storage_loop(self):
        """The one genuinely non-trivial sample here — os.walk() over the
        whole project directory — deliberately kept on its own much
        slower cadence. See _STORAGE_INTERVAL comment above."""
        _storage_cache["used_bytes"] = _bot_storage_used()
        _storage_cache["ts"] = time.time()

    @tasks.loop(seconds=_PING_INTERVAL)
    async def _sample_ping_loop(self):
        """The single source of truth for both usage and ping data.

        Runs on a fixed cadence for the life of the process — not
        triggered by commands or web requests — so !usage, /status, and
        the website's /api/stats all read the exact same fresh snapshot
        instead of racing each other.
        """
        ws_ms = round(self.bot.latency * 1000) if self.bot.latency == self.bot.latency else None  # NaN guard

        api_ms = None
        if self.bot.is_ready() and self.bot.user:
            before = time.monotonic()
            try:
                await self.bot.fetch_user(self.bot.user.id)  # cheap, real REST round-trip
                api_ms = round((time.monotonic() - before) * 1000)
            except Exception:
                pass  # keep the last good api_ms rather than blanking it on a hiccup

        _ping_cache["ws_ms"]  = ws_ms
        _ping_cache["api_ms"] = api_ms if api_ms is not None else _ping_cache["api_ms"]
        _ping_cache["ts"]     = time.time()

    @_sample_loop.before_loop
    async def _before_sample_loop(self):
        await self.bot.wait_until_ready()

    @_sample_storage_loop.before_loop
    async def _before_sample_storage_loop(self):
        await self.bot.wait_until_ready()

    @_sample_ping_loop.before_loop
    async def _before_sample_ping_loop(self):
        await self.bot.wait_until_ready()

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


    # ── !guildinfo ────────────────────────────────────────────────────────────

    @commands.command(name="guildinfo")
    async def prefix_guildinfo(self, ctx: commands.Context, guild_id: int = None):
        """Show detailed info about the current server, or for a specified guild ID.

        Usage: `!guildinfo` or `!guildinfo 123456789012345678`
        """
        # Determine which guild to show: explicit ID takes precedence.
        if guild_id is None:
            if ctx.guild is None:
                await ctx.reply("🚫 This command must be used in a server or you must provide a guild ID.")
                return
            guild = ctx.guild
        else:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                await ctx.reply("❌ I couldn't find a guild with that ID that I'm in.")
                return

        await ctx.reply(embed=_build_guild_embed(guild))

    @app_commands.command(name="guildinfo", description="Show detailed info about this server")
    async def slash_guildinfo(self, interaction: discord.Interaction, guild_id: Optional[int] = None):
        """Show detailed info about the current server or for a specified guild ID.

        Usage: `/guildinfo` or `/guildinfo guild_id:123456789012345678`
        """
        if guild_id is None:
            if interaction.guild is None:
                await interaction.response.send_message(
                    "🚫 This command must be used in a server or you must provide a guild ID.",
                    ephemeral=True,
                )
                return
            guild = interaction.guild
        else:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                await interaction.response.send_message(
                    "❌ I couldn't find a guild with that ID that I'm in.", ephemeral=True
                )
                return

        await interaction.response.send_message(embed=_build_guild_embed(guild))



async def setup(bot: commands.Bot):
    await bot.add_cog(System(bot))