# """
# Music cog — VC music player using wavelink + Lavalink (no yt-dlp, no cookies).

# Uses a public Lavalink node so you don't need to host your own Java server.

# Commands (slash):
#   /play   <query or URL>   Join VC and play (or queue) a song
#   /skip                    Skip current track
#   /stop                    Stop and disconnect
#   /pause                   Pause playback
#   /resume                  Resume playback
#   /queue                   Show the queue
#   /nowplaying              Show current track
#   /volume  <0-100>         Set volume

# Commands (prefix):
#   !play / !p   !skip / !s   !stop   !pause   !resume
#   !queue / !q  !np          !volume / !vol <0-100>

# Requirements:
#   pip install wavelink
#   (no FFmpeg, no yt-dlp, no PyNaCl needed)
# """

# from __future__ import annotations

# import asyncio
# import re
# from typing import Optional, cast

# import discord
# from discord import app_commands
# from discord.ext import commands
# import wavelink
# from cogs.state import (
#     append_song_history,
#     delete_user_playlist,
#     get_setting,
#     set_setting,
#     get_song_history,
#     get_user_playlists,
#     set_user_playlist,
# )


# # ══════════════════════════════════════════════════════════════════════════════
# # Constants
# # ══════════════════════════════════════════════════════════════════════════════

# MUSIC_COLOR  = discord.Color.from_rgb(29, 185, 84)
# ERROR_COLOR  = discord.Color.red()

# # ── Temporary feature toggle ────────────────────────────────────────────────
# # Set to True to make ALL music/VC commands respond with a "temporarily down"
# # message instead of running. Set back to False to restore normal behaviour.
# MUSIC_FEATURE_DOWN = True

# MUSIC_DOWN_EMBED = discord.Embed(
#     title="🚧 Music is temporarily down",
#     description=(
#         "Music/VC commands are currently disabled while we fix some issues "
#         "with the audio backend. Please check back later!"
#     ),
#     color=discord.Color.orange(),
# )

# # Source prefixes for smart detection
# _SPOTIFY_PREFIXES    = ("https://open.spotify.com/", "spotify:")
# _DEEZER_PREFIXES     = ("https://www.deezer.com/", "https://deezer.com/")
# _APPLE_PREFIXES      = ("https://music.apple.com/",)
# _SOUNDCLOUD_PREFIXES = ("https://soundcloud.com/", "https://on.soundcloud.com/")

# # ── Node identifiers ─────────────────────────────────────────────────────────
# # NODE_PUBLIC  : community node, used for YouTube (primary)
# # NODE_SC      : your self-hosted node, used as SoundCloud fallback when
# #                YouTube throws a login/access error
# NODE_PUBLIC = "public-yt"
# NODE_SC     = "self-sc"

# # Public YT nodes tried in order — first working one is used, rest are fallback
# PUBLIC_YT_NODES = [
#     {"identifier": "public-yt-1", "host": "lavalink.jirayu.net",        "password": "youshallnotpass",               "secure": False, "port": 13592},
#     {"identifier": "public-yt-2", "host": "lavalinkv4.serenetia.com",   "password": "https://seretia.link/discord",  "secure": False, "port": 80},
#     {"identifier": "public-yt-3", "host": "lavalink.triniumhost.com",   "password": "kirito",                        "secure": False, "port": 2333},
#     {"identifier": "public-yt-4", "host": "lava2.kasawa.pro",           "password": "youshallnotpass",               "secure": False, "port": 2334},
# ]
# LAVALINK_NODES = PUBLIC_YT_NODES + [
#     # Self-hosted node — SoundCloud fallback only
#     {
#         "identifier": NODE_SC,
#         "host":       "happy-joy-production-906e.up.railway.app",
#         "password":   "jarvisbot",
#         "secure":     True,
#         "port":       443,
#     },
# ]


# # Minimum gap (in seconds) between consecutive track-load requests.
# MIN_TRACK_LOAD_GAP = 2.5


# # Common noise in YouTube video titles that hurts cross-platform search
# # matching (e.g. "David Kushner - Daylight (Official Music Video)" finds
# # worse SoundCloud matches than "David Kushner Daylight"). Matched
# # case-insensitively, with or without surrounding brackets/parens.
# _TITLE_NOISE_RE = re.compile(
#     r"""[\(\[]?\s*(?:official\s+)?(?:music\s+)?(?:video|audio|lyric[s]?|
#         visualizer|m/?v)\s*[\)\]]?""",
#     re.IGNORECASE | re.VERBOSE,
# )


# def _clean_search_query(author: str | None, title: str) -> str:
#     """
#     Build a cross-platform search query from a YouTube track's author
#     and title, stripping common noise like "(Official Music Video)" and
#     avoiding a duplicated artist name when the title already starts with
#     "Artist - Song" (the most common YouTube upload format).
#     """
#     cleaned_title = _TITLE_NOISE_RE.sub("", title).strip(" -–—")
#     cleaned_author = re.sub(r"\s*-\s*Topic$", "", author, flags=re.IGNORECASE).strip() if author else author
#     if cleaned_author and cleaned_title.lower().startswith(cleaned_author.lower()):
#         return cleaned_title
#     return f"{cleaned_author} {cleaned_title}".strip() if cleaned_author else cleaned_title



# # Title keywords signaling a remix/edit that's a genuinely different
# # audio file from the original — sped up, slowed, reverb, nightcore,
# # 8D, etc. We exclude these from fallback candidates rather than risk
# # picking a clearly-altered version of the requested song.
# _REMIX_NOISE_RE = re.compile(
#     r"\b(sped\s*up|slowed(?:\s*\+?\s*reverb)?|nightcore|8d\s*audio|"
#     r"reverb|tiktok\s*remix|bass\s*boosted|extended\s*mix|loop)\b",
#     re.IGNORECASE,
# )


# def _detect_source(query: str) -> str:
#     """
#     Returns the best search prefix for a query.
#     - Direct URLs (Spotify, Deezer, Apple Music, SoundCloud) → passed as-is
#     - Plain text → tries YouTube first, falls back handled in _smart_search
#     """
#     q = query.strip()
#     if q.startswith(_SPOTIFY_PREFIXES):
#         return "spotify"
#     if q.startswith(_DEEZER_PREFIXES):
#         return "deezer"
#     if q.startswith(_APPLE_PREFIXES):
#         return "applemusic"
#     if q.startswith(_SOUNDCLOUD_PREFIXES):
#         return "soundcloud"
#     # Plain text or YouTube URL
#     return "ytsearch"


# async def _smart_search(query: str) -> tuple[wavelink.Search | None, str]:
#     """
#     Search with automatic source detection and fallback chain:
#       1. Detected source (Spotify/Apple/SC URL, or YouTube for plain text)
#       2. SoundCloud (if YouTube fails)
#     Returns (results, source_used_label).
#     """
#     q = query.strip()
#     source = _detect_source(q)

#     # Direct URL sources — pass straight through, no prefix needed
#     if source in ("spotify", "applemusic", "soundcloud"):
#         tracks = await wavelink.Playable.search(q)
#         if tracks:
#             return tracks, source, None
#         return None, source, None

#     # Plain text — try each public YT node in order
#     for n in PUBLIC_YT_NODES:
#         node = wavelink.Pool.get_node(n["identifier"])
#         if node is None:
#             print(f"[YT] Node {n['identifier']} not connected, skipping")
#             continue
#         try:
#             tracks = await wavelink.Playable.search(q, source=wavelink.TrackSource.YouTube, node=node)
#             if tracks:
#                 print(f"[YT] Node {n['identifier']} succeeded")
#                 return tracks, "youtube", node
#             else:
#                 print(f"[YT] Node {n['identifier']} returned no results")
#         except Exception as e:
#             print(f"[YT] Node {n['identifier']} error: {e}")

#     print("[YT] All public nodes failed, giving up")
#     return None, "none", None

# # ══════════════════════════════════════════════════════════════════════════════
# # Helpers
# # ══════════════════════════════════════════════════════════════════════════════

# def _fmt_duration(ms: int) -> str:
#     s = ms // 1000
#     h, r = divmod(s, 3600)
#     m, s = divmod(r, 60)
#     return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# def _track_embed(track: wavelink.Playable, title: str = "🎵 Now Playing",
#                  requester: discord.Member | discord.User | None = None) -> discord.Embed:
#     embed = discord.Embed(
#         title       = title,
#         description = f"**[{track.title}]({track.uri})**",
#         color       = MUSIC_COLOR,
#     )
#     embed.add_field(name="Duration", value=_fmt_duration(track.length), inline=True)
#     embed.add_field(name="Author",   value=track.author or "?",         inline=True)
#     if requester:
#         embed.add_field(name="Requested by", value=requester.mention,   inline=True)
#     if track.artwork:
#         embed.set_thumbnail(url=track.artwork)
#     return embed


# def _err(msg: str) -> discord.Embed:
#     return discord.Embed(description=f"❌ {msg}", color=ERROR_COLOR)


# def _track_info(track: wavelink.Playable) -> dict[str, object]:
#     return {
#         "title":  track.title,
#         "uri":    track.uri,
#         "author": track.author or "",
#         "length": track.length,
#     }


# async def _resolve_saved_track(info: dict[str, object]) -> Optional[wavelink.Playable]:
#     uri = info.get("uri")
#     title = info.get("title", "")
#     author = info.get("author", "")
#     if not uri and not title:
#         return None
#     # Try by URI first, then fall back to title+author search
#     query = uri or f"{author} {title}".strip()
#     tracks, _, _ = await _smart_search(query)
#     if not tracks:
#         return None
#     return tracks[0] if isinstance(tracks, list) else tracks.tracks[0]


# async def _find_similar_track(history: list[dict[str, object]]) -> Optional[wavelink.Playable]:
#     if not history:
#         return None
#     last = history[-1]
#     author = last.get("author", "")
#     title = last.get("title", "")
#     if author:
#         query = f"{author} similar songs"
#     elif title:
#         query = f"songs like {title}"
#     else:
#         return None
#     tracks, _, _ = await _smart_search(query)
#     if not tracks:
#         return None
#     return tracks[0] if isinstance(tracks, list) else tracks.tracks[0]



# # ══════════════════════════════════════════════════════════════════════════════
# # Music Controls Panel
# # ══════════════════════════════════════════════════════════════════════════════

# class SearchResultsView(discord.ui.View):
#     """Dropdown of search results to pick from."""

#     def __init__(
#         self,
#         cog:     "Music",
#         guild:   discord.Guild,
#         author:  discord.Member | discord.User,
#         tracks:  list[wavelink.Playable],
#     ) -> None:
#         super().__init__(timeout=60)
#         self.cog    = cog
#         self.guild  = guild
#         self.author = author
#         self.tracks = tracks

#         options = [
#             discord.SelectOption(
#                 label       = t.title[:100],
#                 description = f"{t.author or '?'}  •  {_fmt_duration(t.length)}"[:100],
#                 value       = str(i),
#                 emoji       = "🎵",
#             )
#             for i, t in enumerate(tracks)
#         ]
#         select = discord.ui.Select(
#             placeholder = "Choose a song to play…",
#             options     = options,
#             min_values  = 1,
#             max_values  = 1,
#         )
#         select.callback = self._on_select
#         self.add_item(select)

#     async def _on_select(self, interaction: discord.Interaction) -> None:
#         if interaction.user.id != self.author.id:
#             await interaction.response.send_message("❌ This isn't your search!", ephemeral=True)
#             return
#         idx   = int(interaction.data["values"][0])
#         track = self.tracks[idx]

#         player = await self.cog._get_player(self.guild, interaction.user,
#                                              interaction.followup.send)
#         if not player:
#             await interaction.response.defer()
#             return

#         if player.playing or player.paused:
#             player.queue.put(track)
#             embed = discord.Embed(
#                 title       = "➕ Added to Queue",
#                 description = f"**[{track.title}]({track.uri})**"
#                               f"Position: `#{len(player.queue)}`  |  {_fmt_duration(track.length)}",
#                 color       = MUSIC_COLOR,
#             )
#         else:
#             await player.play(track, volume=100)
#             embed = _track_embed(track)

#         if track.artwork:
#             embed.set_thumbnail(url=track.artwork)
#         await interaction.response.edit_message(embed=embed, view=None)

#     async def on_timeout(self) -> None:
#         for item in self.children:
#             item.disabled = True


# class SearchModal(discord.ui.Modal, title="🔍 Search for a Song"):
#     """Modal popup with a text input for song search."""

#     query = discord.ui.TextInput(
#         label       = "Song name or YouTube URL",
#         placeholder = "e.g. Blinding Lights, or paste a YouTube link…",
#         min_length  = 1,
#         max_length  = 200,
#     )

#     def __init__(self, cog: "Music", guild: discord.Guild, author) -> None:
#         super().__init__()
#         self.cog    = cog
#         self.guild  = guild
#         self.author = author

#     async def on_submit(self, interaction: discord.Interaction) -> None:
#         await interaction.response.defer(thinking=True)
#         query  = self.query.value.strip()
#         tracks, _, _ = await _smart_search(query)
#         if not tracks:
#             await interaction.followup.send(embed=_err(f"No results found for **{query}**"))
#             return

#         track_list = tracks[:5] if isinstance(tracks, list) else list(tracks.tracks)[:5]
#         embed = discord.Embed(
#             title       = f"🔍 Search results for: {query}",
#             description = "\n".join(
#                 f"`{i+1}.` **{t.title}** — {t.author or '?'} [{_fmt_duration(t.length)}]"
#                 for i, t in enumerate(track_list)
#             ),
#             color       = MUSIC_COLOR,
#         )
#         embed.set_footer(text="Pick a song from the dropdown below")
#         view = SearchResultsView(
#             cog=self.cog, guild=self.guild,
#             author=interaction.user, tracks=track_list
#         )
#         await interaction.followup.send(embed=embed, view=view)


# class MusicControlsView(discord.ui.View):
#     """Interactive music control panel with buttons."""

#     def __init__(self, cog: "Music", guild: discord.Guild) -> None:
#         super().__init__(timeout=180)
#         self.cog      = cog
#         self.guild_id = guild.id
#         self.bot      = cog.bot

#     def _guild(self) -> discord.Guild | None:
#         return self.bot.get_guild(self.guild_id)

#     def _player(self) -> wavelink.Player | None:
#         guild = self._guild()
#         return guild.voice_client if guild else None

#     async def _refresh(self, interaction: discord.Interaction) -> None:
#         player = self._player()
#         guild  = self._guild()
#         embed  = self.cog._controls_embed(guild) if guild else discord.Embed(title="🎵 Music Controls", description="Error getting guild.", color=ERROR_COLOR)
#         self._update_buttons(player)
#         await interaction.response.edit_message(embed=embed, view=self)

#     def _update_buttons(self, player: wavelink.Player | None) -> None:
#         playing = bool(player and (player.playing or player.paused))
#         paused  = bool(player and player.paused)
#         self.btn_pause.label    = "▶ Resume" if paused else "⏸ Pause"
#         self.btn_pause.style    = discord.ButtonStyle.success if paused else discord.ButtonStyle.secondary
#         self.btn_pause.disabled = not playing
#         self.btn_skip.disabled  = not playing
#         self.btn_stop.disabled  = not (player and player.connected)
#         self.btn_vol_down.disabled = not playing
#         self.btn_vol_up.disabled   = not playing

#     @discord.ui.button(label="⏸ Pause", style=discord.ButtonStyle.secondary, row=0)
#     async def btn_pause(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
#         await self.cog._do_pause(self._guild(), lambda *a, **kw: asyncio.sleep(0))
#         await self._refresh(interaction)

#     @discord.ui.button(label="⏭ Skip", style=discord.ButtonStyle.primary, row=0)
#     async def btn_skip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
#         await self.cog._do_skip(self._guild(), lambda *a, **kw: asyncio.sleep(0))
#         await self._refresh(interaction)

#     @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.danger, row=0)
#     async def btn_stop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
#         await self.cog._do_stop(self._guild(), lambda *a, **kw: asyncio.sleep(0))
#         await self._refresh(interaction)

#     @discord.ui.button(label="🔉 Vol -10", style=discord.ButtonStyle.secondary, row=1)
#     async def btn_vol_down(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
#         player = self._player()
#         if player:
#             new_vol = max(0, player.volume - 10)
#             await self.cog._do_volume(self._guild(), new_vol, lambda *a, **kw: asyncio.sleep(0))
#         await self._refresh(interaction)

#     @discord.ui.button(label="🔊 Vol +10", style=discord.ButtonStyle.secondary, row=1)
#     async def btn_vol_up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
#         player = self._player()
#         if player:
#             new_vol = min(100, player.volume + 10)
#             await self.cog._do_volume(self._guild(), new_vol, lambda *a, **kw: asyncio.sleep(0))
#         await self._refresh(interaction)

#     @discord.ui.button(label="🎶 Queue", style=discord.ButtonStyle.secondary, row=1)
#     async def btn_queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
#         player = self._player()
#         if not player or (not player.current and player.queue.is_empty):
#             await interaction.response.send_message("📭 The queue is empty.", ephemeral=True)
#             return
#         lines: list[str] = []
#         if player.current:
#             t = player.current
#             lines.append(f"**Now playing:** [{t.title}]({t.uri}) [{_fmt_duration(t.length)}]")
#         if not player.queue.is_empty:
#             lines.append("\n**Up next:**")
#             for i, t in enumerate(list(player.queue)[:10], 1):
#                 lines.append(f"`{i}.` **{t.title}** [{_fmt_duration(t.length)}]")
#             if len(player.queue) > 10:
#                 lines.append(f"… and {len(player.queue) - 10} more")
#         embed = discord.Embed(title="🎶 Queue", description="\n".join(lines), color=MUSIC_COLOR)
#         await interaction.response.send_message(embed=embed, ephemeral=True)

#     @discord.ui.button(label="🔍 Search", style=discord.ButtonStyle.primary, row=2)
#     async def btn_search(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
#         modal = SearchModal(cog=self.cog, guild=self._guild(), author=interaction.user)
#         await interaction.response.send_modal(modal)

#     @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=2)
#     async def btn_refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
#         await self._refresh(interaction)

#     async def on_timeout(self) -> None:
#         for item in self.children:
#             item.disabled = True


# # ══════════════════════════════════════════════════════════════════════════════
# # Cog
# # ══════════════════════════════════════════════════════════════════════════════

# class Music(commands.Cog):
#     """Voice channel music player powered by Lavalink via wavelink."""

#     def __init__(self, bot: commands.Bot) -> None:
#         self.bot = bot
#         self._last_requester: dict[int, int] = {}
#         self._last_request_time: float = 0.0
#         self._request_lock = asyncio.Lock()
#         # Tracks in-progress SoundCloud-fallback retries per guild, as
#         # {guild_id: {"query": str, "tried_urls": set[str]}}. This can't
#         # be carried on the track's `extras`/userData field instead,
#         # because Lavalink's TrackExceptionEvent payload only echoes back
#         # `encoded`/`info`/`pluginInfo` for the track — userData is NOT
#         # guaranteed to round-trip into event payloads (only into REST
#         # loadtracks responses and the outgoing player PATCH). So extras
#         # set before play() silently doesn't come back on the next
#         # exception for that track, and tagging state that way never
#         # actually told us "this is our own retry failing again." Plain
#         # per-guild state on the cog avoids relying on that round-trip.
#         self._sc_retry_state: dict[int, dict] = {}

#     # ── Feature toggle gate ───────────────────────────────────────────────────
#     # When MUSIC_FEATURE_DOWN is True, every prefix command (cog_check) and
#     # every slash command (interaction_check) in this cog responds with the
#     # "temporarily down" message instead of running the actual command.

#     # ── Feature toggle gate ───────────────────────────────────────────────────
#     # When MUSIC_FEATURE_DOWN is True, every normal prefix command (cog_check)
#     # and every normal slash command (interaction_check) in this cog responds
#     # with the "temporarily down" message instead of running — for EVERYONE,
#     # including the bot owner. Only the dedicated `force*` commands below
#     # (forcejoin, forceplay, forcestop, ...) bypass this, so the owner can
#     # still test the VC/music backend in private.

#     _FORCE_COMMAND_NAMES = {
#         "forcejoin", "forceplay", "forcestop", "forcepause", "forceresume",
#         "forceskip", "forcequeue", "forcenp", "forcevolume", "forcecontrols",
#     }

#     async def cog_check(self, ctx: commands.Context) -> bool:
#         if ctx.command and ctx.command.qualified_name in self._FORCE_COMMAND_NAMES:
#             return True
#         if MUSIC_FEATURE_DOWN:
#             await ctx.reply(embed=MUSIC_DOWN_EMBED)
#             return False
#         return True

#     async def interaction_check(self, interaction: discord.Interaction) -> bool:
#         cmd_name = interaction.command.name if interaction.command else ""
#         if cmd_name in self._FORCE_COMMAND_NAMES:
#             return True
#         if MUSIC_FEATURE_DOWN:
#             if interaction.response.is_done():
#                 await interaction.followup.send(embed=MUSIC_DOWN_EMBED, ephemeral=True)
#             else:
#                 await interaction.response.send_message(embed=MUSIC_DOWN_EMBED, ephemeral=True)
#             return False
#         return True

#     # ── Lavalink node setup ───────────────────────────────────────────────────

#     async def cog_load(self) -> None:
#         self.bot.loop.create_task(self._connect_lavalink())

#     async def _connect_lavalink(self) -> None:
#         await self.bot.wait_until_ready()
#         await asyncio.sleep(2)
#         nodes = [
#             wavelink.Node(
#                 identifier=n["identifier"],
#                 uri=f"{'https' if n.get('secure', False) else 'http'}://{n['host']}:{n['port']}",
#                 password=n["password"],
#             )
#             for n in LAVALINK_NODES
#         ]
#         await wavelink.Pool.connect(nodes=nodes, client=self.bot, cache_capacity=100)
#         print("✅ Music: connected to Lavalink!")
    

#     # ── wavelink events ───────────────────────────────────────────────────────

#     @commands.Cog.listener()
#     async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
#         # payload.player is typed Player | None by wavelink — it can be
#         # None, e.g. when this event fires as a side effect of tearing
#         # down a player during switch_node() (our SoundCloud fallback
#         # path triggers this: the old node's track-end event for the
#         # just-stopped track can arrive after the player's already been
#         # detached). Nothing to do if there's no player to act on.
#         player: wavelink.Player | None = payload.player
#         if player is None:
#             return

#         if player.guild is not None:
#             guild_id = player.guild.id
#             retry_state = self._sc_retry_state.get(guild_id)
#             # If the track that just ended cleanly (finished, replaced,
#             # or stopped — i.e. NOT an exception) is one of our own
#             # SoundCloud fallback attempts, that attempt is done and
#             # didn't need a further retry, so clear the state. We rely
#             # on track_end here rather than track_start, because
#             # track_start can fire essentially back-to-back with
#             # track_exception for a stream that opens but then
#             # immediately errors (e.g. the SoundCloud 404 case), which
#             # would otherwise wipe the retry state right before the
#             # exception handler needs to read it.
#             if retry_state is not None:
#                 ended_uri = getattr(payload.track, "uri", None)
#                 if ended_uri in retry_state.get("tried_urls", set()) and payload.reason != "loadFailed":
#                     self._sc_retry_state.pop(guild_id, None)

#         if not player.queue.is_empty:
#             await player.play(player.queue.get(), volume=100)
#             return

#         autoplay = bool(get_setting(f"autoplay_channel_{player.guild.id}", False))
#         if not autoplay:
#             return

#         user_id = self._last_requester.get(player.guild.id)
#         if not user_id:
#             return

#         history = get_song_history(user_id)
#         similar = await _find_similar_track(history)
#         if similar:
#             await player.play(similar, volume=100)

#     @commands.Cog.listener()
#     async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
#         player = payload.player
#         if player is None or player.guild is None:
#             return

#         guild_id = player.guild.id
#         retry_state = self._sc_retry_state.get(guild_id)
#         if retry_state is None:
#             return

#         # IMPORTANT: for a track whose stream opens but then errors
#         # immediately (e.g. the SoundCloud 404 case — Lavaplayer fires
#         # TrackStartEvent once the stream opens, then TrackExceptionEvent
#         # when the read itself fails a moment later), TrackStartEvent can
#         # arrive for the very fallback track we're mid-retry on, racing
#         # with the TrackExceptionEvent that's about to follow for it.
#         # If we clear retry state here unconditionally, the exception
#         # handler loses its "this is our own retry" signal right before
#         # it needs it, and silently gives up instead of trying the next
#         # candidate. So only clear when the track starting is NOT one of
#         # the URLs we ourselves just tried — i.e. it's a genuinely new,
#         # unrelated song (skip, new queue item, new /play command).
#         started_uri = getattr(payload.track, "uri", None)
#         if started_uri in retry_state.get("tried_urls", set()):
#             return  # this is our own in-flight retry attempt — leave state alone

#         self._sc_retry_state.pop(guild_id, None)

#     @commands.Cog.listener()
#     async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
#         print(f"✅ Lavalink node ready: {payload.node.uri}  (resumed={payload.resumed})")

#     @commands.Cog.listener()
#     async def on_wavelink_node_closed(self, payload: wavelink.NodeClosedEventPayload) -> None:
#         """
#         Fires when a Lavalink node disconnects. We schedule a reconnect so
#         that public/community nodes (which restart often) are transparently
#         re-established without manual intervention.
#         The session on the old node is gone — any player holding a reference
#         to it will get 404s until it is moved to a healthy node. We handle
#         that reactively in _do_play / _try_switch_to_healthy_node.
#         """
#         node = payload.node
#         print(f"⚠️  Lavalink node closed: {node.identifier} ({node.uri}) — scheduling reconnect...")

#         async def _reconnect() -> None:
#             for attempt in range(1, 6):
#                 await asyncio.sleep(5 * attempt)   # back-off: 5s, 10s, 15s, 20s, 25s
#                 try:
#                     fresh = wavelink.Node(
#                         identifier=node.identifier,
#                         uri=node.uri,
#                         password=node.password,
#                     )
#                     await wavelink.Pool.connect(nodes=[fresh], client=self.bot)
#                     print(f"✅  Reconnected to node {node.identifier} (attempt {attempt})")
#                     return
#                 except Exception as e:
#                     print(f"❌  Reconnect attempt {attempt} for {node.identifier} failed: {e}")
#             print(f"❌  Giving up reconnecting to {node.identifier} after 5 attempts.")

#         self.bot.loop.create_task(_reconnect())

#     @commands.Cog.listener()
#     async def on_wavelink_track_exception(
#         self, payload: wavelink.TrackExceptionEventPayload
#     ) -> None:
#         """
#         Fires when Lavalink throws a TrackException during playback.
#         If the error is a YouTube login/access block, we retry the same
#         song on the self-hosted SoundCloud node instead. If a SoundCloud
#         fallback track itself fails (stale/expired stream URL, 404, etc
#         — common on free public/community Lavalink nodes), we try the
#         next candidate from the original search instead of going silent.
#         """
#         player: wavelink.Player | None = payload.player
#         if player is None or player.guild is None:
#             return  # player already disconnected/cleaned up, nothing to retry

#         track = payload.track
#         guild_id = player.guild.id

#         # payload.exception is a TypedDict (plain dict), not an object —
#         # use dict access / .get(), not getattr().
#         exception_data = payload.exception or {}
#         msg = str(exception_data.get("message") or exception_data).lower()

#         # Is this exception firing on a track we ourselves are already
#         # retrying (i.e. a previous SoundCloud fallback that also
#         # failed)? We track this via self._sc_retry_state, keyed by
#         # guild — NOT via track.extras/userData. Lavalink's
#         # TrackExceptionEvent payload only echoes back encoded/info/
#         # pluginInfo for the track; userData is not guaranteed to round
#         # -trip into event payloads (only into REST loadtracks responses
#         # and the outgoing player PATCH), so anything stashed in extras
#         # before play() does not reliably come back to us here.
#         retry_state = self._sc_retry_state.get(guild_id)
#         is_sc_retry = retry_state is not None

#         if not is_sc_retry:
#             is_yt_block = any(
#                 k in msg for k in ("login", "not available", "sign in", "403", "blocked")
#             )
#             if not is_yt_block:
#                 return  # some other error on the original track — let it bubble up

#             current_node_id = getattr(player.node, "identifier", "unknown")
#             print(
#                 f"⚠️  YouTube blocked '{track.title}' "
#                 f"(node={current_node_id}) — retrying on SoundCloud node..."
#             )
#         else:
#             print(
#                 f"⚠️  SoundCloud fallback '{track.title}' failed to play "
#                 f"({msg or 'unknown error'}) — trying next candidate..."
#             )

#         # Pool.get_node() raises InvalidNodeException if the node isn't
#         # found/connected — it does NOT return None. Catch that instead
#         # of checking for None.
#         try:
#             sc_node: wavelink.Node = wavelink.Pool.get_node(NODE_SC)
#         except Exception:
#             print("❌  SC fallback node not connected, cannot retry.")
#             self._sc_retry_state.pop(guild_id, None)
#             return

#         # Figure out which candidates we've already tried for this song,
#         # so a failed fallback retry moves on to the next result instead
#         # of re-trying (and re-failing on) the same broken stream, or
#         # re-running an identical search from scratch. Also carry the
#         # ORIGINAL track's duration through retries (not the failed SC
#         # fallback's duration) so we can keep filtering candidates by
#         # how close they are to the real song length, even on a second
#         # or third retry attempt.
#         if is_sc_retry:
#             query = retry_state["query"]
#             tried_urls: set[str] = set(retry_state["tried_urls"])
#             original_length_ms: int | None = retry_state.get("original_length_ms")
#         else:
#             query = _clean_search_query(track.author, track.title)
#             tried_urls = set()
#             original_length_ms = getattr(track, "length", None)
#         if getattr(track, "uri", None):
#             tried_urls.add(track.uri)

#         async def _search_soundcloud() -> list[wavelink.Playable]:
#             try:
#                 results = await wavelink.Playable.search(
#                     query, source=wavelink.TrackSource.SoundCloud, node=sc_node
#                 )
#             except Exception as e:
#                 print(f"❌  SoundCloud search failed for '{query}': {e}")
#                 return []
#             if not results:
#                 print(f"❌  No SoundCloud result found for '{query}'")
#                 return []
#             candidates = results if isinstance(results, list) else results.tracks

#             # Skip candidates we've already tried and failed on.
#             candidates = [c for c in candidates if getattr(c, "uri", None) not in tried_urls]

#             # Skip obvious remixes/edits — these are a different audio
#             # file from the original, not just a different upload of
#             # the same one (e.g. "(sped up + reverb)", "(nightcore)").
#             # We filter on title text rather than only relying on
#             # duration, since some edits (loops, extended versions)
#             # don't shift duration enough to be caught by that alone.
#             candidates = [c for c in candidates if not _REMIX_NOISE_RE.search(c.title)]

#             # If we know the original track's length, prefer candidates
#             # within a reasonable tolerance of it — sped-up versions run
#             # short, slowed/reverb versions run long, and either is a
#             # clear sign it's not the same recording.
#             if original_length_ms:
#                 tolerance_ms = max(15_000, int(original_length_ms * 0.15))
#                 close_matches = [
#                     c for c in candidates
#                     if abs(getattr(c, "length", original_length_ms) - original_length_ms) <= tolerance_ms
#                 ]
#                 if close_matches:
#                     candidates = close_matches
#                 # If nothing is close, fall through to the unfiltered
#                 # list rather than returning empty — a slightly-off
#                 # duration is still better than no fallback at all.

#             return candidates

#         async def _switch_node() -> bool:
#             if player.node.identifier == sc_node.identifier:
#                 return True
#             # `player.node` is a read-only property — it has no setter.
#             # The correct way to move a connected player to a different
#             # node is Player.switch_node(), added in wavelink 3.5.0.
#             # We do NOT touch player._current here — mutating private
#             # state is unsafe and can corrupt the player if wavelink
#             # internals change. switch_node() handles it correctly on
#             # its own.
#             try:
#                 await player.switch_node(sc_node)
#             except Exception as e:
#                 print(f"❌  Failed to switch player to SC node: {e}")
#                 return False
#             return True

#         candidates, switch_ok = await asyncio.gather(
#             _search_soundcloud(), _switch_node()
#         )

#         if not candidates or not switch_ok:
#             if not candidates:
#                 print(f"❌  No more untried SoundCloud candidates for '{query}' — giving up.")
#             self._sc_retry_state.pop(guild_id, None)
#             return  # specific reason already logged

#         fallback = candidates[0]
#         tried_urls.add(fallback.uri)
#         # Record this attempt so that if THIS fallback also throws an
#         # exception, the handler recognizes it as our own retry (see
#         # the is_sc_retry check near the top) and moves on to the next
#         # untried candidate instead of either looping on the same dead
#         # stream or mistaking the error for a fresh YouTube block.
#         self._sc_retry_state[guild_id] = {
#             "query": query,
#             "tried_urls": tried_urls,
#             "original_length_ms": original_length_ms,
#         }

#         await player.play(fallback, volume=player.volume)
#         print(f"✅  Now playing SoundCloud fallback: {fallback.title}")

#     # ── Internal helpers ──────────────────────────────────────────────────────

#     async def _ensure_healthy_node(self, player: wavelink.Player) -> bool:
#         """Return True if the player's current node is alive and connected."""
#         try:
#             return player.node.status == wavelink.NodeStatus.CONNECTED
#         except Exception:
#             return False

#     async def _try_switch_to_healthy_node(self, player: wavelink.Player) -> bool:
#         """
#         If the player's current node is unhealthy, try switching to any
#         connected public YT node. Returns True if the player ends up on a
#         healthy node (whether it was already fine or we switched), False if
#         no healthy node could be found.
#         """
#         if await self._ensure_healthy_node(player):
#             return True
#         for n in PUBLIC_YT_NODES:
#             try:
#                 node = wavelink.Pool.get_node(n["identifier"])
#             except Exception:
#                 continue
#             if node and node.status == wavelink.NodeStatus.CONNECTED:
#                 try:
#                     await player.switch_node(node)
#                     print(f"[HealthCheck] Switched player to healthy node: {node.identifier}")
#                     return True
#                 except Exception as e:
#                     print(f"[HealthCheck] Could not switch to {node.identifier}: {e}")
#         return False

#     async def _get_player(
#         self,
#         guild:  discord.Guild,
#         author: discord.Member,
#         send_fn,
#         *,
#         connect: bool = True,
#     ) -> wavelink.Player | None:
#         """Return (or create) a Player for this guild. Joins VC if needed."""
#         player: wavelink.Player | None = cast(
#             wavelink.Player | None, guild.voice_client
#         )

#         if not connect:
#             return player

#         if not author.voice or not author.voice.channel:
#             await send_fn(embed=_err("You need to be in a voice channel first!"))
#             return None

#         channel = author.voice.channel

#         if player is None:
#             try:
#                 player = await channel.connect(cls=wavelink.Player, self_deaf=True)
#             except Exception as e:
#                 await send_fn(embed=_err(f"Couldn't connect to VC: {e}"))
#                 return None
#         elif player.channel != channel:
#             await player.move_to(channel)

#         return player

#     # ── Core play logic ───────────────────────────────────────────────────────

#     def _controls_embed(self, guild: discord.Guild) -> discord.Embed:
#         """Build the now-playing embed for the controls panel."""
#         player: wavelink.Player | None = guild.voice_client
#         print(f"[Controls] guild={guild.id} player={player} current={getattr(player, 'current', None)} playing={getattr(player, 'playing', None)}")
#         if not player or not player.current:
#             embed = discord.Embed(
#                 title       = "🎵 Music Controls",
#                 description = "Nothing is playing right now.",
#                 color       = MUSIC_COLOR,
#             )
#             return embed
#         t = player.current
#         status = "⏸ Paused" if player.paused else "▶ Playing"
#         embed = discord.Embed(
#             title       = "🎵 Music Controls",
#             description = f"**[{t.title}]({t.uri})**",
#             color       = MUSIC_COLOR,
#         )
#         embed.add_field(name="Status",   value=status,                    inline=True)
#         embed.add_field(name="Duration", value=_fmt_duration(t.length),   inline=True)
#         embed.add_field(name="Volume",   value=f"{player.volume}%",       inline=True)
#         embed.add_field(name="Author",   value=t.author or "?",           inline=True)
#         embed.add_field(name="Queue",    value=f"{len(player.queue)} songs", inline=True)
#         if t.artwork:
#             embed.set_thumbnail(url=t.artwork)
#         embed.set_footer(text="Controls auto-update when you click buttons")
#         return embed

#     # ── Request pacing ─────────────────────────────────────────────────────────

#     async def _pace_request(self, guild_id: int = 0) -> None:
#         """
#         Sleep just enough so consecutive track-load requests sent to Lavalink
#         are spaced at least MIN_TRACK_LOAD_GAP seconds apart — GLOBALLY,
#         across all guilds/users, since they all share the same Lavalink node.
#         Uses a lock so concurrent requests queue up and wait their turn
#         instead of all checking the timestamp simultaneously.
#         """
#         async with self._request_lock:
#             now = asyncio.get_event_loop().time()
#             wait = MIN_TRACK_LOAD_GAP - (now - self._last_request_time)
#             if wait > 0:
#                 await asyncio.sleep(wait)
#             self._last_request_time = asyncio.get_event_loop().time()

#     async def _do_play(self, guild, author, query: str, send_fn) -> None:
#         player = await self._get_player(guild, author, send_fn)
#         if not player:
#             return

#         await self._pace_request(guild.id)

#         tracks, source_used, yt_node = await _smart_search(query)
#         if not tracks:
#             await send_fn(embed=_err(
#                 f"No results found for **{query}**\n"
#                 "Try a Spotify/Deezer/SoundCloud link, or a different search term."
#             ))
#             return

#         track: wavelink.Playable = tracks[0] if isinstance(tracks, list) else tracks.tracks[0]

#         # Source label for user feedback
#         source_labels = {
#             "youtube": "YouTube", "soundcloud": "SoundCloud",
#             "deezer": "Deezer", "spotify": "Spotify",
#             "applemusic": "Apple Music", "none": "Unknown"
#         }
#         source_label = source_labels.get(source_used, source_used.title())

#         if player.playing or player.paused:
#             player.queue.put(track)
#             embed = discord.Embed(
#                 title       = "➕ Added to Queue",
#                 description = f"**[{track.title}]({track.uri})**\n"
#                               f"Position: `#{len(player.queue)}`  |  {_fmt_duration(track.length)}",
#                 color       = MUSIC_COLOR,
#             )
#             embed.set_footer(text=f"Source: {source_label}")
#             if track.artwork:
#                 embed.set_thumbnail(url=track.artwork)
#             await send_fn(embed=embed)
#         else:
#             if yt_node is not None and yt_node.identifier != player.node.identifier:
#                 try:
#                     await player.switch_node(yt_node)
#                     print(f"[Play] Switched player to node: {yt_node.identifier}")
#                 except Exception as e:
#                     print(f"[Play] Could not switch node: {e}")

#             # Ensure the node is healthy before attempting playback.
#             # Public/community Lavalink nodes restart often, which invalidates
#             # their session. A stale session causes a 404 on the first play()
#             # call after the restart — we catch that here and reconnect.
#             if not await self._try_switch_to_healthy_node(player):
#                 print(f"[Play] No healthy node available, attempting full reconnect...")
#                 try:
#                     await player.disconnect()
#                 except Exception:
#                     pass
#                 player = await self._get_player(guild, author, send_fn)
#                 if not player:
#                     await send_fn(embed=_err("Lost connection to the audio server. Please try again."))
#                     return

#             try:
#                 print(f"[Play] Attempting playback on node: {player.node.identifier}")
#                 await player.play(track, volume=100)
#                 print(f"[Play] Success on node: {player.node.identifier}")
#             except Exception as e:
#                 err_str = str(e)
#                 print(f"[Play] Playback error on node {player.node.identifier}: {e}")
#                 # 404 / Not Found = Lavalink session expired (node restarted).
#                 # Disconnect the stale player, reconnect fresh, and retry once.
#                 if "404" in err_str or "Not Found" in err_str or "session" in err_str.lower():
#                     print(f"[Play] Session expired — reconnecting and retrying...")
#                     try:
#                         await player.disconnect()
#                     except Exception:
#                         pass
#                     player = await self._get_player(guild, author, send_fn)
#                     if not player:
#                         await send_fn(embed=_err("Lost connection to the audio server. Please try again."))
#                         return
#                     try:
#                         await player.play(track, volume=100)
#                         print(f"[Play] Retry succeeded on node: {player.node.identifier}")
#                     except Exception as retry_err:
#                         print(f"[Play] Retry also failed: {retry_err}")
#                         await send_fn(embed=_err(f"Playback failed after reconnect: `{retry_err}`"))
#                         return
#                 else:
#                     await send_fn(embed=_err(f"Playback failed: `{e}`"))
#                     return

#             self._last_requester[player.guild.id] = author.id
#             append_song_history(author.id, _track_info(track))
#             embed = _track_embed(track, requester=author)
#             embed.set_footer(text=f"Source: {source_label}")
#             await send_fn(embed=embed)

#     async def _do_skip(self, guild, send_fn) -> None:
#         player: wavelink.Player | None = guild.voice_client
#         if not player or not (player.playing or player.paused):
#             await send_fn(embed=_err("Nothing is playing right now."))
#             return
#         await player.skip(force=True)
#         await send_fn("⏭️ Skipped!")

#     async def _do_stop(self, guild, send_fn) -> None:
#         player: wavelink.Player | None = guild.voice_client
#         if not player:
#             await send_fn(embed=_err("I'm not in a voice channel."))
#             return
#         player.queue.clear()
#         await player.stop()
#         await player.disconnect()
#         await send_fn("⏹️ Stopped and disconnected.")

#     async def _do_pause(self, guild, send_fn) -> None:
#         player: wavelink.Player | None = guild.voice_client
#         if not player or not player.playing:
#             await send_fn(embed=_err("Nothing is playing."))
#             return
#         await player.pause(not player.paused)
#         await send_fn("⏸️ Paused." if player.paused else "▶️ Resumed.")

#     async def _do_queue(self, guild, send_fn) -> None:
#         player: wavelink.Player | None = guild.voice_client
#         if not player or (not player.current and player.queue.is_empty):
#             await send_fn("📭 The queue is empty.")
#             return

#         lines: list[str] = []
#         if player.current:
#             t = player.current
#             lines.append(f"**Now playing:** [{t.title}]({t.uri}) [{_fmt_duration(t.length)}]")
#         if not player.queue.is_empty:
#             lines.append("\n**Up next:**")
#             for i, t in enumerate(list(player.queue)[:10], 1):
#                 lines.append(f"`{i}.` **{t.title}** [{_fmt_duration(t.length)}]")
#             if len(player.queue) > 10:
#                 lines.append(f"… and {len(player.queue) - 10} more")

#         embed = discord.Embed(title="🎶 Queue", description="\n".join(lines), color=MUSIC_COLOR)
#         await send_fn(embed=embed)

#     async def _do_np(self, guild, send_fn) -> None:
#         player: wavelink.Player | None = guild.voice_client
#         if not player or not player.current:
#             await send_fn(embed=_err("Nothing is playing right now."))
#             return
#         await send_fn(embed=_track_embed(player.current))

#     async def _do_volume(self, guild, vol: int, send_fn) -> None:
#         if not (0 <= vol <= 100):
#             await send_fn(embed=_err("Volume must be between 0 and 100."))
#             return
#         player: wavelink.Player | None = guild.voice_client
#         if not player:
#             await send_fn(embed=_err("I'm not in a voice channel."))
#             return
#         await player.set_volume(vol)
#         await send_fn(f"🔊 Volume set to **{vol}%**")

#     # ── Controls panel ───────────────────────────────────────────────────────────

#     async def _send_controls(self, guild, send_fn) -> None:
#         view   = MusicControlsView(cog=self, guild=guild)
#         player = guild.voice_client
#         view._update_buttons(player)
#         embed  = self._controls_embed(guild)
#         await send_fn(embed=embed, view=view)

#     # ── Slash commands ────────────────────────────────────────────────────────

#     @app_commands.command(name="controls", description="Open the music control panel 🎛️")
#     async def slash_controls(self, interaction: discord.Interaction) -> None:
#         await interaction.response.defer(thinking=True)
#         await self._send_controls(interaction.guild, interaction.followup.send)

#     @app_commands.command(name="play", description="Play a song in your voice channel 🎵")
#     @app_commands.describe(query="Song name or YouTube URL")
#     async def slash_play(self, interaction: discord.Interaction, query: str) -> None:
#         await interaction.response.defer(thinking=True)
#         await self._do_play(interaction.guild, interaction.user, query.strip(),
#                             interaction.followup.send)

#     @app_commands.command(name="skip", description="Skip the current song ⏭️")
#     async def slash_skip(self, interaction: discord.Interaction) -> None:
#         await interaction.response.defer(thinking=True)
#         await self._do_skip(interaction.guild, interaction.followup.send)

#     @app_commands.command(name="stop", description="Stop music and disconnect ⏹️")
#     async def slash_stop(self, interaction: discord.Interaction) -> None:
#         await interaction.response.defer(thinking=True)
#         await self._do_stop(interaction.guild, interaction.followup.send)

#     @app_commands.command(name="pause", description="Pause or resume the current song ⏸️")
#     async def slash_pause(self, interaction: discord.Interaction) -> None:
#         await interaction.response.defer(thinking=True)
#         await self._do_pause(interaction.guild, interaction.followup.send)

#     @app_commands.command(name="queue", description="Show the song queue 🎶")
#     async def slash_queue(self, interaction: discord.Interaction) -> None:
#         await interaction.response.defer(thinking=True)
#         await self._do_queue(interaction.guild, interaction.followup.send)

#     @app_commands.command(name="nowplaying", description="Show what's currently playing 🎵")
#     async def slash_np(self, interaction: discord.Interaction) -> None:
#         await interaction.response.defer(thinking=True)
#         await self._do_np(interaction.guild, interaction.followup.send)

#     @app_commands.command(name="volume", description="Set playback volume (0–100) 🔊")
#     @app_commands.describe(level="Volume level 0–100")
#     async def slash_volume(self, interaction: discord.Interaction, level: int) -> None:
#         await interaction.response.defer(thinking=True)
#         await self._do_volume(interaction.guild, level, interaction.followup.send)

#     # ── Prefix commands ───────────────────────────────────────────────────────

#     @commands.command(name="controls", aliases=["ctrl", "panel", "cp"])
#     async def prefix_controls(self, ctx: commands.Context) -> None:
#         await self._send_controls(ctx.guild, ctx.reply)

#     @commands.command(name="play", aliases=["p"])
#     async def prefix_play(self, ctx: commands.Context, *, query: str = "") -> None:
#         if not query:
#             await ctx.reply("**Usage:** `!play <song name or YouTube URL>`")
#             return
#         async with ctx.typing():
#             await self._do_play(ctx.guild, ctx.author, query.strip(), ctx.reply)

#     @commands.command(name="skip", aliases=["s"])
#     async def prefix_skip(self, ctx: commands.Context) -> None:
#         await self._do_skip(ctx.guild, ctx.reply)

#     @commands.command(name="stop")
#     async def prefix_stop(self, ctx: commands.Context) -> None:
#         await self._do_stop(ctx.guild, ctx.reply)

#     @commands.command(name="pause")
#     async def prefix_pause(self, ctx: commands.Context) -> None:
#         await self._do_pause(ctx.guild, ctx.reply)

#     @commands.command(name="resume")
#     async def prefix_resume(self, ctx: commands.Context) -> None:
#         player: wavelink.Player | None = ctx.guild.voice_client
#         if not player or not player.paused:
#             await ctx.reply(embed=_err("Nothing is paused."))
#             return
#         await player.pause(False)
#         await ctx.reply("▶️ Resumed.")

#     @commands.command(name="queue", aliases=["q"])
#     async def prefix_queue(self, ctx: commands.Context) -> None:
#         await self._do_queue(ctx.guild, ctx.reply)

#     @commands.command(name="np", aliases=["nowplaying"])
#     async def prefix_np(self, ctx: commands.Context) -> None:
#         await self._do_np(ctx.guild, ctx.reply)

#     @commands.command(name="volume", aliases=["vol"])
#     async def prefix_volume(self, ctx: commands.Context, level: int = -1) -> None:
#         if level == -1:
#             player: wavelink.Player | None = ctx.guild.voice_client
#             vol = player.volume if player else 100
#             await ctx.reply(f"🔊 Current volume: **{vol}%**")
#             return
#         await self._do_volume(ctx.guild, level, ctx.reply)

#     @commands.group(name="playlist", invoke_without_command=True)
#     async def prefix_playlist(self, ctx: commands.Context) -> None:
#         await ctx.reply(
#             "**Playlist commands:** `!playlist list`, `!playlist show <name>`, "
#             "`!playlist play <name>`, `!playlist add <name> <song>`, "
#             "`!playlist remove <name> <index>`, `!playlist delete <name>`"
#         )

#     @prefix_playlist.command(name="list")
#     async def playlist_list(self, ctx: commands.Context) -> None:
#         playlists = get_user_playlists(ctx.author.id)
#         if not playlists:
#             await ctx.reply("📂 You don't have any playlists yet.")
#             return
#         names = "\n".join(f"• {name}" for name in playlists)
#         await ctx.reply(embed=discord.Embed(title="Your Playlists", description=names, color=MUSIC_COLOR))

#     @prefix_playlist.command(name="show")
#     async def playlist_show(self, ctx: commands.Context, name: str) -> None:
#         playlists = get_user_playlists(ctx.author.id)
#         key = next((k for k in playlists if k.lower() == name.lower()), None)
#         if key is None:
#             await ctx.reply(f"❌ Playlist `{name}` not found.")
#             return
#         tracks = playlists[key]
#         if not tracks:
#             await ctx.reply(f"📂 Playlist `{key}` is empty.")
#             return
#         lines = [f"`{i+1}.` **{t['title']}**" for i, t in enumerate(tracks[:20])]
#         if len(tracks) > 20:
#             lines.append(f"… and {len(tracks) - 20} more")
#         await ctx.reply(embed=discord.Embed(title=f"Playlist: {key}", description="\n".join(lines), color=MUSIC_COLOR))

#     @prefix_playlist.command(name="play")
#     async def playlist_play(self, ctx: commands.Context, name: str) -> None:
#         playlists = get_user_playlists(ctx.author.id)
#         key = next((k for k in playlists if k.lower() == name.lower()), None)
#         if key is None:
#             await ctx.reply(f"❌ Playlist `{name}` not found.")
#             return
#         tracks = playlists[key]
#         if not tracks:
#             await ctx.reply(f"📂 Playlist `{key}` is empty.")
#             return

#         player = await self._get_player(ctx.guild, ctx.author, ctx.reply)
#         if not player:
#             return

#         playable_tracks: list[wavelink.Playable] = []
#         for info in tracks:
#             await self._pace_request(ctx.guild.id)
#             resolved = await _resolve_saved_track(info)
#             if resolved:
#                 playable_tracks.append(resolved)

#         if not playable_tracks:
#             await ctx.reply("❌ Could not load any songs from that playlist.")
#             return

#         if player.playing or player.paused:
#             for track in playable_tracks:
#                 player.queue.put(track)
#             await ctx.reply(f"✅ Added **{len(playable_tracks)}** songs from `{key}` to the queue.")
#         else:
#             await player.play(playable_tracks[0], volume=100)
#             self._last_requester[ctx.guild.id] = ctx.author.id
#             append_song_history(ctx.author.id, _track_info(playable_tracks[0]))
#             for track in playable_tracks[1:]:
#                 player.queue.put(track)
#             await ctx.reply(embed=_track_embed(playable_tracks[0], requester=ctx.author))

#     @prefix_playlist.command(name="add")
#     async def playlist_add(self, ctx: commands.Context, name: str, *, query: str) -> None:
#         tracks, _, _ = await _smart_search(query)
#         if not tracks:
#             await ctx.reply(f"❌ No results found for **{query}**")
#             return
#         track = tracks[0] if isinstance(tracks, list) else tracks.tracks[0]
#         playlist = get_user_playlists(ctx.author.id)
#         key = next((k for k in playlist if k.lower() == name.lower()), name)
#         tracks_data = playlist.get(key, [])
#         tracks_data.append(_track_info(track))
#         set_user_playlist(ctx.author.id, key, tracks_data)
#         await ctx.reply(f"✅ Added **{track.title}** to playlist `{key}`.")

#     @prefix_playlist.command(name="remove")
#     async def playlist_remove(self, ctx: commands.Context, name: str, index: int) -> None:
#         playlists = get_user_playlists(ctx.author.id)
#         key = next((k for k in playlists if k.lower() == name.lower()), None)
#         if key is None:
#             await ctx.reply(f"❌ Playlist `{name}` not found.")
#             return
#         tracks = playlists[key]
#         if not (1 <= index <= len(tracks)):
#             await ctx.reply("❌ Invalid track index.")
#             return
#         removed = tracks.pop(index - 1)
#         set_user_playlist(ctx.author.id, key, tracks)
#         await ctx.reply(f"✅ Removed **{removed['title']}** from `{key}`.")

#     @prefix_playlist.command(name="delete")
#     async def playlist_delete(self, ctx: commands.Context, name: str) -> None:
#         if not delete_user_playlist(ctx.author.id, name if name in get_user_playlists(ctx.author.id) else next((k for k in get_user_playlists(ctx.author.id) if k.lower() == name.lower()), name)):
#             await ctx.reply(f"❌ Playlist `{name}` not found.")
#             return
#         await ctx.reply(f"✅ Deleted playlist `{name}`.")

#     # ── Owner-only testing commands ─────────────────────────────────────────
#     # These bypass MUSIC_FEATURE_DOWN entirely (cog_check already allows the
#     # bot owner through), so you can test the VC/music backend even while it
#     # shows "temporarily down" to everyone else.

#     @commands.command(name="forcejoin", aliases=["fjoin"])
#     @commands.is_owner()
#     async def prefix_forcejoin(self, ctx: commands.Context) -> None:
#         """Owner-only: force the bot to join your current voice channel."""
#         player = await self._get_player(ctx.guild, ctx.author, ctx.reply)
#         if not player:
#             return
#         await ctx.reply(f"✅ Joined **{player.channel.name}** (force).")

#     @commands.command(name="forceplay", aliases=["fplay"])
#     @commands.is_owner()
#     async def prefix_forceplay(self, ctx: commands.Context, *, query: str = "") -> None:
#         """Owner-only: force-play a track, bypassing the feature-down message."""
#         if not query:
#             await ctx.reply("**Usage:** `!forceplay <song name or YouTube URL>`")
#             return
#         async with ctx.typing():
#             await self._do_play(ctx.guild, ctx.author, query.strip(), ctx.reply)

#     @app_commands.command(name="forcejoin", description="(Owner) Force the bot to join your VC")
#     async def slash_forcejoin(self, interaction: discord.Interaction) -> None:
#         if not await self.bot.is_owner(interaction.user):
#             await interaction.response.send_message("❌ Owner only.", ephemeral=True)
#             return
#         await interaction.response.defer(thinking=True)
#         player = await self._get_player(interaction.guild, interaction.user, interaction.followup.send)
#         if not player:
#             return
#         await interaction.followup.send(f"✅ Joined **{player.channel.name}** (force).")

#     @app_commands.command(name="forceplay", description="(Owner) Force-play a track, bypassing feature-down")
#     @app_commands.describe(query="Song name or YouTube URL")
#     async def slash_forceplay(self, interaction: discord.Interaction, query: str) -> None:
#         if not await self.bot.is_owner(interaction.user):
#             await interaction.response.send_message("❌ Owner only.", ephemeral=True)
#             return
#         await interaction.response.defer(thinking=True)
#         await self._do_play(interaction.guild, interaction.user, query.strip(), interaction.followup.send)

#     @commands.command(name="forcestop", aliases=["fstop"])
#     @commands.is_owner()
#     async def prefix_forcestop(self, ctx: commands.Context) -> None:
#         """Owner-only: force-stop and disconnect, bypassing feature-down."""
#         await self._do_stop(ctx.guild, ctx.reply)

#     @app_commands.command(name="forcestop", description="(Owner) Force stop and disconnect")
#     async def slash_forcestop(self, interaction: discord.Interaction) -> None:
#         if not await self.bot.is_owner(interaction.user):
#             await interaction.response.send_message("❌ Owner only.", ephemeral=True)
#             return
#         await interaction.response.defer(thinking=True)
#         await self._do_stop(interaction.guild, interaction.followup.send)

#     @commands.command(name="forcepause", aliases=["fpause"])
#     @commands.is_owner()
#     async def prefix_forcepause(self, ctx: commands.Context) -> None:
#         """Owner-only: force pause/resume toggle, bypassing feature-down."""
#         await self._do_pause(ctx.guild, ctx.reply)

#     @app_commands.command(name="forcepause", description="(Owner) Force pause/resume toggle")
#     async def slash_forcepause(self, interaction: discord.Interaction) -> None:
#         if not await self.bot.is_owner(interaction.user):
#             await interaction.response.send_message("❌ Owner only.", ephemeral=True)
#             return
#         await interaction.response.defer(thinking=True)
#         await self._do_pause(interaction.guild, interaction.followup.send)

#     @commands.command(name="forceresume", aliases=["fresume"])
#     @commands.is_owner()
#     async def prefix_forceresume(self, ctx: commands.Context) -> None:
#         """Owner-only: force resume playback, bypassing feature-down."""
#         player: wavelink.Player | None = ctx.guild.voice_client
#         if not player or not player.paused:
#             await ctx.reply(embed=_err("Nothing is paused."))
#             return
#         await player.pause(False)
#         await ctx.reply("▶️ Resumed (force).")

#     @commands.command(name="forceskip", aliases=["fskip"])
#     @commands.is_owner()
#     async def prefix_forceskip(self, ctx: commands.Context) -> None:
#         """Owner-only: force skip the current track, bypassing feature-down."""
#         await self._do_skip(ctx.guild, ctx.reply)

#     @app_commands.command(name="forceskip", description="(Owner) Force skip the current track")
#     async def slash_forceskip(self, interaction: discord.Interaction) -> None:
#         if not await self.bot.is_owner(interaction.user):
#             await interaction.response.send_message("❌ Owner only.", ephemeral=True)
#             return
#         await interaction.response.defer(thinking=True)
#         await self._do_skip(interaction.guild, interaction.followup.send)

#     @commands.command(name="forcequeue", aliases=["fqueue", "fq"])
#     @commands.is_owner()
#     async def prefix_forcequeue(self, ctx: commands.Context) -> None:
#         """Owner-only: force show the queue, bypassing feature-down."""
#         await self._do_queue(ctx.guild, ctx.reply)

#     @commands.command(name="forcenp", aliases=["fnp"])
#     @commands.is_owner()
#     async def prefix_forcenp(self, ctx: commands.Context) -> None:
#         """Owner-only: force show now-playing, bypassing feature-down."""
#         await self._do_np(ctx.guild, ctx.reply)

#     @commands.command(name="forcevolume", aliases=["fvol"])
#     @commands.is_owner()
#     async def prefix_forcevolume(self, ctx: commands.Context, level: int) -> None:
#         """Owner-only: force set volume, bypassing feature-down."""
#         await self._do_volume(ctx.guild, level, ctx.reply)

#     @commands.command(name="forcecontrols", aliases=["fctrl", "fcp"])
#     @commands.is_owner()
#     async def prefix_forcecontrols(self, ctx: commands.Context) -> None:
#         """Owner-only: force open the music control panel, bypassing feature-down."""
#         await self._send_controls(ctx.guild, ctx.reply)

#     @commands.command(name="autoplay")
#     async def prefix_autoplay(self, ctx: commands.Context, value: str = None) -> None:
#         key = f"autoplay_channel_{ctx.guild.id}"
#         current = bool(get_setting(key, False))
#         if value is None:
#             await ctx.reply(f"🔁 Autoplay is currently `{'on' if current else 'off'}` for this channel.")
#             return
#         normalized = value.lower()
#         if normalized in {"on", "true", "yes", "1"}:
#             set_setting(key, True)
#             await ctx.reply("✅ Autoplay enabled for this channel.")
#         elif normalized in {"off", "false", "no", "0"}:
#             set_setting(key, False)
#             await ctx.reply("✅ Autoplay disabled for this channel.")
#         else:
#             await ctx.reply("❌ Invalid value. Use `on` or `off`.")

#     # ── Auto-disconnect when VC empties ───────────────────────────────────────

#     @commands.Cog.listener()
#     async def on_voice_state_update(
#         self,
#         member: discord.Member,
#         before: discord.VoiceState,
#         after:  discord.VoiceState,
#     ) -> None:
#         if member.bot:
#             return
#         player: wavelink.Player | None = member.guild.voice_client
#         if not player:
#             return
#         if before.channel == player.channel and len(player.channel.members) == 1:
#             await asyncio.sleep(30)
#             if player.channel and len(player.channel.members) == 1:
#                 player.queue.clear()
#                 await player.disconnect()


# # ══════════════════════════════════════════════════════════════════════════════
# # Setup
# # ══════════════════════════════════════════════════════════════════════════════

# async def setup(bot: commands.Bot) -> None:
#     await bot.add_cog(Music(bot))
#     print("✅ Music cog loaded")

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
FFMPEG_EXECUTABLE = _os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg") or "ffmpeg"

YTDLP_FORMAT_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",  # avoid ipv6 issues on some hosts
    "extract_flat": False,
}

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


def _ensure_opus_loaded() -> None:
    """
    discord.py needs the native libopus library loaded before any audio can
    be encoded for voice. On Windows, discord.opus.load_default() finds the
    bundled DLL automatically — but on Linux/Mac there's no auto-detection,
    so VoiceClient.play() raises a bare `OpusNotLoaded()` (which prints as
    an EMPTY string, e.g. "[Play] Playback error: ") if nothing loads it
    first. We try a few common library names so this works out of the box
    on most distros without the user having to call discord.opus.load_opus()
    themselves.
    """
    if discord.opus.is_loaded():
        return
    for name in _OPUS_CANDIDATES:
        try:
            discord.opus.load_opus(name)
            print(f"✅ Music: loaded libopus via '{name}'")
            return
        except OSError:
            continue
    print(
        "⚠️  Music: could not auto-load libopus — voice playback will fail "
        "with a blank 'OpusNotLoaded' error. Install it via your package "
        "manager (e.g. `apt install libopus0` / `brew install opus`), or "
        "set a custom path with discord.opus.load_opus('<path-to-lib>')."
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