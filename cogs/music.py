+"""
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
from cogs.state import (
    append_song_history,
    delete_user_playlist,
    get_setting,
    set_setting,
    get_song_history,
    get_user_playlists,
    set_user_playlist,
)


# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

MUSIC_COLOR  = discord.Color.from_rgb(29, 185, 84)
ERROR_COLOR  = discord.Color.red()

# Source prefixes for smart detection
_SPOTIFY_PREFIXES    = ("https://open.spotify.com/", "spotify:")
_DEEZER_PREFIXES     = ("https://www.deezer.com/", "https://deezer.com/")
_APPLE_PREFIXES      = ("https://music.apple.com/",)
_SOUNDCLOUD_PREFIXES = ("https://soundcloud.com/", "https://on.soundcloud.com/")

LAVALINK_NODES = [
    {
        "uri": "http://noble-serenity.railway.internal:2333",
        "password": "jarvisbot"
    }
]


def _detect_source(query: str) -> str:
    """
    Returns the best search prefix for a query.
    - Direct URLs (Spotify, Deezer, Apple Music, SoundCloud) → passed as-is
    - Plain text → tries YouTube first, falls back handled in _smart_search
    """
    q = query.strip()
    if q.startswith(_SPOTIFY_PREFIXES):
        return "spotify"
    if q.startswith(_DEEZER_PREFIXES):
        return "deezer"
    if q.startswith(_APPLE_PREFIXES):
        return "applemusic"
    if q.startswith(_SOUNDCLOUD_PREFIXES):
        return "soundcloud"
    # Plain text or YouTube URL
    return "ytsearch"


async def _smart_search(query: str) -> tuple[wavelink.Search | None, str]:
    """
    Search with automatic source detection and fallback chain:
      1. Detected source (Spotify/Apple/SC URL, or YouTube for plain text)
      2. SoundCloud (if YouTube fails)
    Returns (results, source_used_label).
    """
    q = query.strip()
    source = _detect_source(q)

    # Direct URL sources — pass straight through, no prefix needed
    if source in ("spotify", "applemusic", "soundcloud"):
        tracks = await wavelink.Playable.search(q)
        if tracks:
            return tracks, source
        return None, source

    # Plain text — try YouTube first
    tracks = await wavelink.Playable.search(q, source=wavelink.TrackSource.YouTube)
    if tracks:
        return tracks, "youtube"

    # YouTube failed → try SoundCloud
    tracks = await wavelink.Playable.search(q, source=wavelink.TrackSource.SoundCloud)
    if tracks:
        return tracks, "soundcloud"

    return None, "none"

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


def _track_info(track: wavelink.Playable) -> dict[str, object]:
    return {
        "title":  track.title,
        "uri":    track.uri,
        "author": track.author or "",
        "length": track.length,
    }


async def _resolve_saved_track(info: dict[str, object]) -> Optional[wavelink.Playable]:
    uri = info.get("uri")
    title = info.get("title", "")
    author = info.get("author", "")
    if not uri and not title:
        return None
    # Try by URI first, then fall back to title+author search
    query = uri or f"{author} {title}".strip()
    tracks, _ = await _smart_search(query)
    if not tracks:
        return None
    return tracks[0] if isinstance(tracks, list) else tracks.tracks[0]


async def _find_similar_track(history: list[dict[str, object]]) -> Optional[wavelink.Playable]:
    if not history:
        return None
    last = history[-1]
    author = last.get("author", "")
    title = last.get("title", "")
    if author:
        query = f"{author} similar songs"
    elif title:
        query = f"songs like {title}"
    else:
        return None
    tracks, _ = await _smart_search(query)
    if not tracks:
        return None
    return tracks[0] if isinstance(tracks, list) else tracks.tracks[0]



# ══════════════════════════════════════════════════════════════════════════════
# Music Controls Panel
# ══════════════════════════════════════════════════════════════════════════════

class SearchResultsView(discord.ui.View):
    """Dropdown of search results to pick from."""

    def __init__(
        self,
        cog:     "Music",
        guild:   discord.Guild,
        author:  discord.Member | discord.User,
        tracks:  list[wavelink.Playable],
    ) -> None:
        super().__init__(timeout=60)
        self.cog    = cog
        self.guild  = guild
        self.author = author
        self.tracks = tracks

        options = [
            discord.SelectOption(
                label       = t.title[:100],
                description = f"{t.author or '?'}  •  {_fmt_duration(t.length)}"[:100],
                value       = str(i),
                emoji       = "🎵",
            )
            for i, t in enumerate(tracks)
        ]
        select = discord.ui.Select(
            placeholder = "Choose a song to play…",
            options     = options,
            min_values  = 1,
            max_values  = 1,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("❌ This isn't your search!", ephemeral=True)
            return
        idx   = int(interaction.data["values"][0])
        track = self.tracks[idx]

        player = await self.cog._get_player(self.guild, interaction.user,
                                             interaction.followup.send)
        if not player:
            await interaction.response.defer()
            return

        if player.playing or player.paused:
            player.queue.put(track)
            embed = discord.Embed(
                title       = "➕ Added to Queue",
                description = f"**[{track.title}]({track.uri})**"
                              f"Position: `#{len(player.queue)}`  |  {_fmt_duration(track.length)}",
                color       = MUSIC_COLOR,
            )
        else:
            await player.play(track, volume=100)
            embed = _track_embed(track)

        if track.artwork:
            embed.set_thumbnail(url=track.artwork)
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True


class SearchModal(discord.ui.Modal, title="🔍 Search for a Song"):
    """Modal popup with a text input for song search."""

    query = discord.ui.TextInput(
        label       = "Song name or YouTube URL",
        placeholder = "e.g. Blinding Lights, or paste a YouTube link…",
        min_length  = 1,
        max_length  = 200,
    )

    def __init__(self, cog: "Music", guild: discord.Guild, author) -> None:
        super().__init__()
        self.cog    = cog
        self.guild  = guild
        self.author = author

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        query  = self.query.value.strip()
        tracks, _ = await _smart_search(query)
        if not tracks:
            await interaction.followup.send(embed=_err(f"No results found for **{query}**"))
            return

        track_list = tracks[:5] if isinstance(tracks, list) else list(tracks.tracks)[:5]
        embed = discord.Embed(
            title       = f"🔍 Search results for: {query}",
            description = "\n".join(
                f"`{i+1}.` **{t.title}** — {t.author or '?'} [{_fmt_duration(t.length)}]"
                for i, t in enumerate(track_list)
            ),
            color       = MUSIC_COLOR,
        )
        embed.set_footer(text="Pick a song from the dropdown below")
        view = SearchResultsView(
            cog=self.cog, guild=self.guild,
            author=interaction.user, tracks=track_list
        )
        await interaction.followup.send(embed=embed, view=view)


class MusicControlsView(discord.ui.View):
    """Interactive music control panel with buttons."""

    def __init__(self, cog: "Music", guild: discord.Guild) -> None:
        super().__init__(timeout=180)
        self.cog      = cog
        self.guild_id = guild.id
        self.bot      = cog.bot

    def _guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.guild_id)

    def _player(self) -> wavelink.Player | None:
        guild = self._guild()
        return guild.voice_client if guild else None

    async def _refresh(self, interaction: discord.Interaction) -> None:
        player = self._player()
        guild  = self._guild()
        embed  = self.cog._controls_embed(guild) if guild else discord.Embed(title="🎵 Music Controls", description="Error getting guild.", color=ERROR_COLOR)
        self._update_buttons(player)
        await interaction.response.edit_message(embed=embed, view=self)

    def _update_buttons(self, player: wavelink.Player | None) -> None:
        playing = bool(player and (player.playing or player.paused))
        paused  = bool(player and player.paused)
        self.btn_pause.label    = "▶ Resume" if paused else "⏸ Pause"
        self.btn_pause.style    = discord.ButtonStyle.success if paused else discord.ButtonStyle.secondary
        self.btn_pause.disabled = not playing
        self.btn_skip.disabled  = not playing
        self.btn_stop.disabled  = not (player and player.connected)
        self.btn_vol_down.disabled = not playing
        self.btn_vol_up.disabled   = not playing

    @discord.ui.button(label="⏸ Pause", style=discord.ButtonStyle.secondary, row=0)
    async def btn_pause(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog._do_pause(self._guild(), lambda *a, **kw: asyncio.sleep(0))
        await self._refresh(interaction)

    @discord.ui.button(label="⏭ Skip", style=discord.ButtonStyle.primary, row=0)
    async def btn_skip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog._do_skip(self._guild(), lambda *a, **kw: asyncio.sleep(0))
        await self._refresh(interaction)

    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.danger, row=0)
    async def btn_stop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog._do_stop(self._guild(), lambda *a, **kw: asyncio.sleep(0))
        await self._refresh(interaction)

    @discord.ui.button(label="🔉 Vol -10", style=discord.ButtonStyle.secondary, row=1)
    async def btn_vol_down(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self._player()
        if player:
            new_vol = max(0, player.volume - 10)
            await self.cog._do_volume(self._guild(), new_vol, lambda *a, **kw: asyncio.sleep(0))
        await self._refresh(interaction)

    @discord.ui.button(label="🔊 Vol +10", style=discord.ButtonStyle.secondary, row=1)
    async def btn_vol_up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self._player()
        if player:
            new_vol = min(100, player.volume + 10)
            await self.cog._do_volume(self._guild(), new_vol, lambda *a, **kw: asyncio.sleep(0))
        await self._refresh(interaction)

    @discord.ui.button(label="🎶 Queue", style=discord.ButtonStyle.secondary, row=1)
    async def btn_queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self._player()
        if not player or (not player.current and player.queue.is_empty):
            await interaction.response.send_message("📭 The queue is empty.", ephemeral=True)
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
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🔍 Search", style=discord.ButtonStyle.primary, row=2)
    async def btn_search(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        modal = SearchModal(cog=self.cog, guild=self._guild(), author=interaction.user)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=2)
    async def btn_refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._refresh(interaction)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True


# ══════════════════════════════════════════════════════════════════════════════
# Cog
# ══════════════════════════════════════════════════════════════════════════════

class Music(commands.Cog):
    """Voice channel music player powered by Lavalink via wavelink."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_requester: dict[int, int] = {}

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
            return

        autoplay = bool(get_setting(f"autoplay_channel_{player.guild.id}", False))
        if not autoplay:
            return

        user_id = self._last_requester.get(player.guild.id)
        if not user_id:
            return

        history = get_song_history(user_id)
        similar = await _find_similar_track(history)
        if similar:
            await player.play(similar, volume=100)

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

    def _controls_embed(self, guild: discord.Guild) -> discord.Embed:
        """Build the now-playing embed for the controls panel."""
        player: wavelink.Player | None = guild.voice_client
        print(f"[Controls] guild={guild.id} player={player} current={getattr(player, 'current', None)} playing={getattr(player, 'playing', None)}")
        if not player or not player.current:
            embed = discord.Embed(
                title       = "🎵 Music Controls",
                description = "Nothing is playing right now.",
                color       = MUSIC_COLOR,
            )
            return embed
        t = player.current
        status = "⏸ Paused" if player.paused else "▶ Playing"
        embed = discord.Embed(
            title       = "🎵 Music Controls",
            description = f"**[{t.title}]({t.uri})**",
            color       = MUSIC_COLOR,
        )
        embed.add_field(name="Status",   value=status,                    inline=True)
        embed.add_field(name="Duration", value=_fmt_duration(t.length),   inline=True)
        embed.add_field(name="Volume",   value=f"{player.volume}%",       inline=True)
        embed.add_field(name="Author",   value=t.author or "?",           inline=True)
        embed.add_field(name="Queue",    value=f"{len(player.queue)} songs", inline=True)
        if t.artwork:
            embed.set_thumbnail(url=t.artwork)
        embed.set_footer(text="Controls auto-update when you click buttons")
        return embed

    async def _do_play(self, guild, author, query: str, send_fn) -> None:
        player = await self._get_player(guild, author, send_fn)
        if not player:
            return

        tracks, source_used = await _smart_search(query)
        if not tracks:
            await send_fn(embed=_err(
                f"No results found for **{query}**\n"
                "Try a Spotify/Deezer/SoundCloud link, or a different search term."
            ))
            return

        track: wavelink.Playable = tracks[0] if isinstance(tracks, list) else tracks.tracks[0]

        # Source label for user feedback
        source_labels = {
            "youtube": "YouTube", "soundcloud": "SoundCloud",
            "deezer": "Deezer", "spotify": "Spotify",
            "applemusic": "Apple Music", "none": "Unknown"
        }
        source_label = source_labels.get(source_used, source_used.title())

        if player.playing or player.paused:
            player.queue.put(track)
            embed = discord.Embed(
                title       = "➕ Added to Queue",
                description = f"**[{track.title}]({track.uri})**\n"
                              f"Position: `#{len(player.queue)}`  |  {_fmt_duration(track.length)}",
                color       = MUSIC_COLOR,
            )
            embed.set_footer(text=f"Source: {source_label}")
            if track.artwork:
                embed.set_thumbnail(url=track.artwork)
            await send_fn(embed=embed)
        else:
            await player.play(track, volume=100)
            self._last_requester[player.guild.id] = author.id
            append_song_history(author.id, _track_info(track))
            embed = _track_embed(track, requester=author)
            embed.set_footer(text=f"Source: {source_label}")
            await send_fn(embed=embed)

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

    # ── Controls panel ───────────────────────────────────────────────────────────

    async def _send_controls(self, guild, send_fn) -> None:
        view   = MusicControlsView(cog=self, guild=guild)
        player = guild.voice_client
        view._update_buttons(player)
        embed  = self._controls_embed(guild)
        await send_fn(embed=embed, view=view)

    # ── Slash commands ────────────────────────────────────────────────────────

    @app_commands.command(name="controls", description="Open the music control panel 🎛️")
    async def slash_controls(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        await self._send_controls(interaction.guild, interaction.followup.send)

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

    @commands.command(name="controls", aliases=["ctrl", "panel", "cp"])
    async def prefix_controls(self, ctx: commands.Context) -> None:
        await self._send_controls(ctx.guild, ctx.reply)

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

    @commands.group(name="playlist", invoke_without_command=True)
    async def prefix_playlist(self, ctx: commands.Context) -> None:
        await ctx.reply(
            "**Playlist commands:** `!playlist list`, `!playlist show <name>`, "
            "`!playlist play <name>`, `!playlist add <name> <song>`, "
            "`!playlist remove <name> <index>`, `!playlist delete <name>`"
        )

    @prefix_playlist.command(name="list")
    async def playlist_list(self, ctx: commands.Context) -> None:
        playlists = get_user_playlists(ctx.author.id)
        if not playlists:
            await ctx.reply("📂 You don't have any playlists yet.")
            return
        names = "\n".join(f"• {name}" for name in playlists)
        await ctx.reply(embed=discord.Embed(title="Your Playlists", description=names, color=MUSIC_COLOR))

    @prefix_playlist.command(name="show")
    async def playlist_show(self, ctx: commands.Context, name: str) -> None:
        playlists = get_user_playlists(ctx.author.id)
        key = next((k for k in playlists if k.lower() == name.lower()), None)
        if key is None:
            await ctx.reply(f"❌ Playlist `{name}` not found.")
            return
        tracks = playlists[key]
        if not tracks:
            await ctx.reply(f"📂 Playlist `{key}` is empty.")
            return
        lines = [f"`{i+1}.` **{t['title']}**" for i, t in enumerate(tracks[:20])]
        if len(tracks) > 20:
            lines.append(f"… and {len(tracks) - 20} more")
        await ctx.reply(embed=discord.Embed(title=f"Playlist: {key}", description="\n".join(lines), color=MUSIC_COLOR))

    @prefix_playlist.command(name="play")
    async def playlist_play(self, ctx: commands.Context, name: str) -> None:
        playlists = get_user_playlists(ctx.author.id)
        key = next((k for k in playlists if k.lower() == name.lower()), None)
        if key is None:
            await ctx.reply(f"❌ Playlist `{name}` not found.")
            return
        tracks = playlists[key]
        if not tracks:
            await ctx.reply(f"📂 Playlist `{key}` is empty.")
            return

        player = await self._get_player(ctx.guild, ctx.author, ctx.reply)
        if not player:
            return

        playable_tracks: list[wavelink.Playable] = []
        for info in tracks:
            resolved = await _resolve_saved_track(info)
            if resolved:
                playable_tracks.append(resolved)

        if not playable_tracks:
            await ctx.reply("❌ Could not load any songs from that playlist.")
            return

        if player.playing or player.paused:
            for track in playable_tracks:
                player.queue.put(track)
            await ctx.reply(f"✅ Added **{len(playable_tracks)}** songs from `{key}` to the queue.")
        else:
            await player.play(playable_tracks[0], volume=100)
            self._last_requester[ctx.guild.id] = ctx.author.id
            append_song_history(ctx.author.id, _track_info(playable_tracks[0]))
            for track in playable_tracks[1:]:
                player.queue.put(track)
            await ctx.reply(embed=_track_embed(playable_tracks[0], requester=ctx.author))

    @prefix_playlist.command(name="add")
    async def playlist_add(self, ctx: commands.Context, name: str, *, query: str) -> None:
        tracks, _ = await _smart_search(query)
        if not tracks:
            await ctx.reply(f"❌ No results found for **{query}**")
            return
        track = tracks[0] if isinstance(tracks, list) else tracks.tracks[0]
        playlist = get_user_playlists(ctx.author.id)
        key = next((k for k in playlist if k.lower() == name.lower()), name)
        tracks_data = playlist.get(key, [])
        tracks_data.append(_track_info(track))
        set_user_playlist(ctx.author.id, key, tracks_data)
        await ctx.reply(f"✅ Added **{track.title}** to playlist `{key}`.")

    @prefix_playlist.command(name="remove")
    async def playlist_remove(self, ctx: commands.Context, name: str, index: int) -> None:
        playlists = get_user_playlists(ctx.author.id)
        key = next((k for k in playlists if k.lower() == name.lower()), None)
        if key is None:
            await ctx.reply(f"❌ Playlist `{name}` not found.")
            return
        tracks = playlists[key]
        if not (1 <= index <= len(tracks)):
            await ctx.reply("❌ Invalid track index.")
            return
        removed = tracks.pop(index - 1)
        set_user_playlist(ctx.author.id, key, tracks)
        await ctx.reply(f"✅ Removed **{removed['title']}** from `{key}`.")

    @prefix_playlist.command(name="delete")
    async def playlist_delete(self, ctx: commands.Context, name: str) -> None:
        if not delete_user_playlist(ctx.author.id, name if name in get_user_playlists(ctx.author.id) else next((k for k in get_user_playlists(ctx.author.id) if k.lower() == name.lower()), name)):
            await ctx.reply(f"❌ Playlist `{name}` not found.")
            return
        await ctx.reply(f"✅ Deleted playlist `{name}`.")

    @commands.command(name="autoplay")
    async def prefix_autoplay(self, ctx: commands.Context, value: str = None) -> None:
        key = f"autoplay_channel_{ctx.guild.id}"
        current = bool(get_setting(key, False))
        if value is None:
            await ctx.reply(f"🔁 Autoplay is currently `{'on' if current else 'off'}` for this channel.")
            return
        normalized = value.lower()
        if normalized in {"on", "true", "yes", "1"}:
            set_setting(key, True)
            await ctx.reply("✅ Autoplay enabled for this channel.")
        elif normalized in {"off", "false", "no", "0"}:
            set_setting(key, False)
            await ctx.reply("✅ Autoplay disabled for this channel.")
        else:
            await ctx.reply("❌ Invalid value. Use `on` or `off`.")

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