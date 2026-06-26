"""
Music cog — VC music player using yt-dlp + FFmpeg (no Lavalink, no wavelink).

The bot extracts a direct stream URL with yt-dlp and pipes it straight into
the VC with discord.py's native FFmpegPCMAudio. No external Lavalink node,
no node-fallback/health-check logic — everything runs on the bot's own
CPU/network.

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
  pip install yt-dlp PyNaCl
  FFmpeg must be installed and on PATH (or set FFMPEG_PATH env var).
"""

from __future__ import annotations

import asyncio
import functools
import re
import shutil
from collections import deque
from typing import Optional, cast

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

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

# ── Temporary feature toggle ────────────────────────────────────────────────
# Set to True to make ALL music/VC commands respond with a "temporarily down"
# message instead of running. Set back to False to restore normal behaviour.
MUSIC_FEATURE_DOWN = False

MUSIC_DOWN_EMBED = discord.Embed(
    title="🚧 Music is temporarily down",
    description=(
        "Music/VC commands are currently disabled while we fix some issues "
        "with the audio backend. Please check back later!"
    ),
    color=discord.Color.orange(),
)

# Path to the ffmpeg binary. Falls back to "ffmpeg" (PATH lookup) if not found
# via shutil.which — set the FFMPEG_PATH env var if it lives somewhere custom.
import os as _os
import base64 as _base64
import tempfile as _tempfile
FFMPEG_EXECUTABLE = _os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg") or "ffmpeg"

# Optional path to a YouTube cookies.txt file (Netscape format). Needed when
# running from a datacenter IP (Railway, Heroku, AWS, etc) — YouTube
# aggressively bot-checks those ranges and a logged-in session via cookies
# is the most reliable way around the "Sign in to confirm you're not a bot"
# error. Export with a browser extension (e.g. "Get cookies.txt LOCALLY")
# while logged into a YouTube account.
#
# Two ways to provide it (checked in this order):
#   1. YTDLP_COOKIES_B64 — the cookies.txt file, base64-encoded, pasted
#      directly as an env var value. No volume/file upload needed — this
#      is decoded to a temp file at startup. Simplest option for Railway.
#   2. YTDLP_COOKIES_FILE — a path to an already-existing cookies.txt file
#      on disk (e.g. on a mounted volume), used as-is.
YTDLP_COOKIES_B64 = _os.environ.get("YTDLP_COOKIES_B64", "").strip()
YTDLP_COOKIES_FILE = _os.environ.get("YTDLP_COOKIES_FILE", "").strip()

if YTDLP_COOKIES_B64:
    try:
        # validate=True rejects strings containing characters outside the
        # base64 alphabet — without it, b64decode silently treats any text
        # as "valid" base64 and produces garbage bytes instead of erroring,
        # which is exactly what happens if raw cookie text gets pasted into
        # this var by mistake instead of its base64 encoding.
        decoded = _base64.b64decode(YTDLP_COOKIES_B64, validate=True)
        # A real Netscape cookies.txt is plain ASCII/UTF-8 text starting
        # with a comment line or tab-separated fields — sanity check this
        # before trusting it, since garbage-but-validly-padded base64 can
        # still slip through the line above.
        preview = decoded[:200].decode("utf-8")
        if not (preview.startswith("#") or "\t" in preview):
            raise ValueError(
                "decoded content doesn't look like a Netscape cookies.txt "
                "file (expected a '#' comment or tab-separated fields). "
                "Did you paste the file's raw text instead of its base64 "
                "encoding?"
            )
        _tmp = _tempfile.NamedTemporaryFile(
            mode="wb", suffix="_cookies.txt", delete=False
        )
        _tmp.write(decoded)
        _tmp.close()
        YTDLP_COOKIES_FILE = _tmp.name
        print(f"✅ Music: decoded YTDLP_COOKIES_B64 to temp file {YTDLP_COOKIES_FILE}")
    except Exception as e:
        print(
            f"⚠️  Music: failed to decode YTDLP_COOKIES_B64: {e}\n"
            "   Make sure you pasted the OUTPUT of the base64 command "
            "(a long string of letters/numbers), not the cookies.txt "
            "file's own content."
        )

YTDLP_FORMAT_OPTIONS = {
    # Fallback chain: prefer a pure-audio stream, but accept a combined
    # video+audio stream if that's all a given client/video offers, rather
    # than erroring out with "Requested format is not available." FFmpeg
    # can consume HLS (m3u8) streams natively, so we don't need to exclude
    # those — only direct-URL/m3u8 formats matter, not "no formats at all".
    "format": "bestaudio[ext=m4a]/bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",  # avoid ipv6 issues on some hosts
    "extract_flat": False,
    # NOTE: YouTube has been rolling out "SABR" streaming, which makes the
    # "web"/"web_safari" clients return formats with NO direct/HLS url at
    # all ("Some web client https formats have been skipped... YouTube is
    # forcing SABR streaming for this client" — yt-dlp issue #12482). That
    # leaves zero usable formats, hence "Requested format is not
    # available" even though bestaudio/best should always match something.
    # "tv" and "ios" are the clients still returning real playable URLs as
    # of mid-2026. We rely on cookies (set above) for the bot-check, and
    # these clients for getting an actual stream URL back.
    "extractor_args": {"youtube": {"player_client": ["tv", "ios"]}},
}
if YTDLP_COOKIES_FILE and _os.path.isfile(YTDLP_COOKIES_FILE):
    YTDLP_FORMAT_OPTIONS["cookiefile"] = YTDLP_COOKIES_FILE
    print(f"✅ Music: using YouTube cookies from {YTDLP_COOKIES_FILE}")
elif YTDLP_COOKIES_FILE:
    print(f"⚠️  Music: YTDLP_COOKIES_FILE set to '{YTDLP_COOKIES_FILE}' but file not found.")

FFMPEG_BEFORE_OPTS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
)
FFMPEG_OPTS = "-vn"

# Minimum gap (in seconds) between consecutive yt-dlp extraction requests,
# to avoid hammering YouTube and tripping rate limits / bot checks.
MIN_TRACK_LOAD_GAP = 1.5


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "?"
    s = int(seconds)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _err(msg: str) -> discord.Embed:
    return discord.Embed(description=f"❌ {msg}", color=ERROR_COLOR)


_OPUS_CANDIDATES = (
    "opus",            # generic name, works if a dev-symlink exists
    "libopus.so.0",    # common Debian/Ubuntu runtime name
    "libopus.so",
    "libopus.0.dylib", # macOS (Homebrew)
    "libopus.dylib",
)

# Extra search roots for environments without a populated linker cache
# (e.g. Nix-based containers like Railway's, where libopus.so.0 may exist
# but isn't on the dynamic linker's default search path).
_OPUS_SEARCH_DIRS = (
    "/usr/lib", "/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu",
    "/usr/local/lib", "/lib", "/lib/x86_64-linux-gnu",
    "/nix/store", "/opt/homebrew/lib",
)


def _find_libopus_on_disk() -> str | None:
    """
    Fallback for containers (Nix/Railway) where ctypes' default search
    (which relies on the dynamic linker cache) can't resolve the library
    by name even though the file is present on disk. Walks a few common
    library directories looking for any libopus* file.
    """
    import glob
    for root in _OPUS_SEARCH_DIRS:
        if not _os.path.isdir(root):
            continue
        # /nix/store has thousands of subdirs — only descend one extra
        # level for libopus*/lib rather than a full recursive walk.
        patterns = (
            [f"{root}/libopus.so*", f"{root}/libopus*.dylib"]
            if root != "/nix/store"
            else [f"{root}/*/lib/libopus.so*"]
        )
        for pattern in patterns:
            matches = glob.glob(pattern)
            if matches:
                return matches[0]
    return None


def _ensure_opus_loaded() -> None:
    """
    discord.py needs the native libopus library loaded before any audio can
    be encoded for voice. On Windows, discord.opus.load_default() finds the
    bundled DLL automatically — but on Linux/Mac there's no auto-detection,
    so VoiceClient.play() raises a bare `OpusNotLoaded()` (which prints as
    an EMPTY string, e.g. "[Play] Playback error: ") if nothing loads it
    first. We try a few common library names, then fall back to scanning
    disk directly for containers where the linker cache isn't populated.
    """
    if discord.opus.is_loaded():
        return

    tried: list[str] = []
    for name in _OPUS_CANDIDATES:
        tried.append(name)
        try:
            discord.opus.load_opus(name)
            print(f"✅ Music: loaded libopus via '{name}'")
            return
        except OSError:
            continue

    found_path = _find_libopus_on_disk()
    if found_path:
        tried.append(found_path)
        try:
            discord.opus.load_opus(found_path)
            print(f"✅ Music: loaded libopus via disk scan: '{found_path}'")
            return
        except OSError as e:
            print(f"⚠️  Music: found '{found_path}' on disk but ctypes failed to load it: {e}")

    print(
        "⚠️  Music: could not auto-load libopus — voice playback will fail "
        "with a blank 'OpusNotLoaded' error.\n"
        f"   Tried: {tried}\n"
        "   Install it via your package manager (e.g. `apt install libopus0` "
        "/ `brew install opus`), or set a custom path with "
        "discord.opus.load_opus('<path-to-lib>')."
    )


class Track:
    """
    Lightweight stand-in for wavelink.Playable — holds metadata plus the
    resolved stream URL needed to hand off to FFmpegPCMAudio. `length` is
    kept in SECONDS (unlike the old wavelink-ms convention) since that's
    what yt-dlp reports natively.
    """

    __slots__ = ("title", "uri", "webpage_url", "author", "length", "artwork")

    def __init__(
        self,
        title: str,
        uri: str,
        webpage_url: str,
        author: str | None,
        length: float | None,
        artwork: str | None,
    ) -> None:
        self.title = title
        self.uri = uri                  # direct/stream URL fed to FFmpeg
        self.webpage_url = webpage_url   # human-facing link (YouTube page etc.)
        self.author = author
        self.length = length or 0
        self.artwork = artwork


def _track_info(track: Track) -> dict[str, object]:
    return {
        "title":  track.title,
        "uri":    track.webpage_url,
        "author": track.author or "",
        "length": track.length,
    }


def _track_embed(track: Track, title: str = "🎵 Now Playing",
                  requester: discord.Member | discord.User | None = None) -> discord.Embed:
    embed = discord.Embed(
        title       = title,
        description = f"**[{track.title}]({track.webpage_url})**",
        color       = MUSIC_COLOR,
    )
    embed.add_field(name="Duration", value=_fmt_duration(track.length), inline=True)
    embed.add_field(name="Author",   value=track.author or "?",        inline=True)
    if requester:
        embed.add_field(name="Requested by", value=requester.mention,  inline=True)
    if track.artwork:
        embed.set_thumbnail(url=track.artwork)
    return embed


# ── yt-dlp extraction (run in a thread, it's blocking) ──────────────────────

def _extract_sync(query: str, *, search: bool) -> list[dict]:
    """
    Blocking yt-dlp call — must be run via run_in_executor.
    If `search` is True, query is treated as search terms (uses
    ytsearch5: to get multiple candidates for /play picking).
    If False, query is treated as a direct URL.
    """
    opts = dict(YTDLP_FORMAT_OPTIONS)
    target = f"ytsearch5:{query}" if search else query
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
    if info is None:
        return []
    entries = info.get("entries") if "entries" in info else [info]
    return [e for e in entries if e]


async def _extract(query: str, *, search: bool = True) -> list[dict]:
    loop = asyncio.get_event_loop()
    fn = functools.partial(_extract_sync, query, search=search)
    return await loop.run_in_executor(None, fn)


def _entry_to_track(entry: dict) -> Optional[Track]:
    stream_url = entry.get("url")
    if not stream_url:
        # Some extractors nest formats; fall back to best format's url.
        formats = entry.get("formats") or []
        if formats:
            stream_url = formats[-1].get("url")
    if not stream_url:
        return None
    return Track(
        title       = entry.get("title", "Unknown title"),
        uri         = stream_url,
        webpage_url = entry.get("webpage_url") or entry.get("original_url") or stream_url,
        author      = entry.get("uploader") or entry.get("channel"),
        length      = entry.get("duration"),
        artwork     = entry.get("thumbnail"),
    )


_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


async def _smart_search(query: str) -> tuple[list[Track], str]:
    """
    Resolve a query (URL or plain search text) into a list of Track
    candidates via yt-dlp. Returns (tracks, source_label).
    yt-dlp itself supports YouTube, SoundCloud, and many other sites when
    given a direct URL — plain text always searches YouTube.
    """
    q = query.strip()
    is_url = bool(_URL_RE.match(q))
    try:
        entries = await _extract(q, search=not is_url)
    except yt_dlp.utils.DownloadError as e:
        print(f"[yt-dlp] extraction failed for '{q}': {e}")
        return [], "none"
    except Exception as e:
        print(f"[yt-dlp] unexpected error for '{q}': {e}")
        return [], "none"

    tracks = [t for e in entries if (t := _entry_to_track(e)) is not None]
    if not tracks:
        return [], "none"

    if is_url:
        host = q.split("//", 1)[-1].split("/", 1)[0]
        if "soundcloud" in host:
            source = "soundcloud"
        elif "youtube" in host or "youtu.be" in host:
            source = "youtube"
        else:
            source = host
    else:
        source = "youtube"
    return tracks, source


async def _resolve_saved_track(info: dict[str, object]) -> Optional[Track]:
    uri = info.get("uri")
    title = info.get("title", "")
    author = info.get("author", "")
    query = uri or f"{author} {title}".strip()
    if not query:
        return None
    tracks, _ = await _smart_search(str(query))
    return tracks[0] if tracks else None


async def _find_similar_track(history: list[dict[str, object]]) -> Optional[Track]:
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
    return tracks[0] if tracks else None


# ══════════════════════════════════════════════════════════════════════════════
# Guild player — wraps an FFmpeg/PCMVolumeTransformer source + queue
# ══════════════════════════════════════════════════════════════════════════════

class GuildPlayer:
    """
    Per-guild playback state. Mirrors the bits of wavelink.Player that the
    rest of this cog relies on: .current, .queue, .playing, .paused,
    .volume, .voice_client, play()/skip()/stop()/pause()/set_volume().
    """

    def __init__(self, voice_client: discord.VoiceClient, cog: "Music", guild_id: int) -> None:
        self.voice_client = voice_client
        self.cog = cog
        self.guild_id = guild_id
        self.queue: deque[Track] = deque()
        self.current: Optional[Track] = None
        self.volume: int = 100
        self._skip_requested = False
        self._stopped = False

    @property
    def channel(self):
        return self.voice_client.channel

    @property
    def connected(self) -> bool:
        return self.voice_client.is_connected()

    @property
    def playing(self) -> bool:
        return self.voice_client.is_playing()

    @property
    def paused(self) -> bool:
        return self.voice_client.is_paused()

    def queue_is_empty(self) -> bool:
        return len(self.queue) == 0

    def queue_put(self, track: Track) -> None:
        self.queue.append(track)

    def queue_list(self) -> list[Track]:
        return list(self.queue)

    async def play(self, track: Track, *, volume: int | None = None) -> None:
        if volume is not None:
            self.volume = volume
        self.current = track
        self._stopped = False

        def _make_source() -> discord.AudioSource:
            source = discord.FFmpegPCMAudio(
                track.uri,
                executable=FFMPEG_EXECUTABLE,
                before_options=FFMPEG_BEFORE_OPTS,
                options=FFMPEG_OPTS,
            )
            return discord.PCMVolumeTransformer(source, volume=self.volume / 100)

        loop = asyncio.get_event_loop()

        def _after(error: Exception | None) -> None:
            if error:
                detail = str(error) or error.__class__.__name__
                print(f"[Player] Playback error for '{track.title}' ({error.__class__.__name__}): {detail}")
            fut = asyncio.run_coroutine_threadsafe(
                self.cog._on_track_end(self.guild_id, track, error), loop
            )
            try:
                fut.result()
            except Exception as e:
                print(f"[Player] on_track_end handler raised: {e}")

        try:
            audio_source = _make_source()
        except Exception as e:
            print(f"[Player] Failed to build audio source: {e}")
            await self.cog._on_track_end(self.guild_id, track, e)
            return

        self.voice_client.play(audio_source, after=_after)

    async def skip(self) -> None:
        self._skip_requested = True
        if self.voice_client.is_playing() or self.voice_client.is_paused():
            self.voice_client.stop()  # triggers `after`, which advances the queue

    async def stop(self) -> None:
        self._stopped = True
        self.queue.clear()
        if self.voice_client.is_playing() or self.voice_client.is_paused():
            self.voice_client.stop()

    async def pause(self, pause: bool) -> None:
        if pause and self.voice_client.is_playing():
            self.voice_client.pause()
        elif not pause and self.voice_client.is_paused():
            self.voice_client.resume()

    async def set_volume(self, vol: int) -> None:
        self.volume = vol
        source = self.voice_client.source
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = vol / 100

    async def disconnect(self) -> None:
        self._stopped = True
        try:
            await self.voice_client.disconnect(force=True)
        except Exception:
            pass


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
        tracks:  list[Track],
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
            player.queue_put(track)
            embed = discord.Embed(
                title       = "➕ Added to Queue",
                description = f"**[{track.title}]({track.webpage_url})**\n"
                              f"Position: `#{len(player.queue)}`  |  {_fmt_duration(track.length)}",
                color       = MUSIC_COLOR,
            )
        else:
            await player.play(track, volume=player.volume)
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

        track_list = tracks[:5]
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

    def _player(self) -> Optional["GuildPlayer"]:
        return self.cog._players.get(self.guild_id)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        player = self._player()
        guild  = self._guild()
        embed  = self.cog._controls_embed(guild) if guild else discord.Embed(title="🎵 Music Controls", description="Error getting guild.", color=ERROR_COLOR)
        self._update_buttons(player)
        await interaction.response.edit_message(embed=embed, view=self)

    def _update_buttons(self, player: Optional["GuildPlayer"]) -> None:
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
        if not player or (not player.current and player.queue_is_empty()):
            await interaction.response.send_message("📭 The queue is empty.", ephemeral=True)
            return
        lines: list[str] = []
        if player.current:
            t = player.current
            lines.append(f"**Now playing:** [{t.title}]({t.webpage_url}) [{_fmt_duration(t.length)}]")
        if not player.queue_is_empty():
            lines.append("\n**Up next:**")
            for i, t in enumerate(player.queue_list()[:10], 1):
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
    """Voice channel music player powered by yt-dlp + FFmpeg."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._players: dict[int, GuildPlayer] = {}
        self._last_requester: dict[int, int] = {}
        self._last_request_time: float = 0.0
        self._request_lock = asyncio.Lock()

    # ── Feature toggle gate ───────────────────────────────────────────────────
    # When MUSIC_FEATURE_DOWN is True, every normal prefix command (cog_check)
    # and every normal slash command (interaction_check) in this cog responds
    # with the "temporarily down" message instead of running — for EVERYONE,
    # including the bot owner. Only the dedicated `force*` commands below
    # (forcejoin, forceplay, forcestop, ...) bypass this, so the owner can
    # still test the VC/music backend in private.

    _FORCE_COMMAND_NAMES = {
        "forcejoin", "forceplay", "forcestop", "forcepause", "forceresume",
        "forceskip", "forcequeue", "forcenp", "forcevolume", "forcecontrols",
    }

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.command and ctx.command.qualified_name in self._FORCE_COMMAND_NAMES:
            return True
        if MUSIC_FEATURE_DOWN:
            await ctx.reply(embed=MUSIC_DOWN_EMBED)
            return False
        return True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cmd_name = interaction.command.name if interaction.command else ""
        if cmd_name in self._FORCE_COMMAND_NAMES:
            return True
        if MUSIC_FEATURE_DOWN:
            if interaction.response.is_done():
                await interaction.followup.send(embed=MUSIC_DOWN_EMBED, ephemeral=True)
            else:
                await interaction.response.send_message(embed=MUSIC_DOWN_EMBED, ephemeral=True)
            return False
        return True

    # ── Setup / teardown ──────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        if shutil.which("ffmpeg") is None and not _os.environ.get("FFMPEG_PATH"):
            print(
                "⚠️  Music: ffmpeg not found on PATH and FFMPEG_PATH is not set. "
                "Install ffmpeg (e.g. `apt install ffmpeg`) or set FFMPEG_PATH."
            )
        _ensure_opus_loaded()
        print("✅ Music: yt-dlp/FFmpeg backend ready (no Lavalink needed)")

    async def cog_unload(self) -> None:
        for player in list(self._players.values()):
            await player.disconnect()
        self._players.clear()

    # ── Track-end handling (replaces wavelink's on_wavelink_track_end) ────────

    async def _on_track_end(self, guild_id: int, track: Track, error: Exception | None) -> None:
        player = self._players.get(guild_id)
        if player is None:
            return
        if player._stopped:
            return

        if not player.queue_is_empty():
            next_track = player.queue.popleft()
            await player.play(next_track, volume=player.volume)
            return

        player.current = None

        autoplay = bool(get_setting(f"autoplay_channel_{guild_id}", False))
        if not autoplay:
            return

        user_id = self._last_requester.get(guild_id)
        if not user_id:
            return

        history = get_song_history(user_id)
        similar = await _find_similar_track(history)
        if similar:
            await player.play(similar, volume=player.volume)

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _get_player(
        self,
        guild:  discord.Guild,
        author: discord.Member,
        send_fn,
        *,
        connect: bool = True,
    ) -> Optional[GuildPlayer]:
        """Return (or create) a GuildPlayer for this guild. Joins VC if needed."""
        player = self._players.get(guild.id)

        if not connect:
            return player

        if not author.voice or not author.voice.channel:
            await send_fn(embed=_err("You need to be in a voice channel first!"))
            return None

        channel = author.voice.channel

        if player is None or not player.connected:
            try:
                voice_client = await channel.connect(self_deaf=True)
            except Exception as e:
                await send_fn(embed=_err(f"Couldn't connect to VC: {e}"))
                return None
            player = GuildPlayer(voice_client, self, guild.id)
            self._players[guild.id] = player
        elif player.channel != channel:
            await player.voice_client.move_to(channel)

        return player

    # ── Core play logic ───────────────────────────────────────────────────────

    def _controls_embed(self, guild: discord.Guild) -> discord.Embed:
        """Build the now-playing embed for the controls panel."""
        player = self._players.get(guild.id)
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
            description = f"**[{t.title}]({t.webpage_url})**",
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

    # ── Request pacing ─────────────────────────────────────────────────────────

    async def _pace_request(self, guild_id: int = 0) -> None:
        """
        Sleep just enough so consecutive yt-dlp extraction calls are spaced
        at least MIN_TRACK_LOAD_GAP seconds apart, globally — avoids
        hammering YouTube with rapid-fire requests across guilds.
        """
        async with self._request_lock:
            now = asyncio.get_event_loop().time()
            wait = MIN_TRACK_LOAD_GAP - (now - self._last_request_time)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_time = asyncio.get_event_loop().time()

    async def _do_play(self, guild, author, query: str, send_fn) -> None:
        player = await self._get_player(guild, author, send_fn)
        if not player:
            return

        await self._pace_request(guild.id)

        tracks, source_used = await _smart_search(query)
        if not tracks:
            await send_fn(embed=_err(
                f"No results found for **{query}**\n"
                "Try a direct YouTube/SoundCloud link, or a different search term."
            ))
            return

        track: Track = tracks[0]

        source_labels = {
            "youtube": "YouTube", "soundcloud": "SoundCloud", "none": "Unknown"
        }
        source_label = source_labels.get(source_used, source_used.title())

        if player.playing or player.paused:
            player.queue_put(track)
            embed = discord.Embed(
                title       = "➕ Added to Queue",
                description = f"**[{track.title}]({track.webpage_url})**\n"
                              f"Position: `#{len(player.queue)}`  |  {_fmt_duration(track.length)}",
                color       = MUSIC_COLOR,
            )
            embed.set_footer(text=f"Source: {source_label}")
            if track.artwork:
                embed.set_thumbnail(url=track.artwork)
            await send_fn(embed=embed)
        else:
            try:
                await player.play(track, volume=player.volume)
            except Exception as e:
                detail = str(e) or e.__class__.__name__
                print(f"[Play] Playback error ({e.__class__.__name__}): {detail}")
                await send_fn(embed=_err(f"Playback failed: `{detail}`"))
                return

            self._last_requester[guild.id] = author.id
            append_song_history(author.id, _track_info(track))
            embed = _track_embed(track, requester=author)
            embed.set_footer(text=f"Source: {source_label}")
            await send_fn(embed=embed)

    async def _do_skip(self, guild, send_fn) -> None:
        player = self._players.get(guild.id)
        if not player or not (player.playing or player.paused):
            await send_fn(embed=_err("Nothing is playing right now."))
            return
        await player.skip()
        await send_fn("⏭️ Skipped!")

    async def _do_stop(self, guild, send_fn) -> None:
        player = self._players.get(guild.id)
        if not player:
            await send_fn(embed=_err("I'm not in a voice channel."))
            return
        await player.stop()
        await player.disconnect()
        self._players.pop(guild.id, None)
        await send_fn("⏹️ Stopped and disconnected.")

    async def _do_pause(self, guild, send_fn) -> None:
        player = self._players.get(guild.id)
        if not player or not player.playing:
            await send_fn(embed=_err("Nothing is playing."))
            return
        await player.pause(not player.paused)
        await send_fn("⏸️ Paused." if player.paused else "▶️ Resumed.")

    async def _do_queue(self, guild, send_fn) -> None:
        player = self._players.get(guild.id)
        if not player or (not player.current and player.queue_is_empty()):
            await send_fn("📭 The queue is empty.")
            return

        lines: list[str] = []
        if player.current:
            t = player.current
            lines.append(f"**Now playing:** [{t.title}]({t.webpage_url}) [{_fmt_duration(t.length)}]")
        if not player.queue_is_empty():
            lines.append("\n**Up next:**")
            for i, t in enumerate(player.queue_list()[:10], 1):
                lines.append(f"`{i}.` **{t.title}** [{_fmt_duration(t.length)}]")
            if len(player.queue) > 10:
                lines.append(f"… and {len(player.queue) - 10} more")

        embed = discord.Embed(title="🎶 Queue", description="\n".join(lines), color=MUSIC_COLOR)
        await send_fn(embed=embed)

    async def _do_np(self, guild, send_fn) -> None:
        player = self._players.get(guild.id)
        if not player or not player.current:
            await send_fn(embed=_err("Nothing is playing right now."))
            return
        await send_fn(embed=_track_embed(player.current))

    async def _do_volume(self, guild, vol: int, send_fn) -> None:
        if not (0 <= vol <= 100):
            await send_fn(embed=_err("Volume must be between 0 and 100."))
            return
        player = self._players.get(guild.id)
        if not player:
            await send_fn(embed=_err("I'm not in a voice channel."))
            return
        await player.set_volume(vol)
        await send_fn(f"🔊 Volume set to **{vol}%**")

    # ── Controls panel ───────────────────────────────────────────────────────────

    async def _send_controls(self, guild, send_fn) -> None:
        view   = MusicControlsView(cog=self, guild=guild)
        player = self._players.get(guild.id)
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
        player = self._players.get(ctx.guild.id)
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
            player = self._players.get(ctx.guild.id)
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

        playable_tracks: list[Track] = []
        for info in tracks:
            await self._pace_request(ctx.guild.id)
            resolved = await _resolve_saved_track(info)
            if resolved:
                playable_tracks.append(resolved)

        if not playable_tracks:
            await ctx.reply("❌ Could not load any songs from that playlist.")
            return

        if player.playing or player.paused:
            for track in playable_tracks:
                player.queue_put(track)
            await ctx.reply(f"✅ Added **{len(playable_tracks)}** songs from `{key}` to the queue.")
        else:
            await player.play(playable_tracks[0], volume=player.volume)
            self._last_requester[ctx.guild.id] = ctx.author.id
            append_song_history(ctx.author.id, _track_info(playable_tracks[0]))
            for track in playable_tracks[1:]:
                player.queue_put(track)
            await ctx.reply(embed=_track_embed(playable_tracks[0], requester=ctx.author))

    @prefix_playlist.command(name="add")
    async def playlist_add(self, ctx: commands.Context, name: str, *, query: str) -> None:
        tracks, _ = await _smart_search(query)
        if not tracks:
            await ctx.reply(f"❌ No results found for **{query}**")
            return
        track = tracks[0]
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

    # ── Owner-only testing commands ─────────────────────────────────────────
    # These bypass MUSIC_FEATURE_DOWN entirely (cog_check already allows the
    # bot owner through), so you can test the VC/music backend even while it
    # shows "temporarily down" to everyone else.

    @commands.command(name="forcejoin", aliases=["fjoin"])
    @commands.is_owner()
    async def prefix_forcejoin(self, ctx: commands.Context) -> None:
        """Owner-only: force the bot to join your current voice channel."""
        player = await self._get_player(ctx.guild, ctx.author, ctx.reply)
        if not player:
            return
        await ctx.reply(f"✅ Joined **{player.channel.name}** (force).")

    @commands.command(name="forceplay", aliases=["fplay"])
    @commands.is_owner()
    async def prefix_forceplay(self, ctx: commands.Context, *, query: str = "") -> None:
        """Owner-only: force-play a track, bypassing the feature-down message."""
        if not query:
            await ctx.reply("**Usage:** `!forceplay <song name or YouTube URL>`")
            return
        async with ctx.typing():
            await self._do_play(ctx.guild, ctx.author, query.strip(), ctx.reply)

    @app_commands.command(name="forcejoin", description="(Owner) Force the bot to join your VC")
    async def slash_forcejoin(self, interaction: discord.Interaction) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Owner only.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        player = await self._get_player(interaction.guild, interaction.user, interaction.followup.send)
        if not player:
            return
        await interaction.followup.send(f"✅ Joined **{player.channel.name}** (force).")

    @app_commands.command(name="forceplay", description="(Owner) Force-play a track, bypassing feature-down")
    @app_commands.describe(query="Song name or YouTube URL")
    async def slash_forceplay(self, interaction: discord.Interaction, query: str) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Owner only.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await self._do_play(interaction.guild, interaction.user, query.strip(), interaction.followup.send)

    @commands.command(name="forcestop", aliases=["fstop"])
    @commands.is_owner()
    async def prefix_forcestop(self, ctx: commands.Context) -> None:
        """Owner-only: force-stop and disconnect, bypassing feature-down."""
        await self._do_stop(ctx.guild, ctx.reply)

    @app_commands.command(name="forcestop", description="(Owner) Force stop and disconnect")
    async def slash_forcestop(self, interaction: discord.Interaction) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Owner only.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await self._do_stop(interaction.guild, interaction.followup.send)

    @commands.command(name="forcepause", aliases=["fpause"])
    @commands.is_owner()
    async def prefix_forcepause(self, ctx: commands.Context) -> None:
        """Owner-only: force pause/resume toggle, bypassing feature-down."""
        await self._do_pause(ctx.guild, ctx.reply)

    @app_commands.command(name="forcepause", description="(Owner) Force pause/resume toggle")
    async def slash_forcepause(self, interaction: discord.Interaction) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Owner only.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await self._do_pause(interaction.guild, interaction.followup.send)

    @commands.command(name="forceresume", aliases=["fresume"])
    @commands.is_owner()
    async def prefix_forceresume(self, ctx: commands.Context) -> None:
        """Owner-only: force resume playback, bypassing feature-down."""
        player = self._players.get(ctx.guild.id)
        if not player or not player.paused:
            await ctx.reply(embed=_err("Nothing is paused."))
            return
        await player.pause(False)
        await ctx.reply("▶️ Resumed (force).")

    @commands.command(name="forceskip", aliases=["fskip"])
    @commands.is_owner()
    async def prefix_forceskip(self, ctx: commands.Context) -> None:
        """Owner-only: force skip the current track, bypassing feature-down."""
        await self._do_skip(ctx.guild, ctx.reply)

    @app_commands.command(name="forceskip", description="(Owner) Force skip the current track")
    async def slash_forceskip(self, interaction: discord.Interaction) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Owner only.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await self._do_skip(interaction.guild, interaction.followup.send)

    @commands.command(name="forcequeue", aliases=["fqueue", "fq"])
    @commands.is_owner()
    async def prefix_forcequeue(self, ctx: commands.Context) -> None:
        """Owner-only: force show the queue, bypassing feature-down."""
        await self._do_queue(ctx.guild, ctx.reply)

    @commands.command(name="forcenp", aliases=["fnp"])
    @commands.is_owner()
    async def prefix_forcenp(self, ctx: commands.Context) -> None:
        """Owner-only: force show now-playing, bypassing feature-down."""
        await self._do_np(ctx.guild, ctx.reply)

    @commands.command(name="forcevolume", aliases=["fvol"])
    @commands.is_owner()
    async def prefix_forcevolume(self, ctx: commands.Context, level: int) -> None:
        """Owner-only: force set volume, bypassing feature-down."""
        await self._do_volume(ctx.guild, level, ctx.reply)

    @commands.command(name="forcecontrols", aliases=["fctrl", "fcp"])
    @commands.is_owner()
    async def prefix_forcecontrols(self, ctx: commands.Context) -> None:
        """Owner-only: force open the music control panel, bypassing feature-down."""
        await self._send_controls(ctx.guild, ctx.reply)

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
        player = self._players.get(member.guild.id)
        if not player:
            return
        if before.channel == player.channel and len(player.channel.members) == 1:
            await asyncio.sleep(30)
            if player.channel and len(player.channel.members) == 1:
                player.queue.clear()
                await player.disconnect()
                self._players.pop(member.guild.id, None)


# ══════════════════════════════════════════════════════════════════════════════
# Setup
# ══════════════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
    print("✅ Music cog loaded (yt-dlp + FFmpeg backend)")