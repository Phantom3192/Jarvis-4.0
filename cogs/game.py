import discord
from discord.ext import commands
from discord import app_commands
import random
import asyncio
import os
from cogs.ai import generate_ai_response
from cogs.state import check_cooldown

# ═══════════════════════════════════════════════════════════════════════════════
#  HANGMAN
# ═══════════════════════════════════════════════════════════════════════════════

HANGMAN_STAGES = [
    "```\n  ╔═══╗\n  ║   ║\n      ║\n      ║\n      ║\n      ║\n  ════╝```",
    "```\n  ╔═══╗\n  ║   ║\n  😶  ║\n      ║\n      ║\n      ║\n  ════╝```",
    "```\n  ╔═══╗\n  ║   ║\n  😶  ║\n  │   ║\n      ║\n      ║\n  ════╝```",
    "```\n  ╔═══╗\n  ║   ║\n  😟  ║\n ╱│   ║\n      ║\n      ║\n  ════╝```",
    "```\n  ╔═══╗\n  ║   ║\n  😟  ║\n ╱│╲  ║\n      ║\n      ║\n  ════╝```",
    "```\n  ╔═══╗\n  ║   ║\n  😨  ║\n ╱│╲  ║\n ╱    ║\n      ║\n  ════╝```",
    "```\n  ╔═══╗\n  ║   ║\n  😵  ║\n ╱│╲  ║\n ╱ ╲  ║\n      ║\n  ════╝```",
]

HANGMAN_COLORS = [
    discord.Color.green(), discord.Color.green(),
    discord.Color.from_rgb(144, 238, 144), discord.Color.yellow(),
    discord.Color.orange(), discord.Color.from_rgb(255, 80, 0), discord.Color.red(),
]

HANGMAN_WORDS = [
    "python", "discord", "robot", "galaxy", "jarvis", "keyboard", "asteroid",
    "phantom", "thunder", "wizard", "castle", "penguin", "lantern", "tornado",
    "dolphin", "volcano", "muffin", "crystal", "dragon", "shadow", "mirror",
    "pirate", "jungle", "rocket", "cobalt", "marble", "falcon", "puzzle",
]

active_hangman: dict[int, dict] = {}


def _hangman_embed(state: dict, finished: bool = False) -> discord.Embed:
    word       = state["word"]
    guessed    = state["guessed"]
    wrong      = state["wrong"]
    lives_left = 6 - wrong
    display    = "  ".join(f"**{c.upper()}**" if c in guessed else "﹏" for c in word)
    hearts     = "❤️" * lives_left + "🖤" * wrong
    wrong_letters = guessed - set(word)
    wrong_display = "  ".join(f"~~{l.upper()}~~" for l in sorted(wrong_letters)) or "None yet"
    total    = len(word)
    revealed = sum(1 for c in word if c in guessed)
    filled   = round((revealed / total) * 10)
    bar      = "█" * filled + "░" * (10 - filled)
    progress = f"`[{bar}]` {revealed}/{total}"
    color    = HANGMAN_COLORS[min(wrong, 6)] if not finished else discord.Color.greyple()
    embed    = discord.Embed(title="🪢  H A N G M A N", color=color)
    embed.description = HANGMAN_STAGES[min(wrong, 6)]
    embed.add_field(name="Word",             value=display,       inline=False)
    embed.add_field(name=" Lives",           value=hearts,        inline=True)
    embed.add_field(name="📏 Length",        value=f"`{len(word)}` letters", inline=True)
    embed.add_field(name="❌ Wrong Guesses", value=wrong_display, inline=False)
    embed.add_field(name="📊 Progress",      value=progress,      inline=False)
    if not finished:
        embed.set_footer(text="✏️  Type a single letter to guess!")
    return embed


# ═══════════════════════════════════════════════════════════════════════════════
#  COUNTING — Turso-backed, with in-memory mirror + debounced save
# ═══════════════════════════════════════════════════════════════════════════════

# In-memory store: str(guild_id) → {channel_id, count, high_score, last_user_id}
_count_data: dict[str, dict] = {}
_count_save_tasks: dict[str, asyncio.Task] = {}

# Turso connection (shared with state.py approach — separate table)
_count_conn = None


async def _count_init_db():
    """Connect to Turso and load counting data. Called from cog __init__."""
    global _count_conn
    turso_url   = os.getenv("TURSO_URL",   "").strip().lstrip("=").strip()
    turso_token = os.getenv("TURSO_TOKEN", "").strip().lstrip("=").strip()
    if not turso_url or not turso_token:
        print("⚠️  Counting: running in memory-only mode (no TURSO_URL/TOKEN)")
        return
    try:
        import libsql_experimental as libsql
        _count_conn = libsql.connect(database=turso_url, auth_token=turso_token)
        _count_conn.execute("""
            CREATE TABLE IF NOT EXISTS counting (
                guild_id     TEXT PRIMARY KEY,
                channel_id   TEXT,
                count        INTEGER NOT NULL DEFAULT 0,
                high_score   INTEGER NOT NULL DEFAULT 0,
                last_user_id TEXT
            )
        """)
        _count_conn.commit()

        # One-time migration: convert INTEGER channel_id/last_user_id columns to TEXT

        rows = _count_conn.execute(
            "SELECT guild_id, channel_id, count, high_score, last_user_id FROM counting"
        ).fetchall()
        for guild_id, channel_id, count, high_score, last_user_id in rows:
            _count_data[guild_id] = {
                "channel_id":   int(channel_id) if channel_id is not None else None,
                "count":        count,
                "high_score":   high_score,
                "last_user_id": int(last_user_id) if last_user_id is not None else None,
            }
        print("✅ Counting DB ready (Turso)")
    except Exception as e:
        print(f"❌ Counting DB connection failed: {e} — running in memory-only mode")
        _count_conn = None


def _count_guild(guild_id: int) -> dict:
    key = str(guild_id)
    if key not in _count_data:
        _count_data[key] = {"channel_id": None, "count": 0, "high_score": 0, "last_user_id": None}
    return _count_data[key]


def _count_persist(guild_id: int):
    """Upsert guild counting row to Turso. No-op if not connected."""
    global _count_conn
    if _count_conn is None:
        return
    g = _count_data.get(str(guild_id))
    if not g:
        return

    def _do_save():
        _count_conn.execute(
            """INSERT INTO counting (guild_id, channel_id, count, high_score, last_user_id)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   channel_id   = excluded.channel_id,
                   count        = excluded.count,
                   high_score   = excluded.high_score,
                   last_user_id = excluded.last_user_id""",
            (
                str(guild_id),
                str(g["channel_id"]) if g["channel_id"] is not None else None,
                g["count"],
                g["high_score"],
                str(g["last_user_id"]) if g["last_user_id"] is not None else None,
            )
        )
        _count_conn.commit()

    try:
        _do_save()
    except Exception as e:
        msg = str(e).lower()
        if "stream not found" in msg or ("404" in msg and "hrana" in msg):
            print("[Counting] Stream error, attempting reconnect...")
            try:
                import libsql_experimental as libsql
                turso_url   = os.getenv("TURSO_URL",   "").strip().lstrip("=").strip()
                turso_token = os.getenv("TURSO_TOKEN", "").strip().lstrip("=").strip()
                _count_conn = libsql.connect(database=turso_url, auth_token=turso_token)
                _count_conn.execute("""
                    CREATE TABLE IF NOT EXISTS counting (
                        guild_id     TEXT PRIMARY KEY,
                        channel_id   TEXT,
                        count        INTEGER NOT NULL DEFAULT 0,
                        high_score   INTEGER NOT NULL DEFAULT 0,
                        last_user_id TEXT
                    )
                """)
                _count_conn.commit()
                _do_save()
                print("[Counting] Reconnected and saved successfully.")
            except Exception as e2:
                print(f"❌ Counting DB reconnect failed: {e2}")
                _count_conn = None
        else:
            print(f"❌ Counting DB save error: {e}")


def _count_schedule_save(guild_id: int):
    """Debounce DB writes — waits 2 s after last change."""
    key = str(guild_id)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = _count_save_tasks.get(key)
    if task and not task.done():
        task.cancel()
    _count_save_tasks[key] = loop.create_task(_count_debounced_save(guild_id))


async def _count_debounced_save(guild_id: int, delay: float = 2.0):
    await asyncio.sleep(delay)
    _count_persist(guild_id)


def _count_delete_db(guild_id: int):
    """Delete a guild's counting row entirely from Turso."""
    if _count_conn is None:
        return
    try:
        _count_conn.execute("DELETE FROM counting WHERE guild_id = ?", (str(guild_id),))
        _count_conn.commit()
    except Exception as e:
        print(f"❌ Counting DB delete error: {e}")


async def _count_offer_save(message: discord.Message, old: int, reason_text: str) -> bool:
    """
    Show a Yes/No prompt offering to spend JC to keep the count at `old`
    instead of resetting to 0. Always shown, even if the user can't afford
    it — "Yes" will then explain they're short on JC and reset anyway.

    Returns True (the caller should return without resetting — the view's
    callbacks handle every outcome).
    """
    from cogs.economy import SpendCreditsView, COUNT_SAVE_COST, JC_EMOJI, JC_NAME
    from cogs.state import get_credits

    guild_id = message.guild.id
    balance = get_credits(message.author.id)

    def _reset():
        g = _count_guild(guild_id)
        g["count"] = 0
        g["last_user_id"] = None
        _count_schedule_save(guild_id)

    def _reset_embed(extra: str = "") -> discord.Embed:
        return discord.Embed(
            description=(
                (f"{extra}\n" if extra else "")
                + f"{reason_text}\nCount resets to **0**. (was **{old}**)"
            ),
            color=discord.Color.red(),
        )

    async def on_confirm(interaction: discord.Interaction, view: SpendCreditsView):
        # ✅ YES — Streak saved: delete wrong msg + this msg, show saved embed
        embed = discord.Embed(
            title="🛡️ Streak Saved!",
            description=(
                f"{JC_EMOJI} **{COUNT_SAVE_COST} {JC_NAME}** spent.\n"
                f"The count stays at **{old}** — next number is **{old + 1}**."
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=view)
        await asyncio.sleep(4)
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass

    async def on_decline(interaction: discord.Interaction, view: SpendCreditsView, reason: str):
        # ❌ NO — Streak lost: keep wrong msg, delete this msg, drop streak lost msg
        _reset()
        if reason == "insufficient":
            extra = f"❌ Not enough {JC_EMOJI} {JC_NAME}s (you have **{balance}**, need **{COUNT_SAVE_COST}**).\n"
        else:
            extra = ""
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass
        await message.channel.send(
            embed=discord.Embed(
                description=f"{extra}{reason_text}\n💀 {message.author.mention} ruined it! Streak of **{old}** lost! Count resets to **0**.",
                color=discord.Color.red(),
            )
        )

    async def on_timeout_action(view: SpendCreditsView):
        # ⏱️ TIMEOUT — No response: keep wrong msg, delete this msg, drop streak lost msg
        _reset()
        if view.message:
            try:
                await view.message.delete()
            except discord.HTTPException:
                pass
        await message.channel.send(
            embed=discord.Embed(
                description=f"{reason_text}\n💀 {message.author.mention} ruined it! Streak of **{old}** lost! Count resets to **0**.",
                color=discord.Color.red(),
            )
        )

    view = SpendCreditsView(message.author.id, COUNT_SAVE_COST, on_confirm, on_decline, on_timeout_action, timeout=20)
    embed = discord.Embed(
        description=(
            f"{reason_text}\n"
            f"Count would reset to **0** (was **{old}**).\n\n"
            f"Use **{COUNT_SAVE_COST}** {JC_EMOJI} {JC_NAME} to save the streak at **{old}**? "
            f"(you have **{balance}** {JC_EMOJI})"
        ),
        color=discord.Color.orange(),
    )
    msg = await message.reply(embed=embed, view=view)
    view.message = msg
    return True


# ═══════════════════════════════════════════════════════════════════════════════
#  CHESS
# ═══════════════════════════════════════════════════════════════════════════════

import chess as _chess
import io as _io
from PIL import Image as _Image, ImageDraw as _ImageDraw, ImageFont as _ImageFont

_SQ     = 100
_BORDER = 52
_BSIZE  = _SQ * 8 + _BORDER * 2

_COL_LIGHT      = (240, 217, 181)
_COL_DARK       = (181, 136,  99)
_COL_BG         = ( 22,  21,  28)
_COL_BORDER     = ( 49,  46,  43)
_COL_LABEL      = (200, 170, 120)

# Unicode chess piece glyphs: index 0 = white, 1 = black
_PIECE_GLYPH = {
    _chess.KING:   ("♔", "♚"),
    _chess.QUEEN:  ("♕", "♛"),
    _chess.ROOK:   ("♖", "♜"),
    _chess.BISHOP: ("♗", "♝"),
    _chess.KNIGHT: ("♘", "♞"),
    _chess.PAWN:   ("♙", "♟"),
}

_CHESS_FONT_PATH = "/usr/share/fonts/truetype/freefont/FreeSerif.ttf"
_LABEL_FONT_PATH = "/usr/share/fonts/truetype/freefont/FreeSerif.ttf"
_BUNDLED_FONT_PATH = os.path.join(os.path.dirname(__file__), "FreeSerif.ttf")

_CHESS_FONT_FALLBACKS = [
    "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/TTF/FreeSerif.ttf",
    "/usr/share/fonts/gnu-free/FreeSerif.ttf",
]

_cached_piece_font: dict = {}


# Download FreeSerif if not available (e.g. Railway Linux containers)
def _ensure_chess_font():
    if os.path.exists(_CHESS_FONT_PATH) or os.path.exists(_BUNDLED_FONT_PATH):
        return
    try:
        import urllib.request
        url = "https://github.com/opensymbol/free-fonts/raw/master/FreeSerif.ttf"
        urllib.request.urlretrieve(url, _BUNDLED_FONT_PATH)
    except Exception:
        pass

_ensure_chess_font()

def _get_piece_font(size: int):
    if size in _cached_piece_font:
        return _cached_piece_font[size]
    for path in _CHESS_FONT_FALLBACKS:
        if os.path.exists(path):
            font = _ImageFont.truetype(path, size)
            _cached_piece_font[size] = font
            return font
    # Last resort: download FreeSerif at runtime
    try:
        import urllib.request, tempfile
        url = "https://github.com/opensourcedesign/fonts/raw/master/gnu-freefont_freefont-20120503/FreeSerif.ttf"
        tmp = os.path.join(tempfile.gettempdir(), "FreeSerif.ttf")
        if not os.path.exists(tmp):
            urllib.request.urlretrieve(url, tmp)
        font = _ImageFont.truetype(tmp, size)
        _cached_piece_font[size] = font
        return font
    except Exception:
        pass
    return _ImageFont.load_default(size)


def _get_label_font(size: int):
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        _LABEL_FONT_PATH,
    ]:
        if os.path.exists(p):
            return _ImageFont.truetype(p, size)
    return _ImageFont.load_default(size)


def _render_board_image(board: "_chess.Board", flipped: bool = False, last_move=None) -> bytes:
    img = _Image.new("RGB", (_BSIZE, _BSIZE), _COL_BG)
    draw = _ImageDraw.Draw(img, "RGBA")

    # Layered border for depth
    for i in range(8):
        shade = tuple(max(0, c - i * 3) for c in _COL_BORDER)
        draw.rectangle([i, i, _BSIZE - i, _BSIZE - i], outline=shade, width=1)
    draw.rectangle([_BORDER - 5, _BORDER - 5, _BSIZE - _BORDER + 5, _BSIZE - _BORDER + 5],
                   outline=(80, 65, 50), width=2)
    draw.rectangle([_BORDER - 3, _BORDER - 3, _BSIZE - _BORDER + 3, _BSIZE - _BORDER + 3],
                   outline=(55, 44, 33), width=2)

    ranks = range(7, -1, -1) if not flipped else range(8)
    files = range(8)          if not flipped else range(7, -1, -1)

    # Draw squares + overlays
    for ri, rank in enumerate(ranks):
        for fi, file in enumerate(files):
            sq    = _chess.square(file, rank)
            x     = _BORDER + fi * _SQ
            y     = _BORDER + ri * _SQ
            color = _COL_LIGHT if (rank + file) % 2 == 1 else _COL_DARK
            draw.rectangle([x, y, x + _SQ - 1, y + _SQ - 1], fill=color)
            if last_move and sq in (last_move.from_square, last_move.to_square):
                ov = _Image.new("RGBA", (_SQ, _SQ), (100, 180, 70, 110))
                img.paste(ov, (x, y), ov)
            piece = board.piece_at(sq)
            if (piece and piece.piece_type == _chess.KING
                    and piece.color == board.turn and board.is_check()):
                ov = _Image.new("RGBA", (_SQ, _SQ), (220, 40, 40, 170))
                img.paste(ov, (x, y), ov)

    # Draw pieces using Unicode glyphs with outline + shadow
    pfont = _get_piece_font(int(_SQ * 0.70))
    # Make coordinate font MUCH larger (was 18, now 28)
    lfont = _get_label_font(28)
    draw2 = _ImageDraw.Draw(img, "RGBA")

    for ri, rank in enumerate(ranks):
        for fi, file in enumerate(files):
            sq    = _chess.square(file, rank)
            x     = _BORDER + fi * _SQ
            y     = _BORDER + ri * _SQ
            piece = board.piece_at(sq)
            if not piece:
                continue
            glyph = _PIECE_GLYPH[piece.piece_type][0 if piece.color == _chess.WHITE else 1]
            cx, cy = x + _SQ // 2, y + _SQ // 2

            # Drop shadow
            draw2.text((cx + 2, cy + 2), glyph, font=pfont, fill=(0, 0, 0, 100), anchor="mm")

            if piece.color == _chess.WHITE:
                # Outline for white pieces
                for dx, dy in [(-1, -1), (1, -1), (-1, 1), (1, 1)]:
                    draw2.text((cx + dx, cy + dy), glyph, font=pfont,
                               fill=(50, 50, 50, 200), anchor="mm")
                # White fill
                draw2.text((cx, cy), glyph, font=pfont, fill=(255, 255, 255, 255), anchor="mm")
            else:
                # Outline for black pieces
                for dx, dy in [(-1, -1), (1, -1), (-1, 1), (1, 1)]:
                    draw2.text((cx + dx, cy + dy), glyph, font=pfont,
                               fill=(200, 200, 200, 200), anchor="mm")
                # Black fill
                draw2.text((cx, cy), glyph, font=pfont, fill=(20, 20, 20, 255), anchor="mm")

    # Rank & file labels with LARGER font and better positioning
    rank_labels = "87654321" if not flipped else "12345678"
    file_labels = "abcdefgh"  if not flipped else "hgfedcba"
    
    # Make the labels more prominent with a background circle
    for ri, label in enumerate(rank_labels):
        yc = _BORDER + ri * _SQ + _SQ // 2
        # Draw background circle for better visibility
        draw2.ellipse([(_BORDER // 2 - 15), (yc - 15), (_BORDER // 2 + 15), (yc + 15)], 
                      fill=(40, 35, 30, 200))
        draw2.text((_BORDER // 2, yc), label, font=lfont, fill=(220, 200, 150, 255), anchor="mm")
        
        draw2.ellipse([(_BSIZE - _BORDER // 2 - 15), (yc - 15), (_BSIZE - _BORDER // 2 + 15), (yc + 15)], 
                      fill=(40, 35, 30, 200))
        draw2.text((_BSIZE - _BORDER // 2, yc), label, font=lfont, fill=(220, 200, 150, 255), anchor="mm")
    
    for fi, label in enumerate(file_labels):
        xc = _BORDER + fi * _SQ + _SQ // 2
        # Draw background circle for better visibility
        draw2.ellipse([(xc - 15), (_BORDER // 2 - 15), (xc + 15), (_BORDER // 2 + 15)], 
                      fill=(40, 35, 30, 200))
        draw2.text((xc, _BORDER // 2), label, font=lfont, fill=(220, 200, 150, 255), anchor="mm")
        
        draw2.ellipse([(xc - 15), (_BSIZE - _BORDER // 2 - 15), (xc + 15), (_BSIZE - _BORDER // 2 + 15)], 
                      fill=(40, 35, 30, 200))
        draw2.text((xc, _BSIZE - _BORDER // 2), label, font=lfont, fill=(220, 200, 150, 255), anchor="mm")

    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


def _chess_file(game: dict) -> discord.File:
    board     = game["board"]
    flipped   = game.get("flipped", False)
    last_move = board.peek() if board.move_stack else None
    img_bytes = _render_board_image(board, flipped=flipped, last_move=last_move)
    return discord.File(_io.BytesIO(img_bytes), filename="board.png")


def _chess_embed(game: dict, title: str = "♟️ Chess", msg: str = "") -> discord.Embed:
    board      = game["board"]
    white      = game["white"]
    black      = game["black"]
    turn_name  = white.display_name if board.turn == _chess.WHITE else black.display_name
    turn_emoji = "⬜" if board.turn == _chess.WHITE else "⬛"
    colour     = 0xB5893B if board.turn == _chess.WHITE else 0x3B4B5A

    e = discord.Embed(title=title, colour=colour)
    e.set_image(url="attachment://board.png")

    status = ""
    if board.is_checkmate():
        winner = black if board.turn == _chess.WHITE else white
        status = f"♟️ Checkmate! **{winner.display_name}** wins!"
    elif board.is_stalemate():
        status = "🤝 Stalemate — it's a draw!"
    elif board.is_insufficient_material():
        status = "🤝 Draw — insufficient material!"
    elif board.is_check():
        status = f"⚠️ **{turn_name}** is in CHECK!"

    if status: e.add_field(name="📢 Status", value=status, inline=False)
    if msg:    e.add_field(name="📢", value=msg, inline=False)

    e.add_field(name="⬜ White", value=white.mention, inline=True)
    e.add_field(name="⬛ Black", value=black.mention, inline=True)
    e.add_field(name=f"{turn_emoji} Turn", value=f"**{turn_name}** to move", inline=True)
    e.set_footer(text=f"Move {board.fullmove_number} • Pick a move from the dropdown • !resign • !draw • !hint • !undo")
    return e


active_chess:    dict[int, dict] = {}
active_akinator: dict[int, dict] = {}

_PIECE_NAMES = {
    _chess.PAWN: "Pawn", _chess.ROOK: "Rook", _chess.KNIGHT: "Knight",
    _chess.BISHOP: "Bishop", _chess.QUEEN: "Queen", _chess.KING: "King",
}
_PIECE_EMOJI = {
    _chess.PAWN:   ("♙", "♟"), _chess.ROOK:   ("♖", "♜"),
    _chess.KNIGHT: ("♘", "♞"), _chess.BISHOP: ("♗", "♝"),
    _chess.QUEEN:  ("♕", "♛"), _chess.KING:   ("♔", "♚"),
}


def _piece_label(piece, square: int) -> str:
    color_idx = 0 if piece.color == _chess.WHITE else 1
    emoji     = _PIECE_EMOJI[piece.piece_type][color_idx]
    name      = _PIECE_NAMES[piece.piece_type]
    sq_name   = _chess.square_name(square).upper()
    return f"{emoji} {name} on {sq_name}"


def _square_label(board, move) -> str:
    to_sq   = move.to_square
    sq_name = _chess.square_name(to_sq).upper()
    target  = board.piece_at(to_sq)
    if board.is_castling(move):
        side = "Kingside" if _chess.square_file(to_sq) > 4 else "Queenside"
        return f"→ Castle {side}"
    if board.is_en_passant(move):
        return f"→ {sq_name} (en passant)"
    if move.promotion:
        return f"→ {sq_name} (promote to {_PIECE_NAMES.get(move.promotion, '')})"
    if target:
        color_idx = 0 if target.color == _chess.WHITE else 1
        return f"→ {sq_name} (capture {_PIECE_EMOJI[target.piece_type][color_idx]})"
    return f"→ {sq_name}"


def _movable_pieces(board) -> list:
    result = []
    for sq in _chess.SQUARES:
        piece = board.piece_at(sq)
        if not piece or piece.color != board.turn:
            continue
        moves = [m for m in board.legal_moves if m.from_square == sq]
        if moves:
            result.append((sq, piece, moves))
    return result


# ── Chess challenge invite view ───────────────────────────────────────────────

class ChessChallengeView(discord.ui.View):
    """Sent to the opponent so they can Accept or Decline."""

    def __init__(self, challenger: discord.Member, opponent: discord.Member,
                 channel: discord.TextChannel):
        super().__init__(timeout=20)
        self.challenger = challenger
        self.opponent   = opponent
        self.channel    = channel
        self.message: discord.Message | None = None

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("❌ This challenge isn't for you!", ephemeral=True)
            return
        self.stop()

        if self.channel.id in active_chess:
            await interaction.response.edit_message(
                content="⚠️ A chess game already started in this channel before you accepted.",
                view=None
            )
            return

        if random.random() < 0.5:
            white, black = self.challenger, self.opponent
        else:
            white, black = self.opponent, self.challenger

        game = {
            "board":           _chess.Board(),
            "white":           white,
            "black":           black,
            "flipped":         False,
            "draw_offered_by": None,
            "move_history":    [],   # list of move.uci() strings, for undo
            "hints_used":      {},   # str(user_id) -> hints used on the current move
            "undo_offered_by": None,
        }
        active_chess[self.channel.id] = game

        # embed = _chess_embed(game, title="♟️ Chess — Game Start",
        #                      msg=f"{white.mention} ⬜ vs {black.mention} ⬛\n\n**{white.display_name}** goes first!\nUse the dropdown below — pick any move in algebraic notation (e.g. ♙ e4, ♘ Nf3).")
        # view  = ChessMoveSelect(game, white)
        embed = _chess_embed(game, title="♟️ Chess — Game Start",
                     msg=f"{white.mention} ⬜ vs {black.mention} ⬛\n\n**{white.display_name}** goes first!\nPick a piece from the buttons below to move.")
        view = ChessMoveSelect(game, white)
        
        await interaction.response.edit_message(
            content=f"✅ Challenge accepted! {white.mention} vs {black.mention} — {white.mention} it's your turn!",
            embed=embed,
            attachments=[_chess_file(game)],
            view=view,
        )

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("❌ This challenge isn't for you!", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(
            content=f"❌ **{self.opponent.display_name}** declined the chess challenge.",
            view=None,
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    content=(
                        f"⌛ **Chess challenge expired!**\n"
                        f"{self.opponent.mention} didn't respond to {self.challenger.mention}'s "
                        f"challenge in time."
                    ),
                    embed=None,
                    view=self,
                )
            except discord.HTTPException:
                pass


# ── Chess undo agreement view ──────────────────────────────────────────────────

class ChessUndoView(discord.ui.View):
    """Shown to the opponent: agree to undo the last move or decline."""

    def __init__(self, game: dict, channel_id: int, requester: discord.Member, opponent: discord.Member):
        super().__init__(timeout=60)
        self.game = game
        self.channel_id = channel_id
        self.requester = requester
        self.opponent = opponent

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("❌ This request isn't for you!", ephemeral=True)
            return False
        return True

    def _disable(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="✅ Agree", style=discord.ButtonStyle.success)
    async def agree(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable()
        if self.channel_id not in active_chess or active_chess[self.channel_id] is not self.game:
            await interaction.response.edit_message(content="⚠️ This game is no longer active.", view=self)
            self.stop()
            return

        board = self.game["board"]
        history = self.game.get("move_history", [])
        if not history:
            await interaction.response.edit_message(content="⚠️ Nothing to undo.", view=self)
            self.stop()
            return

        board.pop()
        history.pop()
        self.game["draw_offered_by"] = None
        self.game["undo_offered_by"] = None
        self.game["hints_used"] = {}

        next_player = self.game["white"] if board.turn == _chess.WHITE else self.game["black"]
        await interaction.response.edit_message(
            content=f"⏪ **{self.opponent.display_name}** agreed — last move undone! {next_player.mention}, your turn again.",
            embed=_chess_embed(self.game, msg="Move undone."),
            attachments=[_chess_file(self.game)],
            view=ChessMoveSelect(self.game, next_player),
        )
        self.stop()

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable()
        if self.channel_id in active_chess:
            active_chess[self.channel_id]["undo_offered_by"] = None
        await interaction.response.edit_message(
            content=f"❌ **{self.opponent.display_name}** declined the undo request.",
            view=self,
        )
        self.stop()

    async def on_timeout(self):
        self._disable()
        if self.channel_id in active_chess:
            active_chess[self.channel_id]["undo_offered_by"] = None



# ── Chess smart move selector ─────────────────────────────────────────────────
# Generates algebraic-notation move labels so the player never needs to think
# in raw coordinates.  One dropdown lists every legal move as e.g. "♙ e4",
# "♘ Nf3", "O-O" — no two-step pick-piece-then-destination needed.

def _san_with_emoji(board: "_chess.Board", move: "_chess.Move") -> str:
    """Return a short, readable label for a move, e.g. '♙ e4', '♘ Nf3', 'O-O'."""
    piece  = board.piece_at(move.from_square)
    if not piece:
        return board.san(move)
    emoji  = _PIECE_EMOJI[piece.piece_type][0 if piece.color == _chess.WHITE else 1]
    try:
        san = board.san(move)
    except Exception:
        san = move.uci()
    return f"{emoji} {san}"


def _build_move_options(board: "_chess.Board") -> list:
    """
    Return up to 25 SelectOptions covering all legal moves for the current player,
    sorted by piece type (King, Queen, Rook, Bishop, Knight, Pawn) then by SAN.
    Each option value = move.uci().
    Duplicate SAN labels are de-duped by appending the origin square.
    """
    order = [_chess.KING, _chess.QUEEN, _chess.ROOK,
             _chess.BISHOP, _chess.KNIGHT, _chess.PAWN]
    moves_by_type: dict = {pt: [] for pt in order}
    for move in board.legal_moves:
        piece = board.piece_at(move.from_square)
        if piece:
            moves_by_type.setdefault(piece.piece_type, []).append(move)

    options  = []
    seen_san = {}
    for pt in order:
        for move in sorted(moves_by_type.get(pt, []),
                           key=lambda m: board.san(m) if board.is_legal(m) else m.uci()):
            if len(options) >= 25:
                break
            label = _san_with_emoji(board, move)
            # de-dup: if SAN label is ambiguous add origin square hint
            base = label
            if base in seen_san:
                label = f"{base} ({_chess.square_name(move.from_square)})"
            seen_san[base] = True
            options.append(discord.SelectOption(label=label, value=move.uci()))
        if len(options) >= 25:
            break

    return options


class ChessMoveSelect(discord.ui.View):
    """Piece-based chess move selector - pick a piece first, then destination."""
    
    def __init__(self, game: dict, player: discord.Member):
        super().__init__(timeout=180)
        self.game = game
        self.player = player
        self.selected_piece = None
        self._build_piece_buttons()
    
    def _build_piece_buttons(self):
        """Create buttons for each piece that can move."""
        board = self.game["board"]
        movable_pieces = []
        
        for sq in _chess.SQUARES:
            piece = board.piece_at(sq)
            if piece and piece.color == board.turn:
                moves = [m for m in board.legal_moves if m.from_square == sq]
                if moves:
                    movable_pieces.append((sq, piece, moves))
        
        # Clear existing items
        self.clear_items()
        
        # Add piece buttons (max 20 pieces)
        for sq, piece, moves in movable_pieces[:20]:
            piece_name = _PIECE_NAMES[piece.piece_type]
            emoji = _PIECE_EMOJI[piece.piece_type][0 if piece.color == _chess.WHITE else 1]
            square_name = _chess.square_name(sq).upper()
            
            button = discord.ui.Button(
                label=f"{emoji} {piece_name} on {square_name}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"piece_{sq}"
            )
            button.callback = self._make_piece_callback(sq, piece, moves)
            self.add_item(button)
        
        # Add action buttons at the bottom
        resign_btn = discord.ui.Button(label="🏳️ Resign", style=discord.ButtonStyle.danger)
        draw_btn = discord.ui.Button(label="🤝 Offer Draw", style=discord.ButtonStyle.secondary)
        
        resign_btn.callback = self._resign_callback
        draw_btn.callback = self._draw_callback
        
        self.add_item(resign_btn)
        self.add_item(draw_btn)
    
    def _make_piece_callback(self, from_sq, piece, moves):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.player.id:
                await interaction.response.send_message("❌ It's not your turn!", ephemeral=True)
                return
            
            self.selected_piece = from_sq
            # Show destination selector as a new message
            view = DestinationSelect(self.game, self.player, from_sq, moves)
            embed = _chess_embed(self.game, msg=f"Selected {_PIECE_NAMES[piece.piece_type]}. Where to move?")
            
            await interaction.response.send_message(
                content=f"♟️ **{self.player.display_name}** - Choose destination for {_PIECE_NAMES[piece.piece_type]}:",
                embed=embed,
                file=_chess_file(self.game),
                view=view,
                ephemeral=False
            )
        return callback
    
    async def _resign_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("❌ Only the player can resign!", ephemeral=True)
            return
        
        winner = self.game["black"] if self.player.id == self.game["white"].id else self.game["white"]
        channel_id = interaction.channel_id
        
        if channel_id in active_chess:
            del active_chess[channel_id]
        
        await interaction.response.edit_message(
            content=f"🏳️ **{self.player.display_name}** resigned. **{winner.display_name}** wins!",
            embed=_chess_embed(self.game, title="♟️ Resignation"),
            attachments=[_chess_file(self.game)],
            view=None
        )
    
    async def _draw_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("❌ Only the player can offer a draw!", ephemeral=True)
            return
        
        game = self.game
        offered = game.get("draw_offered_by")
        
        if offered is None:
            game["draw_offered_by"] = self.player.id
            opponent = game["black"] if self.player.id == game["white"].id else game["white"]
            await interaction.response.send_message(
                f"🤝 **{self.player.display_name}** offers a draw. {opponent.mention} use the draw button to accept.",
                ephemeral=False
            )
        elif offered == self.player.id:
            await interaction.response.send_message("⏳ You already offered a draw.", ephemeral=True)
        else:
            # Opponent accepted - end the game
            channel_id = interaction.channel_id
            if channel_id in active_chess:
                del active_chess[channel_id]
            await interaction.response.edit_message(
                content="🤝 Both players agreed to a draw!",
                embed=_chess_embed(game, title="♟️ Draw Agreed"),
                attachments=[_chess_file(game)],
                view=None
            )
    
    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

class DestinationSelect(discord.ui.View):
    """Destination selector - shows where a selected piece can move."""
    
    def __init__(self, game: dict, player: discord.Member, from_sq: int, moves: list):
        super().__init__(timeout=60)
        self.game = game
        self.player = player
        self.from_sq = from_sq
        
        # Add destination buttons (max 23 due to Discord limit)
        for move in moves[:23]:
            to_sq = move.to_square
            to_name = _chess.square_name(to_sq).upper()
            piece = game["board"].piece_at(to_sq)
            
            if piece:
                emoji = _PIECE_EMOJI[piece.piece_type][0 if piece.color == _chess.WHITE else 1]
                label = f"Capture {emoji} on {to_name}"
            else:
                label = f"Move to {to_name}"
            
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, custom_id=f"dest_{to_sq}")
            button.callback = self._make_dest_callback(move)
            self.add_item(button)
        
        # Add cancel button to go back to piece selection
        cancel = discord.ui.Button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel_callback
        self.add_item(cancel)
    
    def _make_dest_callback(self, chess_move):  # Renamed parameter to avoid conflict
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.player.id:
                await interaction.response.send_message("❌ It's not your turn!", ephemeral=True)
                return
            
            board = self.game["board"]
            move_to_make = chess_move  # Use the parameter from outer function
            
            # Handle pawn promotion - default to queen for simplicity
            if move_to_make.promotion is None and board.piece_at(move_to_make.from_square):
                piece = board.piece_at(move_to_make.from_square)
                to_rank = _chess.square_rank(move_to_make.to_square)
                if piece.piece_type == _chess.PAWN and to_rank in (0, 7):
                    move_to_make = _chess.Move(move_to_make.from_square, move_to_make.to_square, _chess.QUEEN)
            
            board.push(move_to_make)
            self.game["draw_offered_by"] = None
            self.game["undo_offered_by"] = None
            self.game.setdefault("move_history", []).append(move_to_make.uci())
            self.game["hints_used"] = {}
            
            # Check for checkmate
            if board.is_checkmate():
                winner = self.game["black"] if board.turn == _chess.WHITE else self.game["white"]
                channel_id = interaction.channel_id
                if channel_id in active_chess:
                    del active_chess[channel_id]
                await interaction.response.edit_message(
                    content=f"🏆 **{winner.display_name}** wins by checkmate!",
                    embed=_chess_embed(self.game, title="♟️ Checkmate!"),
                    attachments=[_chess_file(self.game)],
                    view=None
                )
                return
            
            # Check for draw
            if board.is_stalemate() or board.is_insufficient_material() or board.is_seventyfive_moves():
                channel_id = interaction.channel_id
                if channel_id in active_chess:
                    del active_chess[channel_id]
                await interaction.response.edit_message(
                    content="🤝 The game is a draw!",
                    embed=_chess_embed(self.game, title="♟️ Draw!"),
                    attachments=[_chess_file(self.game)],
                    view=None
                )
                return
            
            # Next player's turn
            next_player = self.game["white"] if board.turn == _chess.WHITE else self.game["black"]
            status_msg = "⚠️ Check! Your king is under attack." if board.is_check() else f"Move played! Your turn, {next_player.mention}."
            
            next_view = ChessMoveSelect(self.game, next_player)
            
            await interaction.response.edit_message(
                content=f"{next_player.mention} — your turn!",
                embed=_chess_embed(self.game, msg=status_msg),
                attachments=[_chess_file(self.game)],
                view=next_view
            )
        return callback
    
    async def _cancel_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("❌ This isn't your game!", ephemeral=True)
            return
        
        # Go back to piece selection
        view = ChessMoveSelect(self.game, self.player)
        await interaction.response.edit_message(
            content=f"{self.player.mention} — choose a piece to move:",
            embed=_chess_embed(self.game),
            attachments=[_chess_file(self.game)],
            view=view
        )
    
    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

ChessPieceSelect = ChessMoveSelect
    

# ═══════════════════════════════════════════════════════════════════════════════
#  MAFIA  ── Upgraded Edition
#  New features:
#    • More roles: Vigilante, Jester, Mayor, Serial Killer, Bodyguard, Spy, Escort
#    • Auto-advance day when ALL alive players voted OR day timer expires
#    • Auto-advance night when ALL special roles submitted actions OR night timer expires
#    • Countdown timer displayed on the embed (updated live)
#    • Vote-lock: once everyone votes, day auto-ends immediately
#    • Night action tracker: shows "X/Y roles submitted" publicly
#    • Skip vote (abstain) support
#    • Tie-breaking: no elimination on tie
#    • Anonymous voting mode (host choice) with big reveal at end of day
#    • Last-will system: players write a will that's revealed on death
#    • Mafia chat: mafia members can DM each other via a shared relay
#    • Lobby: host can choose game mode (Classic / Extended roles)
#    • Per-player action status tracking
#    • Rich end-game stats (who killed who, who saved who)
# ═══════════════════════════════════════════════════════════════════════════════

import time as _time

active_mafia: dict[int, dict] = {}   # channel_id → game state
_mafia_timers: dict[int, asyncio.Task] = {}   # channel_id → auto-advance task

# ── Role registry ──────────────────────────────────────────────────────────────

MAFIA_ROLES = {
    "mafia":        "🔫",
    "detective":    "🔍",
    "doctor":       "💉",
    "villager":     "🏘️",
    "vigilante":    "🎯",
    "jester":       "🃏",
    "mayor":        "🎖️",
    "serialkiller": "🪓",
    "bodyguard":    "🛡️",
    "spy":          "🕵️",
    "escort":       "💃",
}

MAFIA_ROLE_COLOR = {
    "mafia":        discord.Color.red(),
    "detective":    discord.Color.blue(),
    "doctor":       discord.Color.green(),
    "villager":     discord.Color.gold(),
    "vigilante":    discord.Color.orange(),
    "jester":       discord.Color.purple(),
    "mayor":        discord.Color.yellow(),
    "serialkiller": discord.Color.dark_red(),
    "bodyguard":    discord.Color.teal(),
    "spy":          discord.Color.blurple(),
    "escort":       discord.Color.magenta(),
}

MAFIA_ROLE_TIPS = {
    "mafia":        "Blend in during the day. Vote with the village early so you don't seem suspicious.",
    "detective":    "Don't reveal yourself too soon — once the Mafia knows who you are, you're a target.",
    "doctor":       "Consider protecting yourself or the Detective. You can also protect the same person twice.",
    "villager":     "Pay close attention to who votes for whom. Patterns reveal the Mafia.",
    "vigilante":    "You get ONE kill. Use it wisely — don't shoot innocents or you'll feel guilty.",
    "jester":       "Your goal is to get VOTED OUT by the village. Act suspicious but don't overdo it!",
    "mayor":        "Once you reveal yourself your vote counts x3, but you become a prime Mafia target.",
    "serialkiller": "You win ALONE. Kill Mafia and Village alike — but survive the vote each day.",
    "bodyguard":    "Protect someone at night. If they're attacked, YOU die instead of them.",
    "spy":          "You see who the Mafia visits each night, but not what they do. Powerful info!",
    "escort":       "Block a player's night action by distracting them. You don't know their role.",
}

MAFIA_ROLE_DESC = {
    "mafia":        "Each night, the Mafia chooses one player to eliminate.",
    "detective":    "Each night, investigate one player — learn if they're Mafia.",
    "doctor":       "Each night, protect one player from elimination (including yourself).",
    "villager":     "No special power. Your weapon is instinct — find the Mafia by day.",
    "vigilante":    "Once per game, shoot one player at night. If they're innocent you'll feel guilt (you die next night). Wins with Village.",
    "jester":       "You have no power. Your goal is to get voted out by the village. If lynched, you WIN!",
    "mayor":        "Once per game, reveal yourself publicly. Your vote then counts as THREE. Wins with Village.",
    "serialkiller": "Kill one player each night. Immune to Doctor saves. Win alone when you're the last one standing.",
    "bodyguard":    "Protect one player each night. If the Mafia attacks them, the Bodyguard dies instead. Wins with Village.",
    "spy":          "Each night, see WHO the Mafia visits (not the action). Wins with Village.",
    "escort":       "Each night, role-block one player — they cannot use their night action. Wins with Village.",
}

# Which roles need a night action DM
NIGHT_ACTION_ROLES = {"mafia", "detective", "doctor", "vigilante", "serialkiller", "bodyguard", "spy", "escort"}

# Which faction each role belongs to
ROLE_FACTION = {
    "mafia":        "mafia",
    "detective":    "village",
    "doctor":       "village",
    "villager":     "village",
    "vigilante":    "village",
    "jester":       "jester",       # wins alone by getting lynched
    "mayor":        "village",
    "serialkiller": "serialkiller", # wins alone
    "bodyguard":    "village",
    "spy":          "village",
    "escort":       "village",
}

# Timing constants
DAY_TIMEOUT   = 30    # seconds before day auto-advances
NIGHT_TIMEOUT = 30    # seconds before night auto-advances
LOBBY_TIMEOUT = 180   # lobby open time


def _assign_mafia_roles(players: list, mode: str = "classic") -> dict:
    """Assign roles based on player count and chosen mode.

    Guaranteed core (every game, every mode):
      1x Mafia, 1x Doctor, 2x Villager  (minimum 4 players)

    Extra slots unlock as player count grows.
    """
    n = len(players)

    # Guaranteed core — always present regardless of mode or player count
    roles: list[str] = ["mafia", "doctor", "villager", "villager"]

    # Extra mafia at higher counts (shared across all modes)
    if n >= 7:
        roles += ["mafia"]   # 2nd mafia at 7+
    if n >= 11:
        roles += ["mafia"]   # 3rd mafia at 11+

    # Extra special roles based on mode
    if mode == "classic":
        if n >= 5:
            roles += ["detective"]

    elif mode == "extended":
        if n >= 5:
            roles += ["detective"]
        if n >= 6:
            roles += ["vigilante"]
        if n >= 8:
            roles += ["mayor"]
        if n >= 9:
            roles += ["bodyguard"]
        if n >= 10:
            roles += ["spy"]
        if n >= 11:
            roles += ["escort"]
        if n >= 12:
            roles += ["jester"]

    elif mode == "chaos":
        if n >= 5:
            roles += ["detective"]
        if n >= 6:
            roles += ["serialkiller"]
        if n >= 7:
            roles += ["vigilante"]
        if n >= 8:
            roles += ["jester"]
        if n >= 9:
            roles += ["bodyguard"]
        if n >= 10:
            roles += ["escort"]
        if n >= 11:
            roles += ["spy"]
        if n >= 12:
            roles += ["mayor"]

    # Pad remaining slots with villagers, then trim to exact count
    roles += ["villager"] * max(0, n - len(roles))
    roles = roles[:n]

    random.shuffle(players)
    return {
        p.id: {
            "member":       p,
            "role":         r,
            "alive":        True,
            "last_will":    "",
            "revealed":     False,
            "guilt":        False,
            "protected_by": None,
        }
        for p, r in zip(players, roles)
    }


def _mafia_alive(game: dict):
    return [v for v in game["players"].values() if v["alive"]]


def _count_night_actors(game: dict) -> tuple[int, int]:
    """Return (submitted, total) for night action roles."""
    actions = game.get("night_actions", {})
    total = sum(
        1 for v in game["players"].values()
        if v["alive"] and v["role"] in NIGHT_ACTION_ROLES
        and not (v["role"] == "escort" and False)  # always counts
    )
    # Deduplicate: mafia acts as one unit
    mafia_alive = [v for v in game["players"].values() if v["alive"] and v["role"] == "mafia"]
    if mafia_alive:
        total = total - len(mafia_alive) + 1  # count mafia as one actor

    submitted = len(actions)
    return submitted, total


def _count_votes(game: dict) -> tuple[int, int]:
    """Return (voted, total_alive) for day voting.
    voted_players tracks everyone who has cast a vote OR abstained."""
    alive_count = sum(1 for v in game["players"].values() if v["alive"])
    # voted_players is a set of uids who have made a choice (including abstain)
    voted_count = len(game.get("voted_players", set()))
    return voted_count, alive_count


def _mafia_win_check(game: dict) -> str | None:
    alive    = _mafia_alive(game)
    n_mafia  = sum(1 for v in alive if v["role"] == "mafia")
    n_sk     = sum(1 for v in alive if v["role"] == "serialkiller")
    n_other  = len(alive) - n_mafia - n_sk

    # Serial killer wins alone when they're the last/only non-village or only survivor
    if n_sk > 0 and n_mafia == 0 and n_other <= n_sk:
        return "serialkiller"

    if n_mafia == 0 and n_sk == 0:
        return "village"

    # Mafia/SK wins when they outnumber or equal village
    if n_mafia >= n_other and n_sk == 0:
        return "mafia"

    if n_sk >= n_other + n_mafia and n_mafia == 0:
        return "serialkiller"

    return None


def _mafia_embed(game: dict, title="🎭 Mafia", msg="") -> discord.Embed:
    alive  = _mafia_alive(game)
    dead   = [v for v in game["players"].values() if not v["alive"]]
    phase  = game.get("phase", "lobby")
    day    = game.get("day", 1)

    if phase == "night":
        color = discord.Color.from_rgb(30, 20, 60)
    elif phase == "day":
        color = discord.Color.from_rgb(255, 200, 60)
    else:
        color = discord.Color.blurple()

    e = discord.Embed(title=title, color=color)
    if msg:
        e.description = msg

    # Alive list with mayor reveal indicator
    alive_lines = []
    for v in alive:
        name = v["member"].display_name
        if v.get("revealed") and v["role"] == "mayor":
            name += " 🎖️*(Mayor)*"
        alive_lines.append(f"✅ {name}")
    alive_list = "\n".join(alive_lines) or "—"

    # Dead list with roles revealed
    dead_lines = []
    for v in dead:
        role_emoji = MAFIA_ROLES.get(v["role"], "❓")
        dead_lines.append(f"💀 {v['member'].display_name} ({role_emoji})")
    dead_list = "\n".join(dead_lines) or "None yet"

    e.add_field(name=f"🟢 Alive ({len(alive)})", value=alive_list,   inline=True)
    e.add_field(name=f"💀 Dead ({len(dead)})",   value=dead_list,    inline=True)

    # Phase info
    phase_str = f"**{phase.capitalize()}** (Day {day})"
    e.add_field(name="📅 Phase", value=phase_str, inline=False)

    # Show vote progress during day
    if phase == "day":
        voted, total = _count_votes(game)
        vote_bar = _progress_bar(voted, total)
        e.add_field(name=f"🗳️ Votes ({voted}/{total})", value=vote_bar, inline=False)

    # Show night action progress
    if phase == "night":
        submitted, total = _count_night_actors(game)
        night_bar = _progress_bar(submitted, total)
        e.add_field(name=f"🌙 Actions ({submitted}/{total} submitted)", value=night_bar, inline=False)

    # Countdown timer
    deadline = game.get("phase_deadline")
    if deadline:
        remaining = max(0, int(deadline - _time.time()))
        mins, secs = divmod(remaining, 60)
        timer_str = f"⏱️ `{mins}:{secs:02d}` remaining"
        if remaining <= 30:
            timer_str = f"⚠️ **{timer_str}**"
        e.add_field(name="⏰ Timer", value=timer_str, inline=False)

    # Mode badge
    mode = game.get("mode", "classic")
    mode_label = {"classic": "🎭 Classic", "extended": "⚔️ Extended", "chaos": "🌀 Chaos"}.get(mode, mode)
    e.set_footer(text=f"{mode_label} • {len(game['players'])} players")
    return e


def _progress_bar(done: int, total: int, width: int = 10) -> str:
    if total == 0:
        return "`[──────────]` 0/0"
    filled = round((done / total) * width)
    bar = "█" * filled + "─" * (width - filled)
    return f"`[{bar}]` {done}/{total}"


# ── Last Will system ────────────────────────────────────────────────────────────

class LastWillModal(discord.ui.Modal, title="✍️ Write Your Last Will"):
    will_text = discord.ui.TextInput(
        label="Your last will (revealed when you die)",
        style=discord.TextStyle.long,
        placeholder="Write anything you want the town to know…",
        required=False,
        max_length=400,
    )

    def __init__(self, game: dict, user_id: int):
        super().__init__()
        self.game    = game
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        p = self.game["players"].get(self.user_id)
        if p:
            p["last_will"] = self.will_text.value.strip()
        await interaction.response.send_message(
            "✅ Last will saved! It will be revealed when you die.", ephemeral=True
        )


# ── Lobby ───────────────────────────────────────────────────────────────────────

class MafiaJoinView(discord.ui.View):
    """Lobby: players join + host configures mode before starting."""

    def __init__(self, host: discord.Member, channel_id: int):
        super().__init__(timeout=LOBBY_TIMEOUT)
        self.host       = host
        self.channel_id = channel_id

    def _player_list(self, game: dict) -> str:
        return ", ".join(v["member"].display_name for v in game["players"].values()) or "—"

    @discord.ui.button(label="🙋 Join Game", style=discord.ButtonStyle.primary, row=0)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = active_mafia.get(self.channel_id)
        if not game or game["phase"] != "lobby":
            await interaction.response.send_message("❌ No lobby open.", ephemeral=True)
            return
        uid = interaction.user.id
        if uid in game["players"]:
            await interaction.response.send_message("⚠️ You're already in!", ephemeral=True)
            return
        game["players"][uid] = {
            "member": interaction.user, "role": None, "alive": True,
            "last_will": "", "revealed": False, "guilt": False, "protected_by": None,
        }
        embed = _mafia_lobby_embed(game)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🚪 Leave", style=discord.ButtonStyle.secondary, row=0)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = active_mafia.get(self.channel_id)
        if not game or game["phase"] != "lobby":
            await interaction.response.send_message("❌ No lobby open.", ephemeral=True)
            return
        uid = interaction.user.id
        if uid == self.host.id:
            await interaction.response.send_message("❌ Host can't leave — use `!stopmafia` to cancel.", ephemeral=True)
            return
        if uid not in game["players"]:
            await interaction.response.send_message("❌ You're not in the lobby.", ephemeral=True)
            return
        del game["players"][uid]
        embed = _mafia_lobby_embed(game)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.select(
        placeholder="🎮 Game Mode (host only)",
        options=[
            discord.SelectOption(label="🎭 Classic",  value="classic",  description="Mafia, Detective, Doctor, Villagers"),
            discord.SelectOption(label="⚔️ Extended", value="extended", description="+ Vigilante, Mayor, Bodyguard, Spy, Escort, Jester"),
            discord.SelectOption(label="🌀 Chaos",    value="chaos",    description="+ Serial Killer, more wild roles"),
        ],
        row=1,
    )
    async def mode_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self.host.id:
            await interaction.response.send_message("❌ Only the host can change the mode.", ephemeral=True)
            return
        game = active_mafia.get(self.channel_id)
        if game:
            game["mode"] = select.values[0]
        embed = _mafia_lobby_embed(game)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🚀 Start (Host)", style=discord.ButtonStyle.success, row=2)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host.id:
            await interaction.response.send_message("❌ Only the host can start.", ephemeral=True)
            return
        game = active_mafia.get(self.channel_id)
        if not game:
            await interaction.response.send_message("❌ No lobby found.", ephemeral=True)
            return
        if len(game["players"]) < 4:
            await interaction.response.send_message("❌ Need at least **4 players** to start!", ephemeral=True)
            return
        self.stop()
        await interaction.response.defer()
        await _mafia_begin(interaction, game)

    async def on_timeout(self):
        game = active_mafia.get(self.channel_id)
        if game and game["phase"] == "lobby":
            del active_mafia[self.channel_id]


def _mafia_lobby_embed(game: dict) -> discord.Embed:
    players = game["players"]
    mode    = game.get("mode", "classic")
    n       = len(players)

    mode_info = {
        "classic":  ("🎭 Classic",  "Mafia · Detective · Doctor · Villagers"),
        "extended": ("⚔️ Extended", "Classic + Vigilante · Mayor · Bodyguard · Spy · Escort · Jester"),
        "chaos":    ("🌀 Chaos",    "Extended + Serial Killer · extra wild roles"),
    }
    mode_label, mode_desc = mode_info.get(mode, ("🎭 Classic", ""))

    player_list = "\n".join(f"{'👑' if i == 0 else '🎮'} {v['member'].display_name}"
                             for i, v in enumerate(players.values())) or "—"

    e = discord.Embed(
        title="🎭 Mafia — Lobby",
        description=f"**{mode_label}**\n_{mode_desc}_\n\nWaiting for players... (**{n}/4+ needed**)",
        color=discord.Color.blurple(),
    )
    e.add_field(name=f"👥 Players ({n})", value=player_list, inline=False)
    e.set_footer(text="Host can change mode with the dropdown • Min 4 players")
    return e


# ── Role card DMs ───────────────────────────────────────────────────────────────

def _mafia_role_dm_embed(role: str, guild_name: str, teammates: list | None = None) -> discord.Embed:
    emoji = MAFIA_ROLES[role]
    color = MAFIA_ROLE_COLOR[role]
    tip   = MAFIA_ROLE_TIPS[role]
    desc  = MAFIA_ROLE_DESC[role]

    e = discord.Embed(
        title=f"{emoji}  You are the **{role.replace('serialkiller','Serial Killer').capitalize()}**",
        description=desc,
        color=color,
    )
    e.add_field(name="🏠 Server",  value=guild_name, inline=True)
    e.add_field(name="💡 Tip",     value=tip,         inline=False)
    if teammates:
        names = ", ".join(m.display_name for m in teammates)
        e.add_field(name="🔫 Mafia Teammates", value=names, inline=False)
    e.set_footer(text="Keep your role secret! Write your last will with !lastwill 🎭")
    return e


# ── Game start ──────────────────────────────────────────────────────────────────

async def _mafia_begin(interaction: discord.Interaction, game: dict):
    """Assign roles, send DMs, then kick off Night 0 (before any discussion)."""
    player_list = [v["member"] for v in game["players"].values()]
    mode        = game.get("mode", "classic")
    assigned    = _assign_mafia_roles(player_list, mode)
    game["players"]       = assigned
    game["phase"]         = "night"
    game["day"]           = 1
    game["votes"]         = {}
    game["night_actions"] = {}
    game["kill_log"]      = []
    game["escort_blocks"] = {}
    game["spy_reports"]   = {}
    game["spy_last_visits"] = []
    game["vig_used"]      = False

    # Identify mafia for teammate reveal
    mafia_members = [v["member"] for v in assigned.values() if v["role"] == "mafia"]

    dm_failures = []
    for uid, v in assigned.items():
        role = v["role"]
        teammates = mafia_members if role == "mafia" and len(mafia_members) > 1 else None
        try:
            await v["member"].send(embed=_mafia_role_dm_embed(role, interaction.guild.name, teammates))
        except discord.Forbidden:
            dm_failures.append(v["member"].display_name)

    fail_note = f"\n⚠️ Could not DM: {', '.join(dm_failures)} — they should enable DMs." if dm_failures else ""

    embed = _mafia_embed(
        game,
        title="🌙 Mafia — Night 0 Begins",
        msg=(
            "Roles have been sent via DM! **Night falls immediately.**\n"
            "Special roles — check your DMs and submit your action.\n"
            "Night auto-resolves when all actions are in, or after 30 seconds." + fail_note
        )
    )
    view = MafiaNightView(game, interaction.channel_id)

    await interaction.edit_original_response(content="✅ Game started! See below.", embed=None, view=None)
    msg = await interaction.channel.send(embed=embed, view=view)
    game["last_msg"] = msg

    await _send_night_dms(game, interaction.channel_id, bot=interaction.client)
    await _start_phase_timer(interaction.channel, game, interaction.channel_id)


# ── Timer engine ────────────────────────────────────────────────────────────────

async def _start_phase_timer(channel, game: dict, channel_id: int):
    """Cancel any existing timer and start a new one for the current phase."""
    _cancel_timer(channel_id)
    timeout = DAY_TIMEOUT if game["phase"] == "day" else NIGHT_TIMEOUT
    game["phase_deadline"] = _time.time() + timeout
    task = asyncio.get_event_loop().create_task(
        _phase_timer_task(channel, game, channel_id, timeout)
    )
    _mafia_timers[channel_id] = task


def _cancel_timer(channel_id: int):
    task = _mafia_timers.pop(channel_id, None)
    if task and not task.done():
        task.cancel()


async def _phase_timer_task(channel, game: dict, channel_id: int, timeout: float):
    """Wait for timeout, then auto-advance the phase."""
    try:
        # With 30s timers, do one update at ~15s mark then advance at 30s
        await asyncio.sleep(timeout / 2)
        if channel_id not in active_mafia or active_mafia[channel_id] is not game:
            return
        # Mid-point embed refresh to show countdown
        try:
            msg = game.get("last_msg")
            if msg:
                phase = game["phase"]
                title = f"☀️ Day {game['day']}" if phase == "day" else f"🌙 Night {game['day']}"
                await msg.edit(embed=_mafia_embed(game, title=title))
        except Exception:
            pass

        await asyncio.sleep(timeout / 2)

        # Timer expired — auto-advance
        if channel_id not in active_mafia or active_mafia[channel_id] is not game:
            return
        phase = game["phase"]
        if phase == "day":
            await _auto_end_day(channel, game, channel_id, reason="⏰ Time's up — day auto-ended!")
        elif phase == "night":
            await _auto_resolve_night(channel, game, channel_id, reason="⏰ Time's up — night resolved automatically!")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[Mafia timer] Error: {e}")


async def _check_auto_advance(channel, game: dict, channel_id: int):
    """Check if all actions are done and auto-advance if so."""
    phase = game["phase"]
    if phase == "day":
        voted, total = _count_votes(game)
        if voted >= total and total > 0:
            await _auto_end_day(channel, game, channel_id, reason="✅ All players voted!")
    elif phase == "night":
        submitted, total = _count_night_actors(game)
        if submitted >= total and total > 0:
            await asyncio.sleep(2)  # brief dramatic pause
            await _auto_resolve_night(channel, game, channel_id, reason="✅ All roles submitted their actions!")


async def _auto_end_day(channel, game: dict, channel_id: int, reason: str = ""):
    """Auto-resolve day phase and move to night."""
    if channel_id not in active_mafia or active_mafia[channel_id] is not game:
        return
    if game["phase"] != "day":
        return

    _cancel_timer(channel_id)
    game["phase_deadline"] = None

    votes = game.get("votes", {})
    elim_msg = _resolve_votes(game, votes)

    winner = _mafia_win_check(game)
    if winner:
        msg = game.get("last_msg")
        await _mafia_end_channel(channel, game, winner, extra=f"{reason}\n{elim_msg}", msg=msg)
        return

    game["phase"]         = "night"
    game["night_actions"] = {}
    game["escort_blocks"] = {}
    game["voted_players"] = set()  # reset for next day

    full_msg = f"{reason}\n{elim_msg}\n\n**Night falls...** 🌙 Special roles — check your DMs!" if reason else elim_msg

    embed = _mafia_embed(game, title=f"🌙 Night {game['day']}", msg=full_msg)
    view  = MafiaNightView(game, channel_id)

    old_msg = game.get("last_msg")
    if old_msg:
        try:
            await old_msg.edit(embed=_mafia_embed(game, title=f"🌙 Night {game['day']} — see below"), view=None)
        except Exception:
            pass

    new_msg = await channel.send(embed=embed, view=view)
    game["last_msg"] = new_msg

    await _send_night_dms(game, channel_id, bot=channel._state._get_client())
    await _start_phase_timer(channel, game, channel_id)


async def _auto_resolve_night(channel, game: dict, channel_id: int, reason: str = ""):
    """Auto-resolve night phase and move to day."""
    if channel_id not in active_mafia or active_mafia[channel_id] is not game:
        return
    if game["phase"] != "night":
        return

    _cancel_timer(channel_id)
    game["phase_deadline"] = None

    msgs = _resolve_night_actions(game)
    if reason:
        msgs.insert(0, reason)

    winner = _mafia_win_check(game)
    if winner:
        old_msg = game.get("last_msg")
        await _mafia_end_channel(channel, game, winner, extra="\n".join(msgs), msg=old_msg)
        return

    game["phase"] = "day"
    game["day"]  += 1
    game["votes"] = {}
    game["voted_players"] = set()  # reset for new day

    full_msg = "\n".join(msgs) + "\n\n☀️ **Day begins!** Discuss and vote — auto-ends when everyone votes or in 30 seconds."
    embed = _mafia_embed(game, title=f"☀️ Day {game['day']} Begins", msg=full_msg)
    view  = MafiaDayView(game, channel_id)

    old_msg = game.get("last_msg")
    if old_msg:
        try:
            await old_msg.edit(embed=_mafia_embed(game, title=f"☀️ Day {game['day']} — see below"), view=None)
        except Exception:
            pass

    new_msg = await channel.send(embed=embed, view=view)
    game["last_msg"] = new_msg

    await _start_phase_timer(channel, game, channel_id)


def _resolve_votes(game: dict, votes: dict) -> str:
    """Tally votes and eliminate the most-voted player. Returns event string."""
    if not votes:
        return "☀️ No votes cast — no one was eliminated today."

    vote_counts: dict[int, int] = {}
    for val in votes.values():
        tid, wt = val if isinstance(val, tuple) else (val, 1)
        vote_counts[tid] = vote_counts.get(tid, 0) + wt

    max_votes = max(vote_counts.values())
    top = [uid for uid, cnt in vote_counts.items() if cnt == max_votes]

    if len(top) > 1:
        names = ", ".join(game["players"][uid]["member"].display_name for uid in top)
        return f"🤝 **Tie vote** between {names} — no one eliminated today."

    eliminated_id = top[0]
    p = game["players"][eliminated_id]
    p["alive"] = False
    elim_name = p["member"].display_name
    elim_role = p["role"]
    elim_emoji = MAFIA_ROLES[elim_role]
    will = p.get("last_will", "")
    will_str = f"\n📜 **Last Will:** _{will}_" if will else ""

    # Jester special win
    if elim_role == "jester":
        return (
            f"🃏 **{elim_name}** was eliminated by vote! They were the **Jester** — "
            f"and the Jester **wins** by getting voted out! 🎉{will_str}"
        )

    vote_tally = "\n".join(
        f"  {game['players'][vid]['member'].display_name}: **{cnt}** vote(s)"
        for vid, cnt in sorted(vote_counts.items(), key=lambda x: -x[1])
    )
    return (
        f"☠️ **{elim_name}** was eliminated by vote ({max_votes} votes)!\n"
        f"They were: {elim_emoji} **{elim_role.replace('serialkiller','Serial Killer').capitalize()}**"
        f"{will_str}\n\n**Vote tally:**\n{vote_tally}"
    )


def _resolve_night_actions(game: dict) -> list[str]:
    """Process all night actions. Returns list of public event strings."""
    actions  = game.get("night_actions", {})
    players  = game["players"]
    msgs     = []

    mafia_kill  = actions.get("mafia")
    doctor_save = actions.get("doctor")
    bg_protect  = actions.get("bodyguard")    # bodyguard target
    vig_kill    = actions.get("vigilante")
    sk_kill     = actions.get("serialkiller")
    escort_blocks = game.get("escort_blocks", {})

    # Escort blocks
    for blocker_id, target_id in escort_blocks.items():
        # Null out the blocked player's action
        for role_key, tid in list(actions.items()):
            if role_key not in ("mafia", "doctor", "bodyguard") and tid == target_id:
                # their role is blocked
                del actions[role_key]
                break

    # Bodyguard: redirect kill to themselves if they're protecting the target
    if mafia_kill and bg_protect == mafia_kill:
        # Bodyguard intercepts the mafia kill
        bg_id = next((uid for uid, v in players.items() if v["alive"] and v["role"] == "bodyguard"), None)
        if bg_id:
            players[bg_id]["alive"] = False
            bg_name = players[bg_id]["member"].display_name
            saved_name = players[mafia_kill]["member"].display_name
            will = players[bg_id].get("last_will", "")
            will_str = f"\n📜 **Last Will:** _{will}_" if will else ""
            msgs.append(
                f"🛡️ **{bg_name}** (Bodyguard) died protecting **{saved_name}** from the Mafia!{will_str}"
            )
            mafia_kill = None  # kill was intercepted

    # Mafia kill
    if mafia_kill:
        p = players.get(mafia_kill)
        if p and p["alive"]:
            if doctor_save == mafia_kill:
                msgs.append(f"💉 Someone was targeted last night... but the **Doctor** saved **{p['member'].display_name}**!")
            else:
                p["alive"] = False
                will = p.get("last_will", "")
                will_str = f"\n📜 **Last Will:** _{will}_" if will else ""
                role_emoji = MAFIA_ROLES[p["role"]]
                msgs.append(
                    f"☠️ **{p['member'].display_name}** was eliminated by the Mafia!\n"
                    f"They were: {role_emoji} **{p['role'].replace('serialkiller','Serial Killer').capitalize()}**{will_str}"
                )
                game["kill_log"].append(("mafia", mafia_kill, None))

    # Serial Killer kill (bypasses doctor)
    if sk_kill:
        p = players.get(sk_kill)
        if p and p["alive"]:
            p["alive"] = False
            will = p.get("last_will", "")
            will_str = f"\n📜 **Last Will:** _{will}_" if will else ""
            role_emoji = MAFIA_ROLES[p["role"]]
            msgs.append(
                f"🪓 **{p['member'].display_name}** was found dead — the work of the **Serial Killer**!\n"
                f"They were: {role_emoji} **{p['role'].replace('serialkiller','Serial Killer').capitalize()}**{will_str}"
            )

    # Vigilante kill
    if vig_kill:
        vig_id = next((uid for uid, v in players.items() if v["alive"] and v["role"] == "vigilante"), None)
        p = players.get(vig_kill)
        if p and p["alive"] and vig_id:
            target_role = p["role"]
            if ROLE_FACTION.get(target_role) == "village":
                # Guilt: vigilante dies next night
                vig_p = players[vig_id]
                vig_p["guilt"] = True
                will = p.get("last_will", "")
                will_str = f"\n📜 **Last Will:** _{will}_" if will else ""
                msgs.append(
                    f"🎯 **{p['member'].display_name}** was shot by the **Vigilante**!\n"
                    f"They were: {MAFIA_ROLES[target_role]} **{target_role.capitalize()}** — an innocent!\n"
                    f"😔 The Vigilante is overcome with guilt...{will_str}"
                )
                p["alive"] = False
            else:
                will = p.get("last_will", "")
                will_str = f"\n📜 **Last Will:** _{will}_" if will else ""
                msgs.append(
                    f"🎯 **{p['member'].display_name}** was shot by the **Vigilante**!\n"
                    f"They were: {MAFIA_ROLES[target_role]} **{target_role.replace('serialkiller','Serial Killer').capitalize()}**!{will_str}"
                )
                p["alive"] = False

    # Vigilante guilt death (from previous night)
    for uid, v in players.items():
        if v["alive"] and v["role"] == "vigilante" and v.get("guilt"):
            v["alive"] = False
            v["guilt"] = False
            will = v.get("last_will", "")
            will_str = f"\n📜 **Last Will:** _{will}_" if will else ""
            msgs.append(
                f"💔 **{v['member'].display_name}** (Vigilante) died of guilt for killing an innocent.{will_str}"
            )

    if not msgs:
        msgs.append("😴 The night was quiet — no one was eliminated.")

    game["night_actions"] = {}
    return msgs


# ── Night DM sender ─────────────────────────────────────────────────────────────

async def _send_night_dms(game: dict, channel_id: int, bot=None):
    """Send DM buttons to all special-role alive players."""
    for uid, v in game["players"].items():
        if not v["alive"]:
            continue
        role = v["role"]
        if role not in NIGHT_ACTION_ROLES:
            continue

        if role == "mafia":
            targets = [p["member"] for p in _mafia_alive(game)
                       if p["member"].id != uid and p["role"] != "mafia"]
            if not targets:
                continue
            view = MafiaNightActionView(game, channel_id, uid, "mafia", targets, bot=bot)
            e = discord.Embed(
                title="🌙 Night falls — Mafia action",
                description="Choose someone to **eliminate** tonight.",
                color=discord.Color.dark_red(),
            )
            e.add_field(name="🎯 Targets", value="\n".join(f"• {m.display_name}" for m in targets))
            e.set_footer(text=f"Night {game['day']} • ⏱️ 30 seconds to act • Private")
        elif role == "detective":
            targets = [p["member"] for p in _mafia_alive(game) if p["member"].id != uid]
            if not targets:
                continue
            view = MafiaNightActionView(game, channel_id, uid, "detective", targets, bot=bot)
            e = discord.Embed(
                title="🌙 Night falls — Detective action",
                description="Choose someone to **investigate**. You'll learn if they're Mafia.",
                color=discord.Color.blue(),
            )
            e.add_field(name="🔍 Suspects", value="\n".join(f"• {m.display_name}" for m in targets))
            e.set_footer(text=f"Night {game['day']} • ⏱️ 30 seconds to act • Result private")
        elif role == "doctor":
            targets = [p["member"] for p in _mafia_alive(game)]
            if not targets:
                continue
            view = MafiaNightActionView(game, channel_id, uid, "doctor", targets, bot=bot)
            e = discord.Embed(
                title="🌙 Night falls — Doctor action",
                description="Choose someone to **protect** from the Mafia tonight.",
                color=discord.Color.green(),
            )
            e.add_field(name="💉 Players", value="\n".join(f"• {m.display_name}" for m in targets))
            e.set_footer(text=f"Night {game['day']} • ⏱️ 30 seconds to act • Private")
        elif role == "vigilante":
            vig_used = game.get("vig_used", False)
            if vig_used:
                try:
                    await v["member"].send("🎯 You've already used your one-shot kill. Stay hidden tonight.")
                except Exception:
                    pass
                continue
            targets = [p["member"] for p in _mafia_alive(game) if p["member"].id != uid]
            if not targets:
                continue
            view = MafiaNightActionView(game, channel_id, uid, "vigilante", targets, bot=bot)
            e = discord.Embed(
                title="🌙 Night falls — Vigilante action",
                description="**One-shot kill**: choose someone to shoot tonight. Killing an innocent will cause your death next night.",
                color=discord.Color.orange(),
            )
            e.add_field(name="🎯 Targets", value="\n".join(f"• {m.display_name}" for m in targets))
            e.set_footer(text=f"Night {game['day']} • ⏱️ 30 seconds • ONE shot only")
        elif role == "serialkiller":
            targets = [p["member"] for p in _mafia_alive(game) if p["member"].id != uid]
            if not targets:
                continue
            view = MafiaNightActionView(game, channel_id, uid, "serialkiller", targets, bot=bot)
            e = discord.Embed(
                title="🌙 Night falls — Serial Killer action",
                description="Choose someone to **kill** tonight. You bypass all protections.",
                color=discord.Color.dark_red(),
            )
            e.add_field(name="🪓 Targets", value="\n".join(f"• {m.display_name}" for m in targets))
            e.set_footer(text=f"Night {game['day']} • ⏱️ 30 seconds • Bypasses Doctor")
        elif role == "bodyguard":
            targets = [p["member"] for p in _mafia_alive(game)]
            if not targets:
                continue
            view = MafiaNightActionView(game, channel_id, uid, "bodyguard", targets, bot=bot)
            e = discord.Embed(
                title="🌙 Night falls — Bodyguard action",
                description="Choose someone to **protect**. If they're attacked, you die instead.",
                color=discord.Color.teal(),
            )
            e.add_field(name="🛡️ Players", value="\n".join(f"• {m.display_name}" for m in targets))
            e.set_footer(text=f"Night {game['day']} • ⏱️ 30 seconds • You die if they're attacked")
        elif role == "spy":
            # Spy doesn't need to pick — they auto-see visits. Send info from last night or skip.
            # Mark spy as "submitted" automatically
            game["night_actions"]["spy"] = uid  # dummy value means spy submitted
            try:
                last_visits = game.get("spy_last_visits", [])
                if last_visits:
                    visited_names = "\n".join(f"• {name}" for name in last_visits)
                    await v["member"].send(
                        embed=discord.Embed(
                            title=f"🕵️ Spy Report — Night {game['day']}",
                            description=f"The Mafia visited:\n{visited_names}\n\n_(You can't stop them — but you know!)_",
                            color=discord.Color.blurple(),
                        )
                    )
                else:
                    await v["member"].send(
                        embed=discord.Embed(
                            title=f"🕵️ Spy Report — Night {game['day']}",
                            description="The Mafia didn't visit anyone last night, or this is Night 1.",
                            color=discord.Color.blurple(),
                        )
                    )
            except Exception:
                pass
            continue
        elif role == "escort":
            targets = [p["member"] for p in _mafia_alive(game) if p["member"].id != uid]
            if not targets:
                continue
            view = MafiaNightActionView(game, channel_id, uid, "escort", targets, bot=bot)
            e = discord.Embed(
                title="🌙 Night falls — Escort action",
                description="Choose someone to **distract** tonight. They can't use their ability.",
                color=discord.Color.magenta(),
            )
            e.add_field(name="💃 Players", value="\n".join(f"• {m.display_name}" for m in targets))
            e.set_footer(text=f"Night {game['day']} • ⏱️ 30 seconds to act • Private")
        else:
            continue

        try:
            await v["member"].send(embed=e, view=view)
        except discord.Forbidden:
            # Can't DM — auto-skip their action
            game["night_actions"][role] = None


# ── Night action DM view ────────────────────────────────────────────────────────

class MafiaNightActionView(discord.ui.View):
    """Sent via DM; player picks a target privately."""

    def __init__(self, game: dict, channel_id: int, actor_id: int, role: str, targets: list, bot=None):
        super().__init__(timeout=NIGHT_TIMEOUT)
        self.game       = game
        self.channel_id = channel_id
        self.actor_id   = actor_id
        self.role       = role
        self._bot       = bot   # stored so on_timeout can trigger auto-resolve
        self._submitted = False
        day = game.get("day", 1)

        action_labels = {
            "mafia":        "☠️ Eliminate",
            "detective":    "🔍 Investigate",
            "doctor":       "💉 Protect",
            "vigilante":    "🎯 Shoot",
            "serialkiller": "🪓 Kill",
            "bodyguard":    "🛡️ Protect",
            "escort":       "💃 Distract",
        }
        action_label = action_labels.get(role, "Choose")

        for member in targets[:20]:  # Discord limits
            btn = discord.ui.Button(
                label=f"{action_label}  {member.display_name}",
                style=discord.ButtonStyle.danger if role in ("mafia", "serialkiller", "vigilante") else discord.ButtonStyle.primary,
                # Include day number so each night's buttons have unique IDs
                custom_id=f"mafnight_{day}_{member.id}_{role}_{actor_id}",
            )
            btn.callback = self._make_callback(member)
            self.add_item(btn)

    def _make_callback(self, target: discord.Member):
        async def callback(interaction: discord.Interaction):
            # ── Defer IMMEDIATELY — Discord only gives 3 seconds before showing
            #    "Interaction Failed". Deferring buys unlimited time for async work.
            try:
                await interaction.response.defer()
            except Exception:
                pass  # already responded — still safe to continue

            if interaction.user.id != self.actor_id:
                await interaction.followup.send("❌ This isn't your action!", ephemeral=True)
                return

            game = active_mafia.get(self.channel_id)
            if not game or game["phase"] != "night":
                await interaction.followup.send("⚠️ The night phase has already ended.", ephemeral=True)
                return

            if self._submitted:
                await interaction.followup.send("⚠️ You already submitted your action for this night.", ephemeral=True)
                return

            p = game["players"].get(target.id)
            if not p or not p["alive"]:
                await interaction.followup.send(f"❌ **{target.display_name}** is no longer alive.", ephemeral=True)
                return

            self._submitted = True
            role = self.role
            if role == "mafia":
                game["night_actions"]["mafia"] = target.id
                game.setdefault("spy_last_visits", []).clear()
                game["spy_last_visits"].append(target.display_name)
            elif role == "vigilante":
                game["night_actions"]["vigilante"] = target.id
                game["vig_used"] = True
            elif role == "escort":
                game["night_actions"]["escort"] = target.id
                game.setdefault("escort_blocks", {})[self.actor_id] = target.id
            else:
                game["night_actions"][role] = target.id

            self.stop()
            for item in self.children:
                item.disabled = True

            # Build feedback content
            if role == "detective":
                is_evil = game["players"][target.id]["role"] in ("mafia", "serialkiller")
                result = "🔫 **EVIL**" if is_evil else "✅ **NOT Evil**"
                content = f"🔍 **Investigation result:** {target.display_name} is {result}."
            elif role == "escort":
                content = f"💃 You distracted **{target.display_name}** — their ability is blocked tonight."
            else:
                action_past = {
                    "mafia": "targeted", "doctor": "protected",
                    "vigilante": "shot", "serialkiller": "attacked",
                    "bodyguard": "stationed yourself to protect",
                }.get(role, "acted on")
                content = f"✅ You {action_past} **{target.display_name}** tonight."

            # Edit the original DM to show the result with buttons disabled
            try:
                await interaction.edit_original_response(content=content, view=self)
            except Exception:
                await interaction.followup.send(content, ephemeral=True)

            # Check if all actions are in — auto-advance if so
            channel = interaction.client.get_channel(self.channel_id)
            if channel:
                await _check_auto_advance(channel, game, self.channel_id)

        return callback

    async def on_timeout(self):
        """Auto-submit a null action AND trigger night resolution so AFK players don't stall the game."""
        if self._submitted:
            return
        self._submitted = True
        game = active_mafia.get(self.channel_id)
        if not game or game["phase"] != "night":
            return
        role = self.role
        # Register a null action so _count_night_actors counts this player as done
        if role == "mafia" and "mafia" not in game["night_actions"]:
            game["night_actions"]["mafia"] = None
        elif role not in game["night_actions"]:
            game["night_actions"][role] = None
        # Disable buttons
        for item in self.children:
            item.disabled = True
        # ── KEY FIX: use the stored bot reference to trigger auto-advance ──────
        bot = self._bot
        if bot:
            channel = bot.get_channel(self.channel_id)
            if channel:
                asyncio.get_event_loop().create_task(
                    _check_auto_advance(channel, game, self.channel_id)
                )


# ── Day view ────────────────────────────────────────────────────────────────────

class MafiaDayView(discord.ui.View):
    """Day phase: vote, mayor reveal, last will. All auto — no force buttons."""

    def __init__(self, game: dict, channel_id: int):
        super().__init__(timeout=DAY_TIMEOUT + 10)
        self.game       = game
        self.channel_id = channel_id

    @discord.ui.button(label="🗳️ Vote", style=discord.ButtonStyle.danger, row=0)
    async def vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.game
        uid  = interaction.user.id
        if uid not in game["players"] or not game["players"][uid]["alive"]:
            await interaction.response.send_message("❌ You're not an alive player.", ephemeral=True)
            return

        alive_options = [
            discord.SelectOption(
                label=v["member"].display_name,
                value=str(v["member"].id),
                description="(Mayor — 3 votes)" if v.get("revealed") else "",
            )
            for v in _mafia_alive(game) if v["member"].id != uid
        ]
        alive_options.append(
            discord.SelectOption(label="🤷 Abstain", value="abstain", description="Skip voting this round")
        )

        select = discord.ui.Select(placeholder="Vote to eliminate…", options=alive_options)

        async def on_select(inter: discord.Interaction):
            # Defer immediately — prevents "Interaction Failed" while we process
            await inter.response.defer()

            chosen = inter.data["values"][0]
            # Mark this player as having voted (regardless of abstain)
            game.setdefault("voted_players", set()).add(uid)

            if chosen == "abstain":
                game["votes"].pop(uid, None)
                voted_count, alive_count = _count_votes(game)
                await inter.followup.send(
                    f"🤷 **{inter.user.display_name}** abstained. ({voted_count}/{alive_count} voted)",
                    ephemeral=False
                )
            else:
                target_id = int(chosen)
                vote_weight = 3 if game["players"][uid].get("revealed") else 1
                game["votes"][uid] = (target_id, vote_weight)
                target_name = game["players"][target_id]["member"].display_name

                tally_counts: dict[int, int] = {}
                for val in game["votes"].values():
                    tid2, wt = val if isinstance(val, tuple) else (val, 1)
                    tally_counts[tid2] = tally_counts.get(tid2, 0) + wt

                tally = "\n".join(
                    f"{game['players'][vid]['member'].display_name}: **{cnt}** vote{'s' if cnt != 1 else ''}"
                    for vid, cnt in sorted(tally_counts.items(), key=lambda x: -x[1])
                )
                voted_count, alive_count = _count_votes(game)
                await inter.followup.send(
                    f"🗳️ **{inter.user.display_name}** voted for **{target_name}**!\n"
                    f"**Votes ({voted_count}/{alive_count}):**\n{tally}",
                    ephemeral=False
                )

            # Auto-advance check
            await _check_auto_advance(inter.channel, game, self.channel_id)

        select.callback = on_select
        v = discord.ui.View(timeout=30)
        v.add_item(select)
        await interaction.response.send_message("Who do you vote to eliminate?", view=v, ephemeral=True)

    @discord.ui.button(label="🎖️ Mayor Reveal", style=discord.ButtonStyle.secondary, row=0)
    async def mayor_reveal(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.game
        uid  = interaction.user.id
        p    = game["players"].get(uid)
        if not p or not p["alive"]:
            await interaction.response.send_message("❌ You're not alive.", ephemeral=True)
            return
        if p["role"] != "mayor":
            await interaction.response.send_message("❌ Only the Mayor can use this.", ephemeral=True)
            return
        if p.get("revealed"):
            await interaction.response.send_message("⚠️ You already revealed yourself.", ephemeral=True)
            return
        p["revealed"] = True
        await interaction.response.send_message(
            f"🎖️ **{interaction.user.display_name}** reveals themselves as the **Mayor**! "
            f"Their vote now counts as **3**! ⚠️ They're now a prime target!",
            ephemeral=False
        )

    @discord.ui.button(label="✍️ Last Will", style=discord.ButtonStyle.secondary, row=0)
    async def last_will(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        if uid not in self.game["players"]:
            await interaction.response.send_message("❌ You're not in this game.", ephemeral=True)
            return
        await interaction.response.send_modal(LastWillModal(self.game, uid))

    async def on_timeout(self):
        pass


# ── Night view (channel) ────────────────────────────────────────────────────────

class MafiaNightView(discord.ui.View):
    """Night phase channel embed — purely informational, all auto-resolved."""

    def __init__(self, game: dict, channel_id: int):
        super().__init__(timeout=NIGHT_TIMEOUT + 10)
        self.game       = game
        self.channel_id = channel_id

    async def on_timeout(self):
        pass


# ── End game ────────────────────────────────────────────────────────────────────

async def _mafia_end_channel(channel, game: dict, winner: str, extra: str = "", msg=None):
    """End the game from a channel context (no interaction)."""
    channel_id = channel.id
    _cancel_timer(channel_id)
    if channel_id in active_mafia:
        del active_mafia[channel_id]

    embed = _build_end_embed(game, winner, extra)

    if msg:
        try:
            await msg.edit(content="🏁 Game over — see below.", embed=None, view=None)
        except Exception:
            pass
    await channel.send(embed=embed)


async def _mafia_end(interaction: discord.Interaction, game: dict, winner: str, extra=""):
    """End the game from an interaction context."""
    cid = interaction.channel_id
    _cancel_timer(cid)
    if cid in active_mafia:
        del active_mafia[cid]

    embed = _build_end_embed(game, winner, extra)
    try:
        await interaction.response.edit_message(content="🏁 Game over — see below.", embed=None, view=None)
    except Exception:
        pass
    await interaction.channel.send(embed=embed)


def _build_end_embed(game: dict, winner: str, extra: str = "") -> discord.Embed:
    if winner == "village":
        title = "🎉 Village Wins!"
        msg   = "The Mafia has been eliminated. Peace is restored to the town!"
        color = discord.Color.green()
    elif winner == "mafia":
        title = "💀 Mafia Wins!"
        msg   = "The Mafia has taken over the town! Evil prevails..."
        color = discord.Color.dark_red()
    elif winner == "jester":
        title = "🃏 Jester Wins!"
        msg   = "The Jester was voted out and wins alone!"
        color = discord.Color.purple()
    elif winner == "serialkiller":
        title = "🪓 Serial Killer Wins!"
        msg   = "The Serial Killer outlasted everyone and stands alone!"
        color = discord.Color.dark_orange()
    else:
        title = "🎭 Game Over"
        msg   = ""
        color = discord.Color.greyple()

    # Full role reveal
    reveal_lines = []
    for v in game["players"].values():
        role_emoji = MAFIA_ROLES.get(v["role"], "❓")
        status     = "💀" if not v["alive"] else "✅"
        will       = v.get("last_will", "")
        line       = f"{status} {role_emoji} **{v['member'].display_name}** — {v['role'].replace('serialkiller','Serial Killer').capitalize()}"
        if will:
            line += f"\n  📜 _{will}_"
        reveal_lines.append(line)

    # Kill log summary
    kill_summary = ""
    kill_log = game.get("kill_log", [])
    if kill_log:
        kill_summary = "\n".join(
            f"• {killer_role.capitalize()} → {game['players'][vic]['member'].display_name}"
            for killer_role, vic, _ in kill_log
            if vic in game["players"]
        )

    embed = discord.Embed(title=title, description=msg, color=color)
    if extra:
        # Truncate if needed
        embed.add_field(name="📋 Last Event", value=extra[:1020], inline=False)
    embed.add_field(name="🎭 Full Role Reveal", value="\n".join(reveal_lines)[:1020], inline=False)
    if kill_summary:
        embed.add_field(name="📊 Kill Log", value=kill_summary[:512], inline=False)

    days_played = game.get("day", 1)
    mode_label  = {"classic": "Classic", "extended": "Extended", "chaos": "Chaos"}.get(game.get("mode", "classic"), "")
    embed.set_footer(text=f"{mode_label} • {days_played} day(s) played • {len(game['players'])} players")
    return embed


# ── Guide embed ─────────────────────────────────────────────────────────────────

def _mafia_guide_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎭 How to Play Mafia — Upgraded Edition",
        color=discord.Color.dark_gold(),
    )
    embed.add_field(name="🎯 Goals", value=(
        "**Village** — Find and eliminate all evil roles.\n"
        "**Mafia** — Outnumber the remaining village.\n"
        "**Jester** — Get voted out by the village!\n"
        "**Serial Killer** — Be the last one standing."
    ), inline=False)
    embed.add_field(name="👥 Roles", value=(
        "🔫 **Mafia** — Eliminate one player each night.\n"
        "🔍 **Detective** — Investigate a player each night.\n"
        "💉 **Doctor** — Protect a player from death each night.\n"
        "🏘️ **Villager** — No power. Vote wisely!\n"
        "🎯 **Vigilante** *(Extended)* — One-shot kill. Kills innocent = you die next night.\n"
        "🎖️ **Mayor** *(Extended)* — Reveal publicly → your vote counts x3.\n"
        "🛡️ **Bodyguard** *(Extended)* — Protect someone; you die if they're attacked.\n"
        "🕵️ **Spy** *(Extended)* — See who the Mafia visits each night.\n"
        "💃 **Escort** *(Extended)* — Block a player's night action.\n"
        "🃏 **Jester** *(Extended)* — Win by getting voted out!\n"
        "🪓 **Serial Killer** *(Chaos)* — Kill each night, bypasses Doctor. Win alone."
    ), inline=False)
    embed.add_field(name="☀️ Day Phase", value=(
        "• Discuss, accuse, and argue!\n"
        "• Click **🗳️ Vote** to select your target.\n"
        "• Day **auto-ends** when everyone votes, or when the timer expires.\n"
        "• Tie vote = no elimination.\n"
        "• **Mayor** can click **🎖️ Mayor Reveal** to triple their vote.\n"
        "• Write your **✍️ Last Will** — revealed when you die!"
    ), inline=False)
    embed.add_field(name="🌙 Night Phase", value=(
        "• Special roles get a **DM with buttons** — pick your target.\n"
        "• Night **auto-resolves** when all roles submit, or timer expires.\n"
        "• **Escort** blocks the target's ability.\n"
        "• **Bodyguard** dies in place of their protect target.\n"
        "• **Serial Killer** bypasses Doctor saves."
    ), inline=False)
    embed.add_field(name="🚀 Starting a Game", value=(
        "1. Run `/mafia` or `!mafia` to open a lobby.\n"
        "2. Everyone clicks **🙋 Join Game**.\n"
        "3. Host picks a **game mode** from the dropdown.\n"
        "4. Host clicks **🚀 Start** (needs 4+ players).\n"
        "5. Roles are DM'd — keep yours secret!"
    ), inline=False)
    embed.add_field(name="📜 Other Commands", value=(
        "`!stopmafia` — Stop the current game\n"
        "`!mafvote @user` — Vote via text command\n"
        "`!lastwill <text>` — Set your last will\n"
        "`!mafguide` — Show this guide"
    ), inline=False)
    embed.set_footer(text="Roles scale with player count • Min 4 players • Auto-timers keep the game moving!")
    return embed


# ═══════════════════════════════════════════════════════════════════════════════
#  MAFIA COG COMMANDS (inserted into Fun class below)
# ═══════════════════════════════════════════════════════════════════════════════

# These are defined as methods and mixed into the Fun cog class.
# They are marked with a sentinel comment so the diff is clear.

class Fun(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        """Called by discord.py after the cog is fully loaded — safe to await here."""
        await _count_init_db()

    async def cog_unload(self) -> None:
        """Force-flush all pending counting saves before unload/shutdown."""
        for key in list(_count_save_tasks):
            task = _count_save_tasks.get(key)
            if task and not task.done():
                task.cancel()
        for guild_id_str in list(_count_data):
            try:
                _count_persist(int(guild_id_str))
            except Exception:
                pass

    # ── Hangman ───────────────────────────────────────────────────────────────

    @app_commands.command(name="hangman", description="Start a game of hangman!")
    async def slash_hangman(self, interaction: discord.Interaction):
        if interaction.channel_id in active_hangman:
            await interaction.response.send_message("⚠️ A hangman game is already running here!", ephemeral=True)
            return
        word  = random.choice(HANGMAN_WORDS)
        state = {"word": word, "guessed": set(), "wrong": 0, "host": interaction.user.id}
        active_hangman[interaction.channel_id] = state
        await interaction.response.send_message(embed=_hangman_embed(state))

    @commands.command(name="hangman")
    async def prefix_hangman(self, ctx: commands.Context):
        if ctx.channel.id in active_hangman:
            await ctx.reply("⚠️ A hangman game is already running here!")
            return
        word  = random.choice(HANGMAN_WORDS)
        state = {"word": word, "guessed": set(), "wrong": 0, "host": ctx.author.id}
        active_hangman[ctx.channel.id] = state
        await ctx.reply(embed=_hangman_embed(state))

    @app_commands.command(name="stophangman", description="Stop the current hangman game")
    async def slash_stophangman(self, interaction: discord.Interaction):
        if interaction.channel_id not in active_hangman:
            await interaction.response.send_message("⚠️ No hangman game is running here.", ephemeral=True)
            return
        state = active_hangman.pop(interaction.channel_id)
        await interaction.response.send_message(f"🛑 Hangman stopped. The word was **{state['word']}**.")

    # ── Chess ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="chess", description="Challenge someone to a game of chess ♟️")
    @app_commands.describe(opponent="The user you want to play against")
    async def slash_chess(self, interaction: discord.Interaction, opponent: discord.Member):
        await self._send_chess_challenge(interaction.channel, interaction.user, opponent,
                                         reply_fn=lambda **kw: interaction.response.send_message(**kw),
                                         interaction=interaction)

    @commands.command(name="chess")
    async def prefix_chess(self, ctx: commands.Context, opponent: discord.Member = None):
        if not opponent:
            await ctx.reply("Usage: `!chess @user`")
            return
        await self._send_chess_challenge(ctx.channel, ctx.author, opponent,
                                         reply_fn=lambda **kw: ctx.reply(**kw))

    async def _send_chess_challenge(self, channel, challenger, opponent, reply_fn, interaction=None):
        if channel.id in active_chess:
            await reply_fn(content="⚠️ A chess game is already running in this channel.", ephemeral=True)
            return
        if opponent.bot:
            await reply_fn(content="❌ You can't play chess against a bot.", ephemeral=True)
            return
        if opponent.id == challenger.id:
            await reply_fn(content="❌ You can't play against yourself.", ephemeral=True)
            return
        view = ChessChallengeView(challenger, opponent, channel)
        result = await reply_fn(
            content=f"♟️ {challenger.mention} challenges {opponent.mention} to a game of Chess!\n"
                    f"{opponent.mention} — do you accept?",
            view=view,
        )
        if result is not None:
            view.message = result
        elif interaction is not None:
            try:
                view.message = await interaction.original_response()
            except discord.HTTPException:
                pass

    
    @commands.command(name="move")
    async def prefix_move(self, ctx: commands.Context, move: str = None):
        if not move:
            await ctx.reply("Usage: `!move e2e4`")
            return
        game = active_chess.get(ctx.channel.id)
        if not game:
            await ctx.reply("❌ No chess game running here.")
            return
        board      = game["board"]
        whose_turn = game["white"] if board.turn == _chess.WHITE else game["black"]
        if ctx.author.id != whose_turn.id:
            await ctx.reply(f"❌ It's **{whose_turn.display_name}'s** turn.")
            return
        try:
            chess_move = _chess.Move.from_uci(move.lower())
            if chess_move not in board.legal_moves:
                raise ValueError()
        except Exception:
            try:
                chess_move = board.parse_san(move)
            except Exception:
                await ctx.reply(f"❌ Invalid move `{move}`.")
                return
        board.push(chess_move)
        game["draw_offered_by"] = None
        game["undo_offered_by"] = None
        game.setdefault("move_history", []).append(chess_move.uci())
        game["hints_used"] = {}
        if board.is_checkmate():
            winner = game["black"] if board.turn == _chess.WHITE else game["white"]
            del active_chess[ctx.channel.id]
            await ctx.reply(content=f"🏆 **{winner.display_name}** wins!",
                            embed=_chess_embed(game, title="♟️ Checkmate!"))
            return
        if board.is_stalemate() or board.is_insufficient_material():
            del active_chess[ctx.channel.id]
            await ctx.reply(content="🤝 Draw!", embed=_chess_embed(game, title="♟️ Draw!"))
            return
        next_player = game["white"] if board.turn == _chess.WHITE else game["black"]
        await ctx.reply(content=f"{next_player.mention} your turn!",
                        embed=_chess_embed(game, msg=f"Move `{chess_move.uci()}` played."),
                        file=_chess_file(game),
                        view=ChessPieceSelect(game, next_player))

    @app_commands.command(name="resign", description="Resign your current chess game")
    async def slash_resign(self, interaction: discord.Interaction):
        await self._handle_resign(interaction.channel, interaction.user,
                                   reply_fn=lambda **kw: interaction.response.send_message(**kw))

    @commands.command(name="resign")
    async def prefix_resign(self, ctx: commands.Context):
        await self._handle_resign(ctx.channel, ctx.author, reply_fn=lambda **kw: ctx.reply(**kw))

    async def _handle_resign(self, channel, user, reply_fn):
        game = active_chess.get(channel.id)
        if not game:
            await reply_fn(content="❌ No chess game running here.", ephemeral=True); return
        if user.id not in (game["white"].id, game["black"].id):
            await reply_fn(content="❌ You're not in this game.", ephemeral=True); return
        winner = game["black"] if user.id == game["white"].id else game["white"]
        del active_chess[channel.id]
        await reply_fn(content=f"🏳️ **{user.display_name}** resigned. **{winner.display_name}** wins!",
                       embed=_chess_embed(game, title="♟️ Resignation"), file=_chess_file(game))

    @app_commands.command(name="draw", description="Offer or accept a draw in chess")
    async def slash_draw(self, interaction: discord.Interaction):
        await self._handle_draw(interaction.channel, interaction.user,
                                 reply_fn=lambda **kw: interaction.response.send_message(**kw))

    @commands.command(name="draw")
    async def prefix_draw(self, ctx: commands.Context):
        await self._handle_draw(ctx.channel, ctx.author, reply_fn=lambda **kw: ctx.reply(**kw))

    async def _handle_draw(self, channel, user, reply_fn):
        game = active_chess.get(channel.id)
        if not game:
            await reply_fn(content="❌ No chess game running here.", ephemeral=True); return
        if user.id not in (game["white"].id, game["black"].id):
            await reply_fn(content="❌ You're not in this game.", ephemeral=True); return
        offered = game["draw_offered_by"]
        if offered is None:
            game["draw_offered_by"] = user.id
            opponent = game["black"] if user.id == game["white"].id else game["white"]
            await reply_fn(content=f"🤝 **{user.display_name}** offers a draw. {opponent.mention} type `!draw` to accept.")
            return
        if offered == user.id:
            await reply_fn(content="⏳ You already offered a draw.", ephemeral=True); return
        del active_chess[channel.id]
        await reply_fn(content="🤝 Both players agreed to a draw!",
                       embed=_chess_embed(game, title="♟️ Draw Agreed"), file=_chess_file(game))

    @app_commands.command(name="undo", description="Request to undo the last chess move (needs opponent's agreement)")
    async def slash_undo(self, interaction: discord.Interaction):
        await self._handle_undo(interaction.channel, interaction.user,
                                 reply_fn=lambda **kw: interaction.response.send_message(**kw))

    @commands.command(name="undo")
    async def prefix_undo(self, ctx: commands.Context):
        await self._handle_undo(ctx.channel, ctx.author, reply_fn=lambda **kw: ctx.reply(**kw))

    async def _handle_undo(self, channel, user, reply_fn):
        game = active_chess.get(channel.id)
        if not game:
            await reply_fn(content="❌ No chess game running here.", ephemeral=True); return
        if user.id not in (game["white"].id, game["black"].id):
            await reply_fn(content="❌ You're not in this game.", ephemeral=True); return
        if not game.get("move_history"):
            await reply_fn(content="❌ No moves to undo yet.", ephemeral=True); return
        if game.get("undo_offered_by"):
            await reply_fn(content="⏳ An undo request is already pending.", ephemeral=True); return

        opponent = game["black"] if user.id == game["white"].id else game["white"]
        game["undo_offered_by"] = user.id
        view = ChessUndoView(game, channel.id, user, opponent)
        await reply_fn(
            content=(
                f"⏪ **{user.display_name}** wants to undo the last move "
                f"(`{game['move_history'][-1]}`). {opponent.mention}, do you agree?"
            ),
            view=view,
        )

    @app_commands.command(name="hint", description="Get an AI hint for your chess position")
    async def slash_hint(self, interaction: discord.Interaction):
        await self._handle_hint(interaction.channel, interaction.user,
                                 reply_fn=lambda **kw: interaction.followup.send(**kw),
                                 defer_fn=lambda: interaction.response.defer(ephemeral=True))

    @commands.command(name="hint")
    async def prefix_hint(self, ctx: commands.Context):
        await self._handle_hint(ctx.channel, ctx.author,
                                 reply_fn=lambda **kw: ctx.reply(**kw),
                                 defer_fn=ctx.typing)

    async def _handle_hint(self, channel, user, reply_fn, defer_fn):
        from cogs.economy import SpendCreditsView, EXTRA_HINT_COST, JC_EMOJI, JC_NAME
        from cogs.state import get_credits

        game = active_chess.get(channel.id)
        if not game:
            await reply_fn(content="❌ No chess game running here.", ephemeral=True); return
        whose_turn = game["white"] if game["board"].turn == _chess.WHITE else game["black"]
        if user.id != whose_turn.id:
            await reply_fn(content="❌ It's not your turn.", ephemeral=True); return

        hints_used = game.setdefault("hints_used", {})
        used = hints_used.get(str(user.id), 0)

        if used >= 1:
            # Free hint already used this turn — offer to spend JC for another.
            balance = get_credits(user.id)

            async def on_confirm(interaction: discord.Interaction, view: SpendCreditsView):
                hint = await self._generate_chess_hint(game, channel, user)
                hints_used[str(user.id)] = used + 1
                embed = discord.Embed(
                    title="💡 Chess Hint",
                    description=f"{JC_EMOJI} **{EXTRA_HINT_COST} {JC_NAME}** spent.\n\n{hint}",
                    color=discord.Color.blue(),
                )
                await interaction.response.edit_message(embed=embed, view=view)

            async def on_decline(interaction: discord.Interaction, view: SpendCreditsView, reason: str):
                if reason == "insufficient":
                    text = f"❌ Not enough {JC_EMOJI} {JC_NAME}s (you have **{balance}**, need **{EXTRA_HINT_COST}**)."
                else:
                    text = "❌ No extra hint this turn."
                await interaction.response.edit_message(content=text, embed=None, view=view)

            view = SpendCreditsView(user.id, EXTRA_HINT_COST, on_confirm, on_decline, timeout=30)
            embed = discord.Embed(
                description=(
                    f"You've already used your free hint for this turn.\n"
                    f"Spend **{EXTRA_HINT_COST}** {JC_EMOJI} {JC_NAME} for another hint? "
                    f"(you have **{balance}** {JC_EMOJI})"
                ),
                color=discord.Color.orange(),
            )
            msg = await reply_fn(embed=embed, view=view)
            view.message = msg
            return

        async with channel.typing():
            hint = await self._generate_chess_hint(game, channel, user)
        hints_used[str(user.id)] = used + 1
        await reply_fn(content=f"💡 **Chess Hint:**\n{hint}")

    async def _generate_chess_hint(self, game: dict, channel, user) -> str:
        board  = game["board"]
        legal  = [m.uci() for m in list(board.legal_moves)[:20]]
        colour = "White" if board.turn == _chess.WHITE else "Black"
        prompt = (f"You are a chess coach. FEN: {board.fen()}\n"
                  f"It is {colour}'s turn. Legal moves: {', '.join(legal)}.\n"
                  f"Suggest the best move and explain why in 2-3 sentences.")
        return await generate_ai_response(user.id, prompt, channel.guild.id if channel.guild else None)

    @app_commands.command(name="stopchess", description="Stop the current chess game")
    async def slash_stopchess(self, interaction: discord.Interaction):
        await self._handle_stopchess(interaction.channel, interaction.user,
                                      reply_fn=lambda **kw: interaction.response.send_message(**kw))

    @commands.command(name="stopchess")
    async def prefix_stopchess(self, ctx: commands.Context):
        await self._handle_stopchess(ctx.channel, ctx.author, reply_fn=lambda **kw: ctx.reply(**kw))

    async def _handle_stopchess(self, channel, user, reply_fn):
        game = active_chess.get(channel.id)
        if not game:
            await reply_fn(content="❌ No chess game running here.", ephemeral=True); return
        is_player = user.id in (game["white"].id, game["black"].id)
        is_admin  = channel.permissions_for(user).manage_messages
        if not (is_player or is_admin):
            await reply_fn(content="❌ Only players or admins can stop the game.", ephemeral=True); return
        del active_chess[channel.id]
        await reply_fn(content="🛑 Chess game stopped.")

    @app_commands.command(name="chessboard", description="Show the current chess board")
    async def slash_chessboard(self, interaction: discord.Interaction):
        game = active_chess.get(interaction.channel_id)
        if not game:
            await interaction.response.send_message("❌ No chess game running here.", ephemeral=True); return
        await interaction.response.send_message(embed=_chess_embed(game), file=_chess_file(game))

    @commands.command(name="chessboard", aliases=["board"])
    async def prefix_chessboard(self, ctx: commands.Context):
        game = active_chess.get(ctx.channel.id)
        if not game:
            await ctx.reply("❌ No chess game running here."); return
        whose_turn = game["white"] if game["board"].turn == _chess.WHITE else game["black"]
        await ctx.reply(embed=_chess_embed(game), file=_chess_file(game),
                        view=ChessPieceSelect(game, whose_turn))

    # ── Mafia ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="mafia", description="Start a Mafia game in this channel 🎭")
    async def slash_mafia(self, interaction: discord.Interaction):
        await self._start_mafia(interaction.channel, interaction.user,
                                reply_fn=lambda **kw: interaction.response.send_message(**kw))

    @commands.command(name="mafia")
    async def prefix_mafia(self, ctx: commands.Context):
        await self._start_mafia(ctx.channel, ctx.author, reply_fn=lambda **kw: ctx.reply(**kw))

    async def _start_mafia(self, channel, host, reply_fn):
        if channel.id in active_mafia:
            await reply_fn(content="⚠️ A Mafia game is already running here!", ephemeral=True)
            return
        game = {
            "phase":         "lobby",
            "players":       {
                host.id: {
                    "member": host, "role": None, "alive": True,
                    "last_will": "", "revealed": False, "guilt": False, "protected_by": None,
                }
            },
            "day":           1,
            "votes":         {},
            "night_actions": {},
            "kill_log":      [],
            "escort_blocks": {},
            "spy_last_visits": [],
            "vig_used":      False,
            "mode":          "classic",
            "voted_players": set(),
        }
        active_mafia[channel.id] = game
        view  = MafiaJoinView(host, channel.id)
        embed = _mafia_lobby_embed(game)
        await reply_fn(
            content=(
                f"🎭 **{host.display_name}** is starting a Mafia game!\n"
                f"Click **🙋 Join Game** to enter. Need at least **4 players**.\n"
                f"Host: pick a game mode from the dropdown, then click **🚀 Start**."
            ),
            embed=embed,
            view=view,
        )

    @commands.command(name="startmafia")
    async def prefix_startmafia(self, ctx: commands.Context):
        game = active_mafia.get(ctx.channel.id)
        if not game:
            await ctx.reply("❌ No Mafia lobby found. Use `!mafia` to start one.")
            return
        host_id = list(game["players"].keys())[0]
        if ctx.author.id != host_id:
            await ctx.reply("❌ Only the host can start.")
            return
        if len(game["players"]) < 4:
            await ctx.reply("❌ Need at least **4 players**!")
            return

        player_list = [v["member"] for v in game["players"].values()]
        mode        = game.get("mode", "classic")
        assigned    = _assign_mafia_roles(player_list, mode)
        game["players"]       = assigned
        game["phase"]         = "night"
        game["day"]           = 1
        game["votes"]         = {}
        game["night_actions"] = {}
        game["voted_players"] = set()

        mafia_members = [v["member"] for v in assigned.values() if v["role"] == "mafia"]
        dm_failures = []
        for uid, v in assigned.items():
            role = v["role"]
            teammates = mafia_members if role == "mafia" and len(mafia_members) > 1 else None
            try:
                await v["member"].send(embed=_mafia_role_dm_embed(role, ctx.guild.name, teammates))
            except discord.Forbidden:
                dm_failures.append(v["member"].display_name)

        fail_note = f"\n⚠️ Could not DM: {', '.join(dm_failures)}" if dm_failures else ""
        embed = _mafia_embed(
            game,
            title="🌙 Mafia — Night 0 Begins",
            msg="Roles sent! Night falls immediately — special roles check your DMs." + fail_note,
        )
        view = MafiaNightView(game, ctx.channel.id)
        msg  = await ctx.send(embed=embed, view=view)
        game["last_msg"] = msg
        await _send_night_dms(game, ctx.channel.id, bot=ctx.bot)
        await _start_phase_timer(ctx.channel, game, ctx.channel.id)

    @commands.command(name="mafvote")
    async def prefix_mafvote(self, ctx: commands.Context, target: discord.Member = None):
        game = active_mafia.get(ctx.channel.id)
        if not game or game["phase"] != "day":
            await ctx.reply("❌ No active Mafia day phase here.")
            return
        uid = ctx.author.id
        if uid not in game["players"] or not game["players"][uid]["alive"]:
            await ctx.reply("❌ You're not an alive player.")
            return
        if not target:
            await ctx.reply("Usage: `!mafvote @player`")
            return
        tid = target.id
        if tid not in game["players"] or not game["players"][tid]["alive"]:
            await ctx.reply("❌ That player is not alive in the game.")
            return

        vote_weight = 3 if game["players"][uid].get("revealed") else 1
        game["votes"][uid] = (tid, vote_weight)
        game.setdefault("voted_players", set()).add(uid)

        tally_counts: dict[int, int] = {}
        for val in game["votes"].values():
            t2, wt = val if isinstance(val, tuple) else (val, 1)
            tally_counts[t2] = tally_counts.get(t2, 0) + wt

        tally = "\n".join(
            f"{game['players'][vid]['member'].display_name}: **{cnt}**"
            for vid, cnt in sorted(tally_counts.items(), key=lambda x: -x[1])
        )
        voted, alive_count = _count_votes(game)
        await ctx.reply(
            f"🗳️ **{ctx.author.display_name}** votes for **{target.display_name}**!\n"
            f"**Votes ({voted}/{alive_count}):**\n{tally}"
        )
        await _check_auto_advance(ctx.channel, game, ctx.channel.id)

    @commands.command(name="lastwill")
    async def prefix_lastwill(self, ctx: commands.Context, *, text: str = ""):
        game = active_mafia.get(ctx.channel.id)
        if not game:
            await ctx.reply("❌ No active Mafia game here.")
            return
        uid = ctx.author.id
        if uid not in game["players"]:
            await ctx.reply("❌ You're not in this game.")
            return
        game["players"][uid]["last_will"] = text[:400]
        await ctx.message.delete()
        try:
            await ctx.author.send(f"✅ Last will saved: _{text[:400]}_" if text else "✅ Last will cleared.")
        except discord.Forbidden:
            pass

    @commands.command(name="stopmafia")
    async def prefix_stopmafia(self, ctx: commands.Context):
        if ctx.channel.id not in active_mafia:
            await ctx.reply("❌ No Mafia game running here.")
            return
        is_admin = ctx.channel.permissions_for(ctx.author).manage_messages
        game     = active_mafia[ctx.channel.id]
        is_player = ctx.author.id in game["players"]
        if not (is_player or is_admin):
            await ctx.reply("❌ Only players or admins can stop the game.")
            return
        _cancel_timer(ctx.channel.id)
        del active_mafia[ctx.channel.id]
        await ctx.reply("🛑 Mafia game stopped.")

    @app_commands.command(name="stopmafia", description="Stop the current Mafia game")
    async def slash_stopmafia(self, interaction: discord.Interaction):
        cid = interaction.channel_id
        if cid not in active_mafia:
            await interaction.response.send_message("❌ No Mafia game running here.", ephemeral=True)
            return
        is_admin = interaction.channel.permissions_for(interaction.user).manage_messages
        game     = active_mafia[cid]
        is_player = interaction.user.id in game["players"]
        if not (is_player or is_admin):
            await interaction.response.send_message("❌ Only players or admins can stop the game.", ephemeral=True)
            return
        _cancel_timer(cid)
        del active_mafia[cid]
        await interaction.response.send_message("🛑 Mafia game stopped.")

    # ── Mafia Guide ───────────────────────────────────────────────────────────

    @app_commands.command(name="mafguide", description="How to play Mafia 🎭")
    async def slash_mafguide(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=_mafia_guide_embed())

    @commands.command(name="mafguide")
    async def prefix_mafguide(self, ctx: commands.Context):
        await ctx.reply(embed=_mafia_guide_embed())

    # ── Counting ──────────────────────────────────────────────────────────────

    @app_commands.command(name="countsetup", description="Set the counting channel for this server")
    @app_commands.describe(channel="The channel where counting will happen")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slash_countsetup(self, interaction: discord.Interaction, channel: discord.TextChannel):
        g = _count_guild(interaction.guild_id)
        g["channel_id"] = channel.id
        _count_persist(interaction.guild_id)  # immediate save, not debounced
        await interaction.response.send_message(
            f"✅ Counting channel set to {channel.mention}!\n"
            f"Count up from **1** — no double-counting allowed."
        )

    @commands.command(name="countsetup")
    @commands.has_permissions(manage_channels=True)
    async def prefix_countsetup(self, ctx: commands.Context, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        g = _count_guild(ctx.guild.id)
        g["channel_id"] = channel.id
        _count_persist(ctx.guild.id)  # immediate save, not debounced
        await ctx.reply(f"✅ Counting channel set to {channel.mention}!\nCount up from **1** — no double-counting allowed.")

    @app_commands.command(name="countremove", description="Remove the counting channel setup")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slash_countremove(self, interaction: discord.Interaction):
        key = str(interaction.guild_id)
        if key in _count_data:
            del _count_data[key]
        _count_delete_db(interaction.guild_id)
        await interaction.response.send_message("🗑️ Counting channel has been removed.")

    @commands.command(name="countremove")
    @commands.has_permissions(manage_channels=True)
    async def prefix_countremove(self, ctx: commands.Context):
        key = str(ctx.guild.id)
        if key in _count_data:
            del _count_data[key]
        _count_delete_db(ctx.guild.id)
        await ctx.reply("🗑️ Counting channel has been removed.")

    @app_commands.command(name="countstats", description="Show the current count and high score")
    async def slash_countstats(self, interaction: discord.Interaction):
        await self._send_countstats(interaction.guild, lambda **kw: interaction.response.send_message(**kw))

    @commands.command(name="countstats")
    async def prefix_countstats(self, ctx: commands.Context):
        await self._send_countstats(ctx.guild, lambda **kw: ctx.reply(**kw))

    async def _send_countstats(self, guild, reply_fn):
        g  = _count_guild(guild.id)
        ch = f"<#{g['channel_id']}>" if g["channel_id"] else "Not set"
        embed = discord.Embed(title="🔢 Counting Stats", color=discord.Color.blurple())
        embed.add_field(name="📍 Channel",       value=ch,                         inline=False)
        embed.add_field(name="🔢 Current Count", value=f"**{g['count']}**",        inline=True)
        embed.add_field(name="🏆 High Score",    value=f"**{g['high_score']}**",   inline=True)
        if g.get("last_user_id"):
            embed.add_field(name="👤 Last Counter", value=f"<@{g['last_user_id']}>", inline=True)
        await reply_fn(embed=embed)

    @app_commands.command(name="countreset", description="Reset the count to 0 (admin only)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def slash_countreset(self, interaction: discord.Interaction):
        await self._do_countreset(interaction.guild_id, lambda **kw: interaction.response.send_message(**kw))

    @commands.command(name="countreset")
    @commands.has_permissions(manage_messages=True)
    async def prefix_countreset(self, ctx: commands.Context):
        await self._do_countreset(ctx.guild.id, lambda **kw: ctx.reply(**kw))

    async def _do_countreset(self, guild_id, reply_fn):
        g = _count_guild(guild_id)
        g["count"] = 0; g["last_user_id"] = None
        _count_schedule_save(guild_id)
        await reply_fn(content="🔄 Count has been reset to **0**.")

    # ── Compliment ────────────────────────────────────────────────────────────

    @app_commands.command(name="compliment", description="Send someone a genuine AI compliment 💛")
    @app_commands.describe(user="The user to compliment")
    async def slash_compliment(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer()
        prompt = (f"Give a single warm, genuine, and creative compliment for a Discord user named '{user.display_name}'. "
                  f"Make it feel heartfelt and unique — not generic. One short paragraph max.")
        reply  = await generate_ai_response(interaction.user.id, prompt, interaction.guild_id)
        embed  = discord.Embed(description=f"💛 {reply}", color=discord.Color.yellow())
        embed.set_author(name=f"A compliment for {user.display_name}", icon_url=user.display_avatar.url)
        embed.set_footer(text=f"Sent by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    @commands.command(name="compliment")
    async def prefix_compliment(self, ctx: commands.Context, user: discord.Member = None):
        if not user:
            await ctx.reply("Usage: `!compliment @user`"); return
        async with ctx.typing():
            prompt = (f"Give a single warm, genuine, and creative compliment for a Discord user named '{user.display_name}'. "
                      f"Make it feel heartfelt and unique — not generic. One short paragraph max.")
            reply  = await generate_ai_response(ctx.author.id, prompt, ctx.guild.id if ctx.guild else None)
        embed = discord.Embed(description=f"💛 {reply}", color=discord.Color.yellow())
        embed.set_author(name=f"A compliment for {user.display_name}", icon_url=user.display_avatar.url)
        await ctx.reply(embed=embed)

    # ── !roll ────────────────────────────────────────────────────────────────

    @staticmethod
    def _roll_embed(user: discord.User | discord.Member) -> discord.Embed:
        number = random.randint(1, 100)
        if number == 100:
            flavor, color = "🌟 PERFECT ROLL!", discord.Color.gold()
        elif number >= 90:
            flavor, color = "🔥 Incredible!", discord.Color.gold()
        elif number == 1:
            flavor, color = "💀 Ouch.", discord.Color.red()
        elif number <= 10:
            flavor, color = "😬 Rough one.", discord.Color.red()
        else:
            flavor, color = "🎲 Rolled!", discord.Color.blurple()

        embed = discord.Embed(
            title=flavor,
            description=f"**{user.display_name}** rolled... **{number}** / 100",
            color=color,
        )
        return embed

    @commands.command(name="roll", aliases=["dice"])
    async def prefix_roll(self, ctx: commands.Context):
        """!roll — roll a random number between 1 and 100."""
        await ctx.reply(embed=self._roll_embed(ctx.author))

    @app_commands.command(name="roll", description="Roll a random number between 1 and 100")
    async def slash_roll(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=self._roll_embed(interaction.user))

    # ── on_message — hangman + counting ──────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # ── Hangman ──
        cid = message.channel.id
        if cid in active_hangman:
            content = message.content.strip().lower()
            if len(content) == 1 and content.isalpha():
                state  = active_hangman[cid]
                letter = content
                word   = state["word"]
                if letter in state["guessed"]:
                    await message.reply(f"⚠️ **{letter}** was already guessed!", delete_after=4)
                    return
                state["guessed"].add(letter)
                if letter not in word:
                    state["wrong"] += 1
                if all(c in state["guessed"] for c in word):
                    del active_hangman[cid]
                    embed = _hangman_embed(state, finished=True)
                    embed.colour = discord.Color.green()
                    embed.add_field(name="🎉 Winner!", value=f"**{message.author.display_name}** guessed the word!", inline=False)
                    await message.reply(embed=embed)
                    return
                if state["wrong"] >= 6:
                    del active_hangman[cid]
                    embed = _hangman_embed(state, finished=True)
                    embed.colour = discord.Color.red()
                    embed.add_field(name="💀 Game Over!", value=f"The word was **{word}**.", inline=False)
                    await message.reply(embed=embed)
                    return
                await message.reply(embed=_hangman_embed(state))
            return  # Don't process counting inside hangman channel

        # ── Counting ──
        if not message.guild:
            return
        g = _count_guild(message.guild.id)
        if not g["channel_id"] or cid != g["channel_id"]:
            return

        # Cooldown: silently ignore if spamming
        if not check_cooldown(message.author.id):
            return
        content = message.content.strip()
        try:
            number = int(content)
        except ValueError:
            return  # silently ignore non-numbers

        expected = g["count"] + 1

        if number != expected:
            old = g["count"]
            await message.add_reaction("❌")
            if await _count_offer_save(message, old, f"❌ **Wrong number!** Expected **{expected}**."):
                return
            g["count"] = 0; g["last_user_id"] = None
            _count_schedule_save(message.guild.id)
            await message.channel.send(
                embed=discord.Embed(
                    description=f"❌ **Wrong number!** Expected **{expected}**.\n💀 {message.author.mention} ruined it! Streak of **{old}** lost! Count resets to **0**.",
                    color=discord.Color.red(),
                )
            )
            return

        if g["last_user_id"] == message.author.id:
            old = g["count"]
            await message.add_reaction("❌")
            if await _count_offer_save(message, old, f"❌ **{message.author.display_name}**, you can't count twice in a row!"):
                return
            g["count"] = 0; g["last_user_id"] = None
            _count_schedule_save(message.guild.id)
            await message.channel.send(
                embed=discord.Embed(
                    description=f"❌ **{message.author.display_name}**, you can't count twice in a row!\n💀 {message.author.mention} ruined it! Streak of **{old}** lost! Count resets to **0**.",
                    color=discord.Color.red(),
                )
            )
            return

        g["count"] = number; g["last_user_id"] = message.author.id
        if number > g["high_score"]:
            g["high_score"] = number
        _count_schedule_save(message.guild.id)
        await message.add_reaction("✅")

        if number % 100 == 0:
            await message.channel.send(f"🎉 **{number}!** Amazing! New high score: **{g['high_score']}**")
        elif number % 50 == 0:
            await message.channel.send(f"🔥 **{number}** — keep it going!")

    # ── Error handlers ────────────────────────────────────────────────────────

    @slash_countsetup.error
    async def _countsetup_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ You need **Manage Channels** permission.", ephemeral=True)

    @slash_countremove.error
    async def _countremove_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ You need **Manage Channels** permission.", ephemeral=True)

    @slash_countreset.error
    async def _countreset_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ You need **Manage Messages** permission.", ephemeral=True)

    # ── Akinator ──────────────────────────────────────────────────────────────

    @app_commands.command(name="akinator", description="Play Akinator — think of a character and I'll guess it!")
    @app_commands.describe(theme="What to guess: characters (default), animals, or objects")
    @app_commands.choices(theme=[
        app_commands.Choice(name="Characters 🧑", value="c"),
        app_commands.Choice(name="Animals 🐾",    value="a"),
        app_commands.Choice(name="Objects 📦",    value="o"),
    ])
    async def slash_akinator(self, interaction: discord.Interaction,
                             theme: app_commands.Choice[str] = None):
        theme_val = theme.value if theme else "c"
        await self._start_akinator(interaction.channel, interaction.user, theme_val,
                                   reply_fn=lambda **kw: interaction.response.send_message(**kw))

    @commands.command(name="akinator", aliases=["aki"])
    async def prefix_akinator(self, ctx: commands.Context, theme: str = "c"):
        theme = theme.lower()
        if theme not in ("c", "a", "o", "characters", "animals", "objects"):
            await ctx.reply("❌ Valid themes: `c` (characters), `a` (animals), `o` (objects)")
            return
        theme = theme[0]  # normalize "characters" → "c" etc.
        await self._start_akinator(ctx.channel, ctx.author, theme,
                                   reply_fn=lambda **kw: ctx.reply(**kw))

    @app_commands.command(name="stopaki", description="Stop the current Akinator game")
    async def slash_stopaki(self, interaction: discord.Interaction):
        await self._stop_akinator(interaction.channel, interaction.user,
                                   reply_fn=lambda **kw: interaction.response.send_message(**kw))

    @commands.command(name="stopaki")
    async def prefix_stopaki(self, ctx: commands.Context):
        await self._stop_akinator(ctx.channel, ctx.author, reply_fn=lambda **kw: ctx.reply(**kw))

    async def _stop_akinator(self, channel, user, reply_fn):
        game = active_akinator.get(channel.id)
        if not game:
            await reply_fn(content="❌ No Akinator game running here.", ephemeral=True)
            return
        is_player = user.id == game["user"].id
        is_admin  = channel.permissions_for(user).manage_messages
        if not (is_player or is_admin):
            await reply_fn(content="❌ Only the player or an admin can stop this game.", ephemeral=True)
            return
        del active_akinator[channel.id]
        await reply_fn(content="🛑 Akinator game stopped.")

    async def _start_akinator(self, channel, user, theme: str, reply_fn):
        if channel.id in active_akinator:
            await reply_fn(content="⚠️ An Akinator game is already running here! Use `!stopaki` to end it.",
                           ephemeral=True)
            return

        theme_labels = {"c": "characters (fictional/real people)", "a": "animals", "o": "objects"}
        theme_desc   = theme_labels.get(theme, "characters")

        # Build initial game state — no external API needed
        game = {
            "user":      user,
            "theme":     theme,
            "theme_desc": theme_desc,
            "history":   [],       # list of {"question": ..., "answer": ...}
            "step":      0,
            "finished":  False,
            "won":       False,
            "guess":     None,     # current guess being proposed
            "msg":       None,
        }
        active_akinator[channel.id] = game

        await reply_fn(content="🔮 Starting Akinator... Think of something and I'll try to guess it!")

        # Ask AI for the first question
        question = await _aki_get_next_question(game, channel.guild.id if channel.guild else None)
        game["current_question"] = question

        embed, view = _aki_embed_view(game, channel.id)
        msg = await channel.send(embed=embed, view=view)
        game["msg"] = msg


# ── AI-powered Akinator helpers ───────────────────────────────────────────────

_AKI_SYSTEM = """You are playing Akinator — a yes/no question game where you must guess what the player is thinking.
Rules:
- Ask ONE yes/no question per turn, or make a guess when confident (80%+ confidence).
- To ask a question, respond ONLY with: QUESTION: <your question>
- To make a guess, respond ONLY with: GUESS: <your guess>
- When you have confirmed a correct guess, respond ONLY with: WIN: <the answer>
- When you give up (after 20+ questions with no confidence), respond ONLY with: GIVE_UP
- Never explain yourself. Only output one of the four formats above.
- Base questions on the theme and narrow down systematically."""


async def _aki_get_next_question(game: dict, guild_id) -> str:
    """Ask AI for the next question or guess. Returns the question/guess text."""
    theme_desc = game["theme_desc"]
    history    = game["history"]
    step       = game["step"]

    history_text = "\n".join(
        f"Q{i+1}: {h['question']} → {h['answer']}"
        for i, h in enumerate(history)
    ) or "None yet."

    prompt = (
        f"Theme: {theme_desc}. The player is thinking of something in this category.\n"
        f"Questions asked so far ({step}):\n{history_text}\n\n"
        f"What is your next move? Remember: QUESTION: / GUESS: / WIN: / GIVE_UP only."
    )

    raw = await generate_ai_response(0, f"{_AKI_SYSTEM}\n\n{prompt}", 0, guild_id)
    raw = raw.strip()

    # Parse the response
    if raw.upper().startswith("GUESS:"):
        game["guess"] = raw[6:].strip()
        return f"Is it **{game['guess']}**?"
    elif raw.upper().startswith("WIN:"):
        game["finished"] = True
        game["won"]      = True
        game["guess"]    = raw[4:].strip()
        return f"I knew it! It's **{game['guess']}**! 🎉"
    elif raw.upper().startswith("GIVE_UP"):
        game["finished"] = True
        game["won"]      = False
        return "You defeated me! I couldn't figure it out. 😔"
    elif raw.upper().startswith("QUESTION:"):
        return raw[9:].strip()
    else:
        # Fallback — treat whole response as question
        return raw


def _aki_embed_view(game: dict, channel_id: int):
    theme_labels = {"c": "Characters 🧑", "a": "Animals 🐾", "o": "Objects 📦"}
    theme_label  = theme_labels.get(game["theme"], "Characters 🧑")

    finished = game.get("finished", False)
    won      = game.get("won", False)
    question = game.get("current_question", "...")
    step     = game.get("step", 0)
    guess    = game.get("guess")

    if finished:
        color = discord.Color.green() if won else discord.Color.red()
    elif guess:
        color = discord.Color.gold()
    else:
        color = discord.Color.purple()

    embed = discord.Embed(title="🔮 Akinator", color=color)
    embed.set_thumbnail(url="https://en.akinator.com/bundles/elokencobundle/img/akinator.png")

    if finished:
        embed.description = question
    elif guess:
        embed.description = f"🤔 **My guess — Question {step}**\n\n> {question}"
    else:
        # Confidence bar grows with step count (max 100% at step 25)
        confidence = min(100, step * 4)
        bar_filled = round(confidence / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        embed.description = f"**Question {step + 1}**\n\n> {question}"
        embed.add_field(name="📊 Confidence", value=f"`[{bar}]` {confidence}%", inline=True)

    embed.set_footer(text=f"Theme: {theme_label} • {game['user'].display_name}'s game • !stopaki to quit")

    view = AkinatorView(game, channel_id) if not finished else None
    return embed, view


class AkinatorView(discord.ui.View):
    def __init__(self, game: dict, channel_id: int):
        super().__init__(timeout=120)
        self.game       = game
        self.channel_id = channel_id

        if game.get("guess"):
            # Guess-confirmation mode: Yes / No
            yes_btn = discord.ui.Button(label="✅ Yes, that's it!", style=discord.ButtonStyle.success, custom_id="aki_yes")
            no_btn  = discord.ui.Button(label="❌ No, wrong guess",  style=discord.ButtonStyle.danger,  custom_id="aki_no")
            yes_btn.callback = self._on_confirm_yes
            no_btn.callback  = self._on_confirm_no
            self.add_item(yes_btn)
            self.add_item(no_btn)
        else:
            buttons = [
                ("✅ Yes",         "yes", discord.ButtonStyle.success),
                ("❌ No",          "no",  discord.ButtonStyle.danger),
                ("🤷 Don't Know", "idk", discord.ButtonStyle.secondary),
                ("🟡 Probably",   "p",   discord.ButtonStyle.secondary),
                ("🟠 Prob. Not",  "pn",  discord.ButtonStyle.secondary),
            ]
            for label, answer_key, style in buttons:
                btn = discord.ui.Button(label=label, style=style, custom_id=f"aki_{answer_key}")
                btn.callback = self._make_answer_callback(answer_key)
                self.add_item(btn)

    def _make_answer_callback(self, answer_key: str):
        _ANSWER_LABELS = {
            "yes": "Yes", "no": "No", "idk": "Don't Know",
            "p": "Probably", "pn": "Probably Not",
        }
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.game["user"].id:
                await interaction.response.send_message("❌ This isn't your game!", ephemeral=True)
                return
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
            except discord.errors.NotFound:
                return  # Interaction token expired — silently ignore duplicate/late click
            self.stop()
            game = self.game
            # Record the answer in history
            game["history"].append({
                "question": game.get("current_question", ""),
                "answer":   _ANSWER_LABELS.get(answer_key, answer_key),
            })
            game["step"]  += 1
            game["guess"]  = None  # clear pending guess

            # Get next move from AI (can take a few seconds — defer keeps token alive)
            next_q = await _aki_get_next_question(game, interaction.guild_id)
            game["current_question"] = next_q

            embed, view = _aki_embed_view(game, self.channel_id)
            if game.get("finished") and self.channel_id in active_akinator:
                del active_akinator[self.channel_id]
            # Must use edit_original_response after defer(), not interaction.message.edit()
            await interaction.edit_original_response(embed=embed, view=view)
        return callback

    async def _on_confirm_yes(self, interaction: discord.Interaction):
        if interaction.user.id != self.game["user"].id:
            await interaction.response.send_message("❌ This isn't your game!", ephemeral=True)
            return
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except discord.errors.NotFound:
            return  # Interaction token expired — silently ignore
        self.stop()
        game = self.game
        game["finished"] = True
        game["won"]      = True
        game["current_question"] = f"I knew it! It's **{game['guess']}**! 🎉"
        if self.channel_id in active_akinator:
            del active_akinator[self.channel_id]
        embed, _ = _aki_embed_view(game, self.channel_id)
        await interaction.edit_original_response(embed=embed, view=None)

    async def _on_confirm_no(self, interaction: discord.Interaction):
        if interaction.user.id != self.game["user"].id:
            await interaction.response.send_message("❌ This isn't your game!", ephemeral=True)
            return
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except discord.errors.NotFound:
            return  # Interaction token expired — silently ignore
        self.stop()
        game = self.game
        # Record the wrong guess and keep going
        game["history"].append({
            "question": game.get("current_question", ""),
            "answer":   "No (wrong guess)",
        })
        game["step"]  += 1
        game["guess"]  = None

        next_q = await _aki_get_next_question(game, interaction.guild_id)
        game["current_question"] = next_q

        embed, view = _aki_embed_view(game, self.channel_id)
        if game.get("finished") and self.channel_id in active_akinator:
            del active_akinator[self.channel_id]
        await interaction.edit_original_response(embed=embed, view=view)

    async def on_timeout(self):
        if self.channel_id in active_akinator:
            del active_akinator[self.channel_id]
        try:
            msg = self.game.get("msg")
            if msg:
                await msg.edit(
                    content="⏰ Akinator session timed out due to inactivity.",
                    embed=None, view=None
                )
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Fun(bot))