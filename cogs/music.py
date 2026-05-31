"""
Music cog — VC music player using wavelink + Lavalink (no yt-dlp, no cookies).

Uses a public Lavalink node so you don't need to host your own Java server.

Commands (slash):
  /play   <query or URL>   Join VC and play (or queue) a song
  /skip                    Skip current track
  /stop                    Stop and disconnect
  /pause                   Pause playback
  /resume                  Resume playback
  /queue                   Show the queue
  /nowplaying              Show current track
  /volume  <0-100>         Set volume

Commands (prefix):
  !play / !p   !skip / !s   !stop   !pause   !resume
  !queue / !q  !np          !volume / !vol <0-100>

Requirements:
  pip install wavelink
  (no FFmpeg, no yt-dlp, no PyNaCl needed)
"""

from __future__ import annotations

import asyncio
from typing import Optional, cast

import discord
from discord import app_commands
from discord.ext import commands
import wavelink


# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

MUSIC_COLOR  = discord.Color.from_rgb(29, 185, 84)
ERROR_COLOR  = discord.Color.red()

# Your self-hosted Lavalink on Wispbyte
LAVALINK_NODES = [
    {"uri": "http://93.115.101.176:13101", "password": "jarvisbot"},
]


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_duration(ms: int) -> str:
    s = ms // 1000
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _track_embed(track: wavelink.Playable, title: str = "🎵 Now Playing",
                 requester: discord.Member | discord.User | None = None) -> discord.Embed:
    embed = discord.Embed(
        title       = title,
        description = f"**[{track.title}]({track.uri})**",
        color       = MUSIC_COLOR,
    )
    embed.add_field(name="Duration", value=_fmt_duration(track.length), inline=True)
    embed.add_field(name="Author",   value=track.author or "?",         inline=True)
    if requester:
        embed.add_field(name="Requested by", value=requester.mention,   inline=True)
    if track.artwork:
        embed.set_thumbnail(url=track.artwork)
    return embed


def _err(msg: str) -> discord.Embed:
    return discord.Embed(description=f"❌ {msg}", color=ERROR_COLOR)


# ══════════════════════════════════════════════════════════════════════════════
# Cog
# ══════════════════════════════════════════════════════════════════════════════

class Music(commands.Cog):
    """Voice channel music player powered by Lavalink via wavelink."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Lavalink node setup ───────────────────────────────────────────────────

    async def cog_load(self) -> None:
        self.bot.loop.create_task(self._connect_lavalink())

    async def _connect_lavalink(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(2)
        nodes = [
            wavelink.Node(uri=n["uri"], password=n["password"])
            for n in LAVALINK_NODES
        ]
        await wavelink.Pool.connect(nodes=nodes, client=self.bot, cache_capacity=100)
        print("✅ Music: connected to Lavalink!")

    # ── wavelink events ───────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        player: wavelink.Player = payload.player
        if not player.queue.is_empty:
            await player.play(player.queue.get(), volume=100)

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        print(f"✅ Lavalink node ready: {payload.node.uri}  (resumed={payload.resumed})")

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _get_player(
        self,
        guild:  discord.Guild,
        author: discord.Member,
        send_fn,
        *,
        connect: bool = True,
    ) -> wavelink.Player | None:
        """Return (or create) a Player for this guild. Joins VC if needed."""
        player: wavelink.Player | None = cast(
            wavelink.Player | None, guild.voice_client
        )

        if not connect:
            return player

        if not author.voice or not author.voice.channel:
            await send_fn(embed=_err("You need to be in a voice channel first!"))
            return None

        channel = author.voice.channel

        if player is None:
            try:
                player = await channel.connect(cls=wavelink.Player, self_deaf=True)
            except Exception as e:
                await send_fn(embed=_err(f"Couldn't connect to VC: {e}"))
                return None
        elif player.channel != channel:
            await player.move_to(channel)

        return player

    # ── Core play logic ───────────────────────────────────────────────────────

    async def _do_play(self, guild, author, query: str, send_fn) -> None:
        player = await self._get_player(guild, author, send_fn)
        if not player:
            return

        # Search YouTube (wavelink handles this cleanly, no bot detection)
        tracks: wavelink.Search = await wavelink.Playable.search(query, source=wavelink.TrackSource.YouTube)
        if not tracks:
            await send_fn(embed=_err(f"No results found for **{query}**"))
            return

        track: wavelink.Playable = tracks[0] if isinstance(tracks, list) else tracks.tracks[0]

        if player.playing or player.paused:
            player.queue.put(track)
            embed = discord.Embed(
                title       = "➕ Added to Queue",
                description = f"**[{track.title}]({track.uri})**\n"
                              f"Position: `#{len(player.queue)}`  |  {_fmt_duration(track.length)}",
                color       = MUSIC_COLOR,
            )
            if track.artwork:
                embed.set_thumbnail(url=track.artwork)
            await send_fn(embed=embed)
        else:
            await player.play(track, volume=100)
            await send_fn(embed=_track_embed(track, requester=author))

    async def _do_skip(self, guild, send_fn) -> None:
        player: wavelink.Player | None = guild.voice_client
        if not player or not (player.playing or player.paused):
            await send_fn(embed=_err("Nothing is playing right now."))
            return
        await player.skip(force=True)
        await send_fn("⏭️ Skipped!")

    async def _do_stop(self, guild, send_fn) -> None:
        player: wavelink.Player | None = guild.voice_client
        if not player:
            await send_fn(embed=_err("I'm not in a voice channel."))
            return
        player.queue.clear()
        await player.stop()
        await player.disconnect()
        await send_fn("⏹️ Stopped and disconnected.")

    async def _do_pause(self, guild, send_fn) -> None:
        player: wavelink.Player | None = guild.voice_client
        if not player or not player.playing:
            await send_fn(embed=_err("Nothing is playing."))
            return
        await player.pause(not player.paused)
        await send_fn("⏸️ Paused." if player.paused else "▶️ Resumed.")

    async def _do_queue(self, guild, send_fn) -> None:
        player: wavelink.Player | None = guild.voice_client
        if not player or (not player.current and player.queue.is_empty):
            await send_fn("📭 The queue is empty.")
            return

        lines: list[str] = []
        if player.current:
            t = player.current
            lines.append(f"**Now playing:** [{t.title}]({t.uri}) [{_fmt_duration(t.length)}]")
        if not player.queue.is_empty:
            lines.append("\n**Up next:**")
            for i, t in enumerate(list(player.queue)[:10], 1):
                lines.append(f"`{i}.` **{t.title}** [{_fmt_duration(t.length)}]")
            if len(player.queue) > 10:
                lines.append(f"… and {len(player.queue) - 10} more")

        embed = discord.Embed(title="🎶 Queue", description="\n".join(lines), color=MUSIC_COLOR)
        await send_fn(embed=embed)

    async def _do_np(self, guild, send_fn) -> None:
        player: wavelink.Player | None = guild.voice_client
        if not player or not player.current:
            await send_fn(embed=_err("Nothing is playing right now."))
            return
        await send_fn(embed=_track_embed(player.current))

    async def _do_volume(self, guild, vol: int, send_fn) -> None:
        if not (0 <= vol <= 100):
            await send_fn(embed=_err("Volume must be between 0 and 100."))
            return
        player: wavelink.Player | None = guild.voice_client
        if not player:
            await send_fn(embed=_err("I'm not in a voice channel."))
            return
        await player.set_volume(vol)
        await send_fn(f"🔊 Volume set to **{vol}%**")

    # ── Slash commands ────────────────────────────────────────────────────────

    @app_commands.command(name="play", description="Play a song in your voice channel 🎵")
    @app_commands.describe(query="Song name or YouTube URL")
    async def slash_play(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(thinking=True)
        await self._do_play(interaction.guild, interaction.user, query.strip(),
                            interaction.followup.send)

    @app_commands.command(name="skip", description="Skip the current song ⏭️")
    async def slash_skip(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        await self._do_skip(interaction.guild, interaction.followup.send)

    @app_commands.command(name="stop", description="Stop music and disconnect ⏹️")
    async def slash_stop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        await self._do_stop(interaction.guild, interaction.followup.send)

    @app_commands.command(name="pause", description="Pause or resume the current song ⏸️")
    async def slash_pause(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        await self._do_pause(interaction.guild, interaction.followup.send)

    @app_commands.command(name="queue", description="Show the song queue 🎶")
    async def slash_queue(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        await self._do_queue(interaction.guild, interaction.followup.send)

    @app_commands.command(name="nowplaying", description="Show what's currently playing 🎵")
    async def slash_np(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        await self._do_np(interaction.guild, interaction.followup.send)

    @app_commands.command(name="volume", description="Set playback volume (0–100) 🔊")
    @app_commands.describe(level="Volume level 0–100")
    async def slash_volume(self, interaction: discord.Interaction, level: int) -> None:
        await interaction.response.defer(thinking=True)
        await self._do_volume(interaction.guild, level, interaction.followup.send)

    # ── Prefix commands ───────────────────────────────────────────────────────

    @commands.command(name="play", aliases=["p"])
    async def prefix_play(self, ctx: commands.Context, *, query: str = "") -> None:
        if not query:
            await ctx.reply("**Usage:** `!play <song name or YouTube URL>`")
            return
        async with ctx.typing():
            await self._do_play(ctx.guild, ctx.author, query.strip(), ctx.reply)

    @commands.command(name="skip", aliases=["s"])
    async def prefix_skip(self, ctx: commands.Context) -> None:
        await self._do_skip(ctx.guild, ctx.reply)

    @commands.command(name="stop")
    async def prefix_stop(self, ctx: commands.Context) -> None:
        await self._do_stop(ctx.guild, ctx.reply)

    @commands.command(name="pause")
    async def prefix_pause(self, ctx: commands.Context) -> None:
        await self._do_pause(ctx.guild, ctx.reply)

    @commands.command(name="resume")
    async def prefix_resume(self, ctx: commands.Context) -> None:
        player: wavelink.Player | None = ctx.guild.voice_client
        if not player or not player.paused:
            await ctx.reply(embed=_err("Nothing is paused."))
            return
        await player.pause(False)
        await ctx.reply("▶️ Resumed.")

    @commands.command(name="queue", aliases=["q"])
    async def prefix_queue(self, ctx: commands.Context) -> None:
        await self._do_queue(ctx.guild, ctx.reply)

    @commands.command(name="np", aliases=["nowplaying"])
    async def prefix_np(self, ctx: commands.Context) -> None:
        await self._do_np(ctx.guild, ctx.reply)

    @commands.command(name="volume", aliases=["vol"])
    async def prefix_volume(self, ctx: commands.Context, level: int = -1) -> None:
        if level == -1:
            player: wavelink.Player | None = ctx.guild.voice_client
            vol = player.volume if player else 100
            await ctx.reply(f"🔊 Current volume: **{vol}%**")
            return
        await self._do_volume(ctx.guild, level, ctx.reply)

    # ── Auto-disconnect when VC empties ───────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after:  discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        player: wavelink.Player | None = member.guild.voice_client
        if not player:
            return
        if before.channel == player.channel and len(player.channel.members) == 1:
            await asyncio.sleep(30)
            if player.channel and len(player.channel.members) == 1:
                player.queue.clear()
                await player.disconnect()


# ══════════════════════════════════════════════════════════════════════════════
# Setup
# ══════════════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
    print("✅ Music cog loaded")