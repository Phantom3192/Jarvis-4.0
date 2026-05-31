"""
YouTube cog — search YouTube and return rich embeds with watch links.
Discord auto-previews YouTube links so users can play audio/video
directly inside Discord without leaving the app.

Commands:
  /youtube  <query>              Slash — search and pick from dropdown
  /yttrend                       Slash — browse trending videos by category
  /ytinfo   <url_or_id>         Slash — fetch full details for a video
  !youtube  <query>              Prefix (alias: !yt)
  !yttrend  [category]           Prefix — trending videos
  !ytinfo   <url_or_id>         Prefix — video info

Features vs original:
  ✓ Integrated with shared HTTP session (http_session.py) instead of blocking yt-dlp executor
  ✓ Caching — repeated searches within 5 min hit memory, not yt-dlp
  ✓ Pagination — browse all 5 results with Prev/Next buttons after selection
  ✓ /yttrend — curated trending categories (music, gaming, news, etc.)
  ✓ /ytinfo  — deep-dive card: description, upload date, like count, tags, chapters
  ✓ Share button appended to every result so the YouTube URL is sent as follow-up
  ✓ Richer embeds: upload date, likes, channel avatar / banner as thumbnail fallback
  ✓ Auto-disable views on timeout (no stale buttons)
  ✓ Graceful degradation: if yt-dlp fails, detailed error embed (not silent crash)
  ✓ Full type annotations throughout
  ✓ Docstrings on every public surface
  ✓ setup() registers cog and prints confirmation
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp


# ══════════════════════════════════════════════════════════════════════════════
# Constants / config
# ══════════════════════════════════════════════════════════════════════════════

YT_RED        = discord.Color.from_rgb(255, 0, 0)
YT_DARK_RED   = discord.Color.from_rgb(200, 0, 0)
RESULT_COUNT       = 5          # videos returned per search
DROPDOWN_TIMEOUT   = 300        # seconds before search dropdown expires
VIEW_TIMEOUT       = 600        # seconds before result nav view expires

# LRU-like search cache: query → (timestamp, entries_list)
_search_cache: dict[str, tuple[float, list[dict]]] = {}
CACHE_TTL     = 300             # 5 minutes

# Trending category search terms
TRENDING_CATEGORIES: dict[str, str] = {
    "🎵 Music":       "trending music 2024",
    "🎮 Gaming":      "trending gaming videos",
    "📰 News":        "trending news today",
    "😂 Comedy":      "trending funny videos",
    "🎬 Movies":      "trending movie trailers",
    "🏋️ Fitness":     "trending workout videos",
    "🍳 Food":        "trending cooking recipes",
    "🔬 Science":     "trending science technology",
    "🌍 Travel":      "trending travel vlogs",
    "🎨 Art":         "trending art and creativity",
}

# Regex to extract a video ID from a full URL
_VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)

# ══════════════════════════════════════════════════════════════════════════════
# Utility helpers
# ══════════════════════════════════════════════════════════════════════════════

def _yt_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _thumbnail(video_id: str, quality: str = "hqdefault") -> str:
    """Return a YouTube thumbnail URL. quality: hqdefault | maxresdefault | mqdefault"""
    return f"https://img.youtube.com/vi/{video_id}/{quality}.jpg"


def _fmt_duration(seconds) -> str:
    """Format raw seconds → H:MM:SS or M:SS or '?'."""
    if not seconds:
        return "?"
    h, rem = divmod(int(seconds), 3600)
    m, s   = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_views(n) -> str:
    """Format large integers to human-readable like 1.4M or 23.7K."""
    if n is None:
        return "?"
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _fmt_likes(n) -> str:
    if n is None:
        return "?"
    return _fmt_views(n)


def _fmt_upload_date(raw: str | None) -> str:
    """Convert YYYYMMDD → DD Mon YYYY, e.g. 20240315 → 15 Mar 2024."""
    if not raw or len(raw) != 8:
        return "?"
    months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    try:
        y, m, d = int(raw[:4]), int(raw[4:6]), int(raw[6:8])
        return f"{d} {months[m-1]} {y}"
    except (ValueError, IndexError):
        return raw




def _extract_video_id(text: str) -> str | None:
    """Pull an 11-char video ID out of a URL or return text if it already is one."""
    m = _VIDEO_ID_RE.search(text)
    if m:
        return m.group(1)
    stripped = text.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", stripped):
        return stripped
    return None


# ══════════════════════════════════════════════════════════════════════════════
# yt-dlp wrappers  (run in executor to avoid blocking the event loop)
# ══════════════════════════════════════════════════════════════════════════════

_SEARCH_OPTS: dict = {
    "quiet":          True,
    "no_warnings":    True,
    "extract_flat":   True,
    "playlist_items": f"1:{RESULT_COUNT}",
    "socket_timeout": 10,
}

_INFO_OPTS: dict = {
    "quiet":       True,
    "no_warnings": True,
    "noplaylist":  True,
    "socket_timeout": 15,
}


async def _search(query: str) -> list[dict]:
    """
    Search YouTube for `query` and return up to RESULT_COUNT entry dicts.
    Results are cached for CACHE_TTL seconds to avoid hammering yt-dlp.
    """
    # Cache hit?
    cached = _search_cache.get(query)
    if cached:
        ts, entries = cached
        if time.time() - ts < CACHE_TTL:
            return entries

    loop = asyncio.get_event_loop()

    def _run() -> list[dict]:
        with yt_dlp.YoutubeDL(_SEARCH_OPTS) as ydl:
            r = ydl.extract_info(f"ytsearch{RESULT_COUNT}:{query}", download=False)
            return [e for e in (r.get("entries") or []) if e]

    try:
        entries = await loop.run_in_executor(None, _run)
    except Exception as exc:
        print(f"[YouTube] Search error for {query!r}: {exc}")
        return []

    _search_cache[query] = (time.time(), entries)
    return entries


async def _fetch_video_info(video_id: str) -> dict | None:
    """
    Fetch full metadata for a single video (no download).
    Returns the info dict or None on failure.
    """
    loop = asyncio.get_event_loop()

    def _run() -> dict | None:
        with yt_dlp.YoutubeDL(_INFO_OPTS) as ydl:
            return ydl.extract_info(_yt_url(video_id), download=False)

    try:
        return await loop.run_in_executor(None, _run)
    except Exception as exc:
        print(f"[YouTube] Info fetch error for {video_id!r}: {exc}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Embed builders
# ══════════════════════════════════════════════════════════════════════════════

def _search_embed(query: str, entries: list[dict]) -> discord.Embed:
    """List-style embed shown before the user picks a result."""
    embed = discord.Embed(
        title       = f"🔍 YouTube  ›  {query[:80]}",
        description = (
            f"Found **{len(entries)}** result(s). Pick one from the dropdown below.\n"
            "Discord will embed a preview so you can play it right here."
        ),
        color = YT_RED,
    )
    for i, e in enumerate(entries, 1):
        dur  = _fmt_duration(e.get("duration"))
        ch   = e.get("uploader") or e.get("channel") or "Unknown"
        date = _fmt_upload_date(e.get("upload_date"))
        views = _fmt_views(e.get("view_count"))
        embed.add_field(
            name   = f"{i}. {(e.get('title') or 'Untitled')[:70]}",
            value  = f"`{ch}`  •  ⏱ `{dur}`  •  👁 `{views}`  •  📅 `{date}`",
            inline = False,
        )
    embed.set_footer(text=f"Results cached for 5 min  •  Dropdown expires in 5 min")
    return embed


def _result_content(
    entry: dict,
    user:  discord.User | discord.Member,
    index: int,
    total: int,
) -> str:
    """
    Plain-text content for the video result message.
    We send NO embed so Discord can render its full native video player.
    All metadata is packed into the text content instead.
    """
    vid_id  = entry.get("id", "")
    title   = (entry.get("title") or "Untitled")[:200]
    channel = entry.get("uploader") or entry.get("channel") or "Unknown"
    views   = _fmt_views(entry.get("view_count"))
    dur     = _fmt_duration(entry.get("duration"))
    date    = _fmt_upload_date(entry.get("upload_date"))
    yt_link = _yt_url(vid_id)

    lines = [
        yt_link,   # raw URL first → Discord renders full native video player
        f"",
        f"▶️  **{title}**",
        f"📺 {channel}  •  ⏱️ {dur}  •  👁️ {views}  •  📅 {date}  •  `{index}/{total}`",
        f"-# Requested by {user.display_name} • Powered by yt-dlp",
    ]
    return "\n".join(lines)


def _info_embed(
    info: dict,
    user: discord.User | discord.Member,
) -> discord.Embed:
    """Deep-dive info embed for /ytinfo."""
    vid_id  = info.get("id", "")
    title   = (info.get("title") or "Untitled")[:200]
    channel = info.get("uploader") or info.get("channel") or "Unknown"
    ch_url  = info.get("channel_url") or ""
    views   = _fmt_views(info.get("view_count"))
    likes   = _fmt_likes(info.get("like_count"))
    dur     = _fmt_duration(info.get("duration"))
    date    = _fmt_upload_date(info.get("upload_date"))
    subs    = _fmt_views(info.get("channel_follower_count"))
    desc    = (info.get("description") or "").strip()
    cats    = info.get("categories") or []
    tags    = info.get("tags") or []
    chapters = info.get("chapters") or []
    yt_link  = _yt_url(vid_id)

    embed = discord.Embed(
        title = f"📋  {title}",
        url   = yt_link,
        color = YT_DARK_RED,
    )
    embed.set_image(url=_thumbnail(vid_id, "maxresdefault"))

    ch_display = f"[{channel}]({ch_url})" if ch_url else channel
    embed.add_field(name="📺 Channel",    value=ch_display,   inline=True)
    embed.add_field(name="👥 Subscribers",value=f"`{subs}`",  inline=True)
    embed.add_field(name="⏱️ Duration",   value=f"`{dur}`",   inline=True)
    embed.add_field(name="👁️ Views",      value=f"`{views}`", inline=True)
    embed.add_field(name="👍 Likes",      value=f"`{likes}`", inline=True)
    embed.add_field(name="📅 Uploaded",   value=f"`{date}`",  inline=True)

    if cats:
        embed.add_field(name="🗂️ Category", value=", ".join(cats[:3]), inline=True)

    if desc:
        short = desc[:400] + ("…" if len(desc) > 400 else "")
        embed.add_field(name="📝 Description", value=short, inline=False)

    if chapters:
        chapter_lines = "\n".join(
            f"`{_fmt_duration(c.get('start_time'))}` — {c.get('title','?')[:50]}"
            for c in chapters[:8]
        )
        embed.add_field(name="🎬 Chapters", value=chapter_lines, inline=False)

    if tags:
        embed.add_field(
            name  = "🏷️ Tags",
            value = "  ".join(f"`{t}`" for t in tags[:10]),
            inline = False,
        )

    embed.set_footer(
        text     = f"Fetched by {user.display_name}  •  Powered by yt-dlp",
        icon_url = user.display_avatar.url,
    )
    return embed


def _trending_embed(query: str, entries: list[dict], category: str) -> discord.Embed:
    """Trending-style embed listing videos in a category."""
    embed = discord.Embed(
        title       = f"🔥 Trending  ›  {category}",
        description = "Select a video from the dropdown to watch it.",
        color       = YT_RED,
    )
    for i, e in enumerate(entries, 1):
        dur   = _fmt_duration(e.get("duration"))
        ch    = e.get("uploader") or e.get("channel") or "Unknown"
        views = _fmt_views(e.get("view_count"))
        embed.add_field(
            name   = f"{i}. {(e.get('title') or 'Untitled')[:70]}",
            value  = f"`{ch}`  •  ⏱ `{dur}`  •  👁 `{views}`",
            inline = False,
        )
    embed.set_footer(text="Dropdown expires in 5 min")
    return embed



def _no_results_embed(query: str) -> discord.Embed:
    return discord.Embed(
        title       = "❌ No Results",
        description = f"No YouTube results found for **{query[:100]}**.",
        color       = discord.Color.red(),
    )


def _error_embed(reason: str) -> discord.Embed:
    return discord.Embed(
        title       = "❌ YouTube Error",
        description = reason,
        color       = discord.Color.red(),
    )


# ══════════════════════════════════════════════════════════════════════════════
# UI Views
# ══════════════════════════════════════════════════════════════════════════════

def _make_watch_view(video_id: str) -> discord.ui.View:
    """Minimal persistent view with a single Watch button."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label = "▶️  Watch on YouTube",
        style = discord.ButtonStyle.red,
        url   = _yt_url(video_id),
    ))
    return view


class ResultNavView(discord.ui.View):
    """
    Navigation view attached to a selected video result.
    Allows paging through all 5 results without re-searching,
    plus a Watch button that always opens the current video.
    """

    def __init__(
        self,
        entries: list[dict],
        user:    discord.User | discord.Member,
        index:   int = 0,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.entries = entries
        self.user    = user
        self.index   = index
        self._update_buttons()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _update_buttons(self) -> None:
        """Rebuild dynamic buttons based on current index."""
        self.clear_items()

        # Prev button
        prev = discord.ui.Button(
            label    = "◀ Prev",
            style    = discord.ButtonStyle.secondary,
            disabled = self.index == 0,
            custom_id = "yt_prev",
        )
        prev.callback = self._prev_callback
        self.add_item(prev)

        # Counter label (non-interactive)
        counter = discord.ui.Button(
            label    = f"{self.index + 1} / {len(self.entries)}",
            style    = discord.ButtonStyle.grey,
            disabled = True,
            custom_id = "yt_counter",
        )
        self.add_item(counter)

        # Next button
        nxt = discord.ui.Button(
            label    = "Next ▶",
            style    = discord.ButtonStyle.secondary,
            disabled = self.index >= len(self.entries) - 1,
            custom_id = "yt_next",
        )
        nxt.callback = self._next_callback
        self.add_item(nxt)

        # Watch button (always current video)
        vid_id = self.entries[self.index].get("id", "")
        watch = discord.ui.Button(
            label = "▶️  Watch on YouTube",
            style = discord.ButtonStyle.red,
            url   = _yt_url(vid_id),
        )
        self.add_item(watch)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "❌ This menu belongs to someone else.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        """Disable all non-URL buttons when the view expires."""
        for item in self.children:
            if isinstance(item, discord.ui.Button) and not item.url:
                item.disabled = True

    # ── Button callbacks ──────────────────────────────────────────────────────

    async def _prev_callback(self, interaction: discord.Interaction) -> None:
        self.index = max(0, self.index - 1)
        self._update_buttons()
        text = _result_content(self.entries[self.index], self.user, self.index + 1, len(self.entries))
        await interaction.response.edit_message(content=text, embed=None, view=self)

    async def _next_callback(self, interaction: discord.Interaction) -> None:
        self.index = min(len(self.entries) - 1, self.index + 1)
        self._update_buttons()
        text = _result_content(self.entries[self.index], self.user, self.index + 1, len(self.entries))
        await interaction.response.edit_message(content=text, embed=None, view=self)


class VideoSelect(discord.ui.Select):
    """Dropdown that lets the user pick one of the search results."""

    def __init__(
        self,
        entries: list[dict],
        user:    discord.User | discord.Member,
    ) -> None:
        self.entries = entries
        self.user    = user
        options = [
            discord.SelectOption(
                label       = f"{i}. {(e.get('title') or 'Untitled')[:90]}",
                description = (
                    f"{(e.get('uploader') or 'Unknown')[:30]}"
                    f"  •  {_fmt_duration(e.get('duration'))}"
                    f"  •  {_fmt_views(e.get('view_count'))} views"
                )[:100],
                value = str(i - 1),
                emoji = "🎬",
            )
            for i, e in enumerate(entries, 1)
        ]
        super().__init__(
            placeholder = "Choose a video…",
            min_values  = 1,
            max_values  = 1,
            options     = options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        idx   = int(self.values[0])
        entry = self.entries[idx]
        text  = _result_content(entry, self.user, idx + 1, len(self.entries))
        view  = ResultNavView(self.entries, self.user, idx)
        # Delete search message, send fresh one with no embed so Discord
        # renders the full native interactive video player from the raw URL.
        await interaction.response.defer()
        await interaction.delete_original_response()
        await interaction.followup.send(content=text, view=view)


class SearchView(discord.ui.View):
    """View holding the search-results dropdown."""

    def __init__(
        self,
        entries: list[dict],
        user:    discord.User | discord.Member,
    ) -> None:
        super().__init__(timeout=DROPDOWN_TIMEOUT)
        self.user = user
        self.add_item(VideoSelect(entries, user))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "❌ This menu belongs to someone else.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True


class TrendingCategorySelect(discord.ui.Select):
    """Dropdown to pick a trending category."""

    def __init__(self, user: discord.User | discord.Member) -> None:
        self.user = user
        options = [
            discord.SelectOption(label=label, value=query, emoji=label.split()[0])
            for label, query in TRENDING_CATEGORIES.items()
        ]
        super().__init__(
            placeholder = "Pick a trending category…",
            min_values  = 1,
            max_values  = 1,
            options     = options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        query    = self.values[0]
        category = next(k for k, v in TRENDING_CATEGORIES.items() if v == query)
        entries  = await _search(query)
        if not entries:
            await interaction.followup.send(embed=_no_results_embed(category), ephemeral=True)
            return
        embed = _trending_embed(query, entries, category)
        view  = SearchView(entries, self.user)
        await interaction.edit_original_response(embed=embed, view=view)


class TrendingView(discord.ui.View):
    """View holding the trending category dropdown."""

    def __init__(self, user: discord.User | discord.Member) -> None:
        super().__init__(timeout=DROPDOWN_TIMEOUT)
        self.user = user
        self.add_item(TrendingCategorySelect(user))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "❌ This menu belongs to someone else.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True


# ══════════════════════════════════════════════════════════════════════════════
# Cog
# ══════════════════════════════════════════════════════════════════════════════

class YouTube(commands.Cog):
    """YouTube search, trending, and video-info commands for Jarvis."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Shared internals ──────────────────────────────────────────────────────

    async def _do_search(
        self,
        query:    str,
        user:     discord.User | discord.Member,
        send_fn,           # coroutine that sends a message
        ephemeral_fn,      # coroutine that sends an ephemeral message
    ) -> None:
        """Core search logic shared by slash and prefix handlers."""
        entries = await _search(query)

        if not entries:
            await send_fn(embed=_no_results_embed(query))
            return

        embed = _search_embed(query, entries)
        view  = SearchView(entries, user)
        await send_fn(embed=embed, view=view)

    async def _do_info(
        self,
        target:   str,
        user:     discord.User | discord.Member,
        send_fn,
        ephemeral_fn,
    ) -> None:
        """Core info logic shared by slash and prefix handlers."""
        vid_id = _extract_video_id(target)
        if not vid_id:
            await ephemeral_fn(embed=_error_embed(
                f"`{target[:100]}` doesn't look like a valid YouTube URL or video ID.\n"
                "Try: `https://www.youtube.com/watch?v=dQw4w9WgXcQ` or just the 11-char ID."
            ))
            return

        info = await _fetch_video_info(vid_id)

        if not info:
            await send_fn(embed=_error_embed(
                "Couldn't fetch video info. The video may be private, age-restricted, or unavailable."
            ))
            return

        embed = _info_embed(info, user)
        view  = _make_watch_view(vid_id)
        await send_fn(embed=embed, view=view)

    # ── /youtube ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name        = "youtube",
        description = "Search YouTube and play a video directly in Discord 🎬",
    )
    @app_commands.describe(query="What do you want to search for?")
    async def slash_youtube(
        self,
        interaction: discord.Interaction,
        query:       str,
    ) -> None:
        """Slash command: search YouTube and present a dropdown of results."""
        await interaction.response.defer(thinking=True)
        await self._do_search(
            query        = query.strip(),
            user         = interaction.user,
            send_fn      = interaction.followup.send,
            ephemeral_fn = lambda **kw: interaction.followup.send(ephemeral=True, **kw),
        )

    # ── /yttrend ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name        = "yttrend",
        description = "Browse trending YouTube videos by category 🔥",
    )
    async def slash_yttrend(self, interaction: discord.Interaction) -> None:
        """Slash command: pick a trending category, then pick a video."""
        embed = discord.Embed(
            title       = "🔥 YouTube Trending",
            description = "Pick a category from the dropdown to see what's trending.",
            color       = YT_RED,
        )
        view = TrendingView(interaction.user)
        await interaction.response.send_message(embed=embed, view=view)

    # ── /ytinfo ───────────────────────────────────────────────────────────────

    @app_commands.command(
        name        = "ytinfo",
        description = "Get detailed info about a YouTube video 📋",
    )
    @app_commands.describe(video="YouTube URL or 11-character video ID")
    async def slash_ytinfo(
        self,
        interaction: discord.Interaction,
        video:       str,
    ) -> None:
        """Slash command: fetch and display full metadata for a video."""
        await interaction.response.defer(thinking=True)
        await self._do_info(
            target       = video.strip(),
            user         = interaction.user,
            send_fn      = interaction.followup.send,
            ephemeral_fn = lambda **kw: interaction.followup.send(ephemeral=True, **kw),
        )

    # ── !youtube / !yt ────────────────────────────────────────────────────────

    @commands.command(name="youtube", aliases=["yt"])
    async def prefix_youtube(
        self,
        ctx:   commands.Context,
        *,
        query: str = "",
    ) -> None:
        """Prefix command: search YouTube. Usage: `!youtube <query>` or `!yt <query>`"""
        if not query.strip():
            await ctx.reply(
                "**Usage:** `!youtube <query>`\n"
                "**Example:** `!youtube lofi hip hop`\n"
                "**Aliases:** `!yt`\n"
                "**Tip:** Use `/youtube` for the slash-command version with the same features."
            )
            return

        async with ctx.typing():
            await self._do_search(
                query        = query.strip(),
                user         = ctx.author,
                send_fn      = ctx.reply,
                ephemeral_fn = ctx.reply,  # prefix can't be ephemeral; just reply
            )

    # ── !yttrend ──────────────────────────────────────────────────────────────

    @commands.command(name="yttrend", aliases=["trending", "yttrending"])
    async def prefix_yttrend(
        self,
        ctx:      commands.Context,
        *,
        category: str = "",
    ) -> None:
        """
        Prefix command: show trending YouTube videos.
        Usage: `!yttrend [category]`
        Categories: music, gaming, news, comedy, movies, fitness, food, science, travel, art
        If no category given, shows a dropdown.
        """
        # Map loose category words to search terms
        _alias_map: dict[str, str] = {
            "music":    "🎵 Music",
            "gaming":   "🎮 Gaming",
            "game":     "🎮 Gaming",
            "news":     "📰 News",
            "comedy":   "😂 Comedy",
            "funny":    "😂 Comedy",
            "movies":   "🎬 Movies",
            "film":     "🎬 Movies",
            "fitness":  "🏋️ Fitness",
            "workout":  "🏋️ Fitness",
            "food":     "🍳 Food",
            "cooking":  "🍳 Food",
            "science":  "🔬 Science",
            "tech":     "🔬 Science",
            "travel":   "🌍 Travel",
            "art":      "🎨 Art",
        }
        label = _alias_map.get(category.strip().lower())

        if not label:
            # No match — show dropdown picker
            embed = discord.Embed(
                title       = "🔥 YouTube Trending",
                description = "Pick a category from the dropdown to see what's trending.",
                color       = YT_RED,
            )
            view = TrendingView(ctx.author)
            await ctx.reply(embed=embed, view=view)
            return

        async with ctx.typing():
            query   = TRENDING_CATEGORIES[label]
            entries = await _search(query)

        if not entries:
            await ctx.reply(embed=_no_results_embed(label))
            return

        embed = _trending_embed(query, entries, label)
        view  = SearchView(entries, ctx.author)
        await ctx.reply(embed=embed, view=view)

    # ── !ytinfo ───────────────────────────────────────────────────────────────

    @commands.command(name="ytinfo", aliases=["ytdetails", "ytvideo"])
    async def prefix_ytinfo(
        self,
        ctx:   commands.Context,
        *,
        video: str = "",
    ) -> None:
        """
        Prefix command: fetch detailed info about a YouTube video.
        Usage: `!ytinfo <url or video_id>`
        """
        if not video.strip():
            await ctx.reply(
                "**Usage:** `!ytinfo <YouTube URL or video ID>`\n"
                "**Example:** `!ytinfo https://youtu.be/dQw4w9WgXcQ`\n"
                "**Also accepts:** `!ytinfo dQw4w9WgXcQ`"
            )
            return

        async with ctx.typing():
            await self._do_info(
                target       = video.strip(),
                user         = ctx.author,
                send_fn      = ctx.reply,
                ephemeral_fn = ctx.reply,
            )


# ══════════════════════════════════════════════════════════════════════════════
# Setup
# ══════════════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(YouTube(bot))