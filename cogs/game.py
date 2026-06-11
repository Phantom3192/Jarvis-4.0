import discord
from discord.ext import commands
from discord import app_commands
import random
import asyncio
import json
import os
from cogs.ai import generate_ai_response
from cogs.http_session import get_session

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
    global _count_conn, _count_data
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
                channel_id   INTEGER,
                count        INTEGER NOT NULL DEFAULT 0,
                high_score   INTEGER NOT NULL DEFAULT 0,
                last_user_id INTEGER
            )
        """)
        _count_conn.commit()
        rows = _count_conn.execute(
            "SELECT guild_id, channel_id, count, high_score, last_user_id FROM counting"
        ).fetchall()
        for guild_id, channel_id, count, high_score, last_user_id in rows:
            _count_data[guild_id] = {
                "channel_id":   channel_id,
                "count":        count,
                "high_score":   high_score,
                "last_user_id": last_user_id,
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
    if _count_conn is None:
        return
    g = _count_data.get(str(guild_id))
    if not g:
        return
    try:
        _count_conn.execute(
            """INSERT INTO counting (guild_id, channel_id, count, high_score, last_user_id)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   channel_id   = excluded.channel_id,
                   count        = excluded.count,
                   high_score   = excluded.high_score,
                   last_user_id = excluded.last_user_id""",
            (str(guild_id), g["channel_id"], g["count"], g["high_score"], g["last_user_id"])
        )
        _count_conn.commit()
    except Exception as e:
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


def _get_piece_font(size: int):
    if os.path.exists(_CHESS_FONT_PATH):
        return _ImageFont.truetype(_CHESS_FONT_PATH, size)
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
    e.set_footer(text=f"Move {board.fullmove_number} • Pick a move from the dropdown • !resign • !draw • !hint")
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
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent   = opponent
        self.channel    = channel

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
        try:
            # Disable buttons on timeout
            for item in self.children:
                item.disabled = True
        except Exception:
            pass



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
            board = self.game["board"]
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
#  MAFIA
# ═══════════════════════════════════════════════════════════════════════════════

active_mafia: dict[int, dict] = {}  # channel_id → game

MAFIA_ROLES = {
    "mafia":     "🔫",
    "detective": "🔍",
    "doctor":    "💉",
    "villager":  "🏘️",
}

MAFIA_ROLE_DESC = {
    "mafia":     "**Mafia** — Each night, the Mafia chooses one player to eliminate.",
    "detective": "**Detective** — Each night, you investigate one player to learn if they're Mafia.",
    "doctor":    "**Doctor** — Each night, you protect one player from elimination.",
    "villager":  "**Villager** — You have no special power. Vote wisely during the day!",
}


def _assign_mafia_roles(players: list) -> dict:
    """Assign roles based on player count."""
    n = len(players)
    roles: list[str] = []
    # Scale mafia count: 1 for ≤5, 2 for 6-9, 3 for 10+
    mafia_count = 1 if n <= 5 else (2 if n <= 9 else 3)
    roles += ["mafia"] * mafia_count
    roles += ["detective"]
    if n >= 5:
        roles += ["doctor"]
    roles += ["villager"] * (n - len(roles))
    random.shuffle(players)
    return {p.id: {"member": p, "role": r, "alive": True}
            for p, r in zip(players, roles)}


def _mafia_alive(game: dict):
    return [v for v in game["players"].values() if v["alive"]]


def _mafia_win_check(game: dict) -> str | None:
    alive    = _mafia_alive(game)
    n_mafia  = sum(1 for v in alive if v["role"] == "mafia")
    n_village = len(alive) - n_mafia
    if n_mafia == 0:
        return "village"
    if n_mafia >= n_village:
        return "mafia"
    return None


def _mafia_embed(game: dict, title="🎭 Mafia", msg="") -> discord.Embed:
    alive  = _mafia_alive(game)
    dead   = [v for v in game["players"].values() if not v["alive"]]
    phase  = game.get("phase", "lobby")
    color  = discord.Color.red() if phase == "night" else discord.Color.gold()
    e      = discord.Embed(title=title, color=color)
    if msg:
        e.description = msg
    alive_list = "\n".join(f"✅ {v['member'].display_name}" for v in alive) or "—"
    dead_list  = "\n".join(f"💀 {v['member'].display_name}" for v in dead)  or "None yet"
    e.add_field(name=f"🟢 Alive ({len(alive)})", value=alive_list,  inline=True)
    e.add_field(name=f"💀 Dead ({len(dead)})",   value=dead_list,   inline=True)
    e.add_field(name="📅 Phase", value=f"**{phase.capitalize()}** (Day {game.get('day', 1)})", inline=False)
    return e


class MafiaJoinView(discord.ui.View):
    """Lobby: players join before the host starts."""

    def __init__(self, host: discord.Member, channel_id: int):
        super().__init__(timeout=120)
        self.host       = host
        self.channel_id = channel_id

    @discord.ui.button(label="🙋 Join Game", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = active_mafia.get(self.channel_id)
        if not game or game["phase"] != "lobby":
            await interaction.response.send_message("❌ No lobby open.", ephemeral=True)
            return
        uid = interaction.user.id
        if uid in game["players"]:
            await interaction.response.send_message("⚠️ You're already in!", ephemeral=True)
            return
        game["players"][uid] = {"member": interaction.user, "role": None, "alive": True}
        names = ", ".join(v["member"].display_name for v in game["players"].values())
        await interaction.response.edit_message(
            content=f"🎭 **Mafia Lobby** — {len(game['players'])} players joined: {names}\n"
                    f"{self.host.mention} type `!startmafia` when ready (min 4 players).",
            view=self,
        )

    @discord.ui.button(label="🚀 Start (Host)", style=discord.ButtonStyle.success)
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
        await _mafia_begin(interaction, game)

    async def on_timeout(self):
        game = active_mafia.get(self.channel_id)
        if game and game["phase"] == "lobby":
            del active_mafia[self.channel_id]


async def _mafia_begin(interaction: discord.Interaction, game: dict):
    """Assign roles and send DMs, then begin Day 1."""
    player_list = [v["member"] for v in game["players"].values()]
    assigned    = _assign_mafia_roles(player_list)
    game["players"] = assigned
    game["phase"]   = "day"
    game["day"]     = 1
    game["votes"]   = {}
    game["night_actions"] = {}

    # DM roles
    dm_failures = []
    for uid, v in assigned.items():
        role = v["role"]
        emoji = MAFIA_ROLES[role]
        desc  = MAFIA_ROLE_DESC[role]
        try:
            await v["member"].send(
                f"🎭 **Mafia** — Your role in **{interaction.guild.name}**:\n\n"
                f"{emoji} {desc}"
            )
        except discord.Forbidden:
            dm_failures.append(v["member"].display_name)

    fail_note = ""
    if dm_failures:
        fail_note = f"\n⚠️ Could not DM: {', '.join(dm_failures)} — they should enable DMs."

    embed = _mafia_embed(game, title="🎭 Mafia — Day 1 Begins",
                         msg="Roles have been sent via DM! Discuss who you think the Mafia is.\nUse the **Vote** button or `!mafvote @user` to vote someone out.\nType `!mafnight` to end discussion and move to night."+fail_note)
    view  = MafiaDayView(game, interaction.channel_id)
    await interaction.response.edit_message(content=None, embed=embed, view=view)


class MafiaDayView(discord.ui.View):
    """Day phase: players vote to eliminate someone."""

    def __init__(self, game: dict, channel_id: int):
        super().__init__(timeout=300)
        self.game       = game
        self.channel_id = channel_id

    @discord.ui.button(label="🗳️ Vote", style=discord.ButtonStyle.danger)
    async def vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.game
        uid  = interaction.user.id
        if uid not in game["players"] or not game["players"][uid]["alive"]:
            await interaction.response.send_message("❌ You're not an alive player.", ephemeral=True)
            return
        alive_options = [
            discord.SelectOption(label=v["member"].display_name, value=str(v["member"].id))
            for v in _mafia_alive(game) if v["member"].id != uid
        ]
        if not alive_options:
            await interaction.response.send_message("No one to vote for.", ephemeral=True)
            return
        select = discord.ui.Select(placeholder="Vote to eliminate…", options=alive_options)

        async def on_select(inter: discord.Interaction):
            target_id = int(inter.data["values"][0])
            game["votes"][uid] = target_id
            target_name = game["players"][target_id]["member"].display_name
            vote_counts = {}
            for v in game["votes"].values():
                vote_counts[v] = vote_counts.get(v, 0) + 1
            tally = "\n".join(
                f"{game['players'][vid]['member'].display_name}: {cnt} vote(s)"
                for vid, cnt in sorted(vote_counts.items(), key=lambda x: -x[1])
            )
            await inter.response.send_message(
                f"🗳️ **{inter.user.display_name}** voted for **{target_name}**!\n\n**Current votes:**\n{tally}",
                ephemeral=False
            )

        select.callback = on_select
        v = discord.ui.View(timeout=30)
        v.add_item(select)
        await interaction.response.send_message("Who do you vote to eliminate?", view=v, ephemeral=True)

    @discord.ui.button(label="🌙 End Day / Go to Night", style=discord.ButtonStyle.secondary)
    async def end_day(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.game
        uid  = interaction.user.id
        is_admin = interaction.channel.permissions_for(interaction.user).manage_messages
        if uid not in game["players"] and not is_admin:
            await interaction.response.send_message("❌ Only players or admins can advance the phase.", ephemeral=True)
            return

        # Tally votes
        votes = game.get("votes", {})
        if votes:
            vote_counts = {}
            for v in votes.values():
                vote_counts[v] = vote_counts.get(v, 0) + 1
            eliminated_id = max(vote_counts, key=vote_counts.__getitem__)
            game["players"][eliminated_id]["alive"] = False
            elim_name = game["players"][eliminated_id]["member"].display_name
            elim_role = game["players"][eliminated_id]["role"]
            elim_emoji = MAFIA_ROLES[elim_role]
            elim_msg = f"☠️ **{elim_name}** was eliminated by vote! They were: {elim_emoji} **{elim_role.capitalize()}**"
        else:
            elim_msg = "☀️ No votes cast — no one was eliminated today."

        game["votes"] = {}
        winner = _mafia_win_check(game)
        if winner:
            await _mafia_end(interaction, game, winner, extra=elim_msg)
            return

        game["phase"]         = "night"
        game["night_actions"] = {}
        embed = _mafia_embed(game, title=f"🌙 Night {game['day']}",
                             msg=f"{elim_msg}\n\n**Night falls...** Mafia, Detective, and Doctor: check your DMs and submit your action with `!mafaction @user`.")
        self.stop()
        view = MafiaNightView(game, self.channel_id)
        await interaction.response.edit_message(embed=embed, view=view)
        # DM special roles
        for uid2, v in game["players"].items():
            if not v["alive"]:
                continue
            if v["role"] == "mafia":
                targets = [p["member"].display_name for p in _mafia_alive(game) if p["member"].id != uid2 and p["role"] != "mafia"]
                try:
                    await v["member"].send(
                        f"🌙 **Night {game['day']}** — Choose your target:\n"
                        f"Alive non-mafia players: {', '.join(targets)}\n"
                        f"Use `!mafaction @target` in the game channel to eliminate."
                    )
                except discord.Forbidden:
                    pass
            elif v["role"] in ("detective", "doctor"):
                action = "investigate" if v["role"] == "detective" else "protect"
                try:
                    await v["member"].send(
                        f"🌙 **Night {game['day']}** — Choose who to **{action}**:\n"
                        f"Use `!mafaction @target` in the game channel."
                    )
                except discord.Forbidden:
                    pass

    async def on_timeout(self):
        pass


class MafiaNightView(discord.ui.View):
    def __init__(self, game: dict, channel_id: int):
        super().__init__(timeout=180)
        self.game       = game
        self.channel_id = channel_id

    @discord.ui.button(label="☀️ Resolve Night", style=discord.ButtonStyle.primary)
    async def resolve(self, interaction: discord.Interaction, button: discord.ui.Button):
        game     = self.game
        uid      = interaction.user.id
        is_admin = interaction.channel.permissions_for(interaction.user).manage_messages
        if uid not in game["players"] and not is_admin:
            await interaction.response.send_message("❌ Only players or admins can advance.", ephemeral=True)
            return

        actions     = game.get("night_actions", {})
        mafia_kills = [tid for (role, tid) in actions.items() if role == "mafia"]
        doctor_save = [tid for (role, tid) in actions.items() if role == "doctor"]
        det_checks  = {uid2: tid for (role, tid) in actions.items() if role == "detective"
                       for uid2, v in game["players"].items() if v["role"] == "detective" and uid2 == uid}

        msgs = []
        killed_id = mafia_kills[0] if mafia_kills else None
        if killed_id and killed_id not in doctor_save:
            game["players"][killed_id]["alive"] = False
            killed_name = game["players"][killed_id]["member"].display_name
            killed_role = game["players"][killed_id]["role"]
            msgs.append(f"☠️ **{killed_name}** was eliminated by the Mafia! They were: {MAFIA_ROLES[killed_role]} **{killed_role.capitalize()}**")
        elif killed_id and killed_id in doctor_save:
            saved_name = game["players"][killed_id]["member"].display_name
            msgs.append(f"💉 Someone was targeted last night... but the **Doctor** saved **{saved_name}**!")
        else:
            msgs.append("😴 The night was quiet — no one was eliminated.")

        game["night_actions"] = {}
        game["phase"]         = "day"
        game["day"]          += 1
        game["votes"]         = {}

        winner = _mafia_win_check(game)
        if winner:
            await _mafia_end(interaction, game, winner, extra="\n".join(msgs))
            return

        embed = _mafia_embed(game, title=f"☀️ Day {game['day']} Begins",
                             msg="\n".join(msgs) + "\n\nDiscuss and vote! Use **Vote** or `!mafvote @user`.")
        self.stop()
        view = MafiaDayView(game, self.channel_id)
        await interaction.response.edit_message(embed=embed, view=view)

    async def on_timeout(self):
        pass


async def _mafia_end(interaction: discord.Interaction, game: dict, winner: str, extra=""):
    cid = interaction.channel_id
    if cid in active_mafia:
        del active_mafia[cid]
    if winner == "village":
        title = "🎉 Village Wins!"
        msg   = "The Mafia has been eliminated. Peace is restored!"
        color = discord.Color.green()
    else:
        title = "💀 Mafia Wins!"
        msg   = "The Mafia has taken over the town!"
        color = discord.Color.dark_red()

    # Reveal all roles
    reveal = "\n".join(
        f"{MAFIA_ROLES[v['role']]} **{v['member'].display_name}** — {v['role'].capitalize()}"
        f"{' 💀' if not v['alive'] else ''}"
        for v in game["players"].values()
    )
    embed = discord.Embed(title=title, description=msg, color=color)
    if extra:
        embed.add_field(name="Last Event", value=extra, inline=False)
    embed.add_field(name="📋 Full Role Reveal", value=reveal, inline=False)
    await interaction.response.edit_message(content=None, embed=embed, view=None)


# ═══════════════════════════════════════════════════════════════════════════════
#  COG
# ═══════════════════════════════════════════════════════════════════════════════

class Fun(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        asyncio.create_task(_count_init_db())

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
                                         reply_fn=lambda **kw: interaction.response.send_message(**kw))

    @commands.command(name="chess")
    async def prefix_chess(self, ctx: commands.Context, opponent: discord.Member = None):
        if not opponent:
            await ctx.reply("Usage: `!chess @user`")
            return
        await self._send_chess_challenge(ctx.channel, ctx.author, opponent,
                                         reply_fn=lambda **kw: ctx.reply(**kw))

    async def _send_chess_challenge(self, channel, challenger, opponent, reply_fn):
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
        await reply_fn(
            content=f"♟️ {challenger.mention} challenges {opponent.mention} to a game of Chess!\n"
                    f"{opponent.mention} — do you accept?",
            view=view,
        )

    
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
        game = active_chess.get(channel.id)
        if not game:
            await reply_fn(content="❌ No chess game running here.", ephemeral=True); return
        whose_turn = game["white"] if game["board"].turn == _chess.WHITE else game["black"]
        if user.id != whose_turn.id:
            await reply_fn(content="❌ It's not your turn.", ephemeral=True); return
        async with channel.typing():
            board  = game["board"]
            legal  = [m.uci() for m in list(board.legal_moves)[:20]]
            colour = "White" if board.turn == _chess.WHITE else "Black"
            prompt = (f"You are a chess coach. FEN: {board.fen()}\n"
                      f"It is {colour}'s turn. Legal moves: {', '.join(legal)}.\n"
                      f"Suggest the best move and explain why in 2-3 sentences.")
            hint = await generate_ai_response(user.id, prompt, channel.guild.id if channel.guild else None)
        await reply_fn(content=f"💡 **Chess Hint:**\n{hint}")

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

    @app_commands.command(name="mafia", description="Start a Mafia game in this channel")
    async def slash_mafia(self, interaction: discord.Interaction):
        await self._start_mafia(interaction.channel, interaction.user,
                                reply_fn=lambda **kw: interaction.response.send_message(**kw))

    @commands.command(name="mafia")
    async def prefix_mafia(self, ctx: commands.Context):
        await self._start_mafia(ctx.channel, ctx.author, reply_fn=lambda **kw: ctx.reply(**kw))

    async def _start_mafia(self, channel, host, reply_fn):
        if channel.id in active_mafia:
            await reply_fn(content="⚠️ A Mafia game is already running here!", ephemeral=True); return
        game = {
            "phase":         "lobby",
            "players":       {host.id: {"member": host, "role": None, "alive": True}},
            "day":           1,
            "votes":         {},
            "night_actions": {},
        }
        active_mafia[channel.id] = game
        view = MafiaJoinView(host, channel.id)
        await reply_fn(
            content=f"🎭 **{host.display_name}** is starting a Mafia game!\n"
                    f"Press **Join Game** to enter. Need at least **4 players**.\n"
                    f"{host.mention} press **Start** when everyone is ready.",
            view=view,
        )

    @commands.command(name="startmafia")
    async def prefix_startmafia(self, ctx: commands.Context):
        game = active_mafia.get(ctx.channel.id)
        if not game:
            await ctx.reply("❌ No Mafia lobby found. Use `!mafia` to start one."); return
        if ctx.author.id != list(game["players"].keys())[0]:
            await ctx.reply("❌ Only the host can start."); return
        if len(game["players"]) < 4:
            await ctx.reply("❌ Need at least **4 players**!"); return

        class FakeInteraction:
            channel_id = ctx.channel.id
            guild      = ctx.guild
            async def response_edit_message(self, **kw): pass

        # Use a direct channel send since we don't have an interaction here
        player_list = [v["member"] for v in game["players"].values()]
        assigned    = _assign_mafia_roles(player_list)
        game["players"] = assigned
        game["phase"]   = "day"
        game["day"]     = 1
        game["votes"]   = {}
        game["night_actions"] = {}

        dm_failures = []
        for uid, v in assigned.items():
            role = v["role"]; emoji = MAFIA_ROLES[role]; desc = MAFIA_ROLE_DESC[role]
            try:
                await v["member"].send(f"🎭 **Mafia** — Your role in **{ctx.guild.name}**:\n\n{emoji} {desc}")
            except discord.Forbidden:
                dm_failures.append(v["member"].display_name)

        fail_note = f"\n⚠️ Could not DM: {', '.join(dm_failures)}" if dm_failures else ""
        embed = _mafia_embed(game, title="🎭 Mafia — Day 1 Begins",
                             msg="Roles sent! Discuss and vote. Use `!mafvote @user` or the Vote button." + fail_note)
        view  = MafiaDayView(game, ctx.channel.id)
        await ctx.send(embed=embed, view=view)

    @commands.command(name="mafvote")
    async def prefix_mafvote(self, ctx: commands.Context, target: discord.Member = None):
        game = active_mafia.get(ctx.channel.id)
        if not game or game["phase"] != "day":
            await ctx.reply("❌ No active Mafia day phase here."); return
        uid = ctx.author.id
        if uid not in game["players"] or not game["players"][uid]["alive"]:
            await ctx.reply("❌ You're not an alive player."); return
        if not target:
            await ctx.reply("Usage: `!mafvote @player`"); return
        tid = target.id
        if tid not in game["players"] or not game["players"][tid]["alive"]:
            await ctx.reply("❌ That player is not alive in the game."); return
        game["votes"][uid] = tid
        vote_counts = {}
        for v in game["votes"].values():
            vote_counts[v] = vote_counts.get(v, 0) + 1
        tally = "\n".join(
            f"{game['players'][vid]['member'].display_name}: {cnt}"
            for vid, cnt in sorted(vote_counts.items(), key=lambda x: -x[1])
        )
        await ctx.reply(f"🗳️ **{ctx.author.display_name}** votes for **{target.display_name}**!\n\n**Votes:**\n{tally}")

    @commands.command(name="mafaction")
    async def prefix_mafaction(self, ctx: commands.Context, target: discord.Member = None):
        game = active_mafia.get(ctx.channel.id)
        if not game or game["phase"] != "night":
            await ctx.reply("❌ No active Mafia night phase here."); return
        uid = ctx.author.id
        if uid not in game["players"] or not game["players"][uid]["alive"]:
            await ctx.reply("❌ You're not an alive player."); return
        role = game["players"][uid]["role"]
        if role not in ("mafia", "detective", "doctor"):
            await ctx.reply("❌ Villagers have no night action.", delete_after=5); return
        if not target:
            await ctx.reply(f"Usage: `!mafaction @player`"); return
        if target.id not in game["players"] or not game["players"][target.id]["alive"]:
            await ctx.reply("❌ That player is not alive."); return
        game["night_actions"][role] = target.id
        if role == "detective":
            is_mafia = game["players"][target.id]["role"] == "mafia"
            try:
                await ctx.author.send(
                    f"🔍 **Investigation result:** {target.display_name} is "
                    f"{'🔫 **MAFIA**' if is_mafia else '✅ **NOT Mafia**'}."
                )
            except discord.Forbidden:
                pass
            await ctx.message.delete()
            await ctx.send(f"🌙 A detective has completed their investigation.", delete_after=5)
        else:
            action_word = "targeted" if role == "mafia" else "protected"
            try:
                await ctx.author.send(f"✅ You have {action_word} **{target.display_name}** tonight.")
            except discord.Forbidden:
                pass
            await ctx.message.delete()
            await ctx.send(f"🌙 A night action has been submitted.", delete_after=5)

    @commands.command(name="stopmafia")
    async def prefix_stopmafia(self, ctx: commands.Context):
        if ctx.channel.id not in active_mafia:
            await ctx.reply("❌ No Mafia game running here."); return
        is_admin = ctx.channel.permissions_for(ctx.author).manage_messages
        game     = active_mafia[ctx.channel.id]
        is_host  = ctx.author.id in game["players"]
        if not (is_host or is_admin):
            await ctx.reply("❌ Only players or admins can stop the game."); return
        del active_mafia[ctx.channel.id]
        await ctx.reply("🛑 Mafia game stopped.")

    # ── Counting ──────────────────────────────────────────────────────────────

    @app_commands.command(name="countsetup", description="Set the counting channel for this server")
    @app_commands.describe(channel="The channel where counting will happen")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slash_countsetup(self, interaction: discord.Interaction, channel: discord.TextChannel):
        g = _count_guild(interaction.guild_id)
        g["channel_id"] = channel.id
        _count_schedule_save(interaction.guild_id)
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
        _count_schedule_save(ctx.guild.id)
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
        content = message.content.strip()
        try:
            number = int(content)
        except ValueError:
            return  # silently ignore non-numbers

        expected = g["count"] + 1

        if number != expected:
            old = g["count"]
            g["count"] = 0; g["last_user_id"] = None
            _count_schedule_save(message.guild.id)
            await message.add_reaction("❌")
            await message.reply(f"❌ **Wrong number!** Expected **{expected}**. Count resets to **0**. (was {old})", delete_after=8)
            return

        if g["last_user_id"] == message.author.id:
            old = g["count"]
            g["count"] = 0; g["last_user_id"] = None
            _count_schedule_save(message.guild.id)
            await message.add_reaction("❌")
            await message.reply(f"❌ **{message.author.display_name}**, you can't count twice in a row! Count resets to **0**. (was {old})", delete_after=8)
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

        await reply_fn(content="🔮 Starting Akinator... Think of something and I'll try to guess it!")

        try:
            from akinator.async_client import AsyncClient
            aki = AsyncClient()
            await aki.start_game(theme=theme)
        except Exception as e:
            await channel.send(f"❌ Failed to connect to Akinator: `{e}`\nThe service may be temporarily down.")
            return

        game = {
            "aki":    aki,
            "user":   user,
            "theme":  theme,
            "msg":    None,   # the current discord Message with buttons
        }
        active_akinator[channel.id] = game

        embed, view = _aki_embed_view(game, channel.id)
        msg = await channel.send(embed=embed, view=view)
        game["msg"] = msg


def _aki_embed_view(game: dict, channel_id: int):
    aki   = game["aki"]
    theme_labels = {"c": "Characters 🧑", "a": "Animals 🐾", "o": "Objects 📦"}
    theme_label  = theme_labels.get(game["theme"], "Characters 🧑")

    color = discord.Color.purple()
    if aki.finished:
        color = discord.Color.green() if aki.win else discord.Color.red()

    embed = discord.Embed(title="🔮 Akinator", color=color)
    embed.set_thumbnail(url=aki.akitude_url)

    if aki.finished:
        if aki.win:
            embed.description = f"✨ **I got it!**\n\n{aki.question}"
            if aki.photo:
                embed.set_image(url=aki.photo)
            if aki.name_proposition:
                embed.add_field(name="My guess was:", value=f"**{aki.name_proposition}**", inline=False)
                if aki.description_proposition:
                    embed.add_field(name="About:", value=aki.description_proposition, inline=False)
        else:
            embed.description = f"😔 **You defeated me!**\n\n{aki.question}"
    else:
        if aki.win:
            # Akinator is proposing a guess — waiting for yes/no confirmation
            embed.description = f"🤔 **Is it... {aki.name_proposition}?**"
            if aki.description_proposition:
                embed.add_field(name="About:", value=aki.description_proposition, inline=False)
            if aki.photo:
                embed.set_image(url=aki.photo)
        else:
            embed.description = f"**Question {aki.step + 1}**\n\n> {aki.question}"

        bar_filled = round(aki.progression / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        embed.add_field(name="📊 Confidence", value=f"`[{bar}]` {aki.progression:.0f}%", inline=True)

    embed.set_footer(text=f"Theme: {theme_label} • {game['user'].display_name}'s game • !stopaki to quit")

    view = AkinatorView(game, channel_id) if not aki.finished else None
    return embed, view


class AkinatorView(discord.ui.View):
    def __init__(self, game: dict, channel_id: int):
        super().__init__(timeout=120)
        self.game       = game
        self.channel_id = channel_id
        aki = game["aki"]

        if aki.win:
            # Propose mode: only Yes / No
            yes_btn = discord.ui.Button(label="✅ Yes, that's it!", style=discord.ButtonStyle.success, custom_id="aki_yes")
            no_btn  = discord.ui.Button(label="❌ No, wrong guess",  style=discord.ButtonStyle.danger,  custom_id="aki_no")
            yes_btn.callback = self._on_yes
            no_btn.callback  = self._on_no
            self.add_item(yes_btn)
            self.add_item(no_btn)
        else:
            buttons = [
                ("✅ Yes",         "yes",   discord.ButtonStyle.success),
                ("❌ No",          "no",    discord.ButtonStyle.danger),
                ("🤷 Don't Know", "i",     discord.ButtonStyle.secondary),
                ("🟡 Probably",   "p",     discord.ButtonStyle.secondary),
                ("🟠 Prob. Not",  "pn",    discord.ButtonStyle.secondary),
                ("⬅️ Undo",       "_back", discord.ButtonStyle.secondary),
            ]
            for label, cid, style in buttons:
                btn = discord.ui.Button(label=label, style=style, custom_id=f"aki_{cid}")
                if cid == "_back":
                    btn.disabled = (aki.step == 0)
                    btn.callback = self._on_back
                else:
                    btn.callback = self._make_answer_callback(cid)
                self.add_item(btn)

    def _make_answer_callback(self, answer: str):
        async def callback(interaction: discord.Interaction):
            await self._handle_answer(interaction, answer)
        return callback

    async def _check_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.game["user"].id:
            await interaction.response.send_message("❌ This isn't your game!", ephemeral=True)
            return False
        return True

    async def _handle_answer(self, interaction: discord.Interaction, answer: str):
        if not await self._check_user(interaction):
            return
        await interaction.response.defer()
        self.stop()
        game = self.game
        aki  = game["aki"]
        try:
            await aki.answer(answer)
        except Exception as e:
            await interaction.followup.send(f"❌ Akinator error: `{e}`", ephemeral=True)
            if self.channel_id in active_akinator:
                del active_akinator[self.channel_id]
            return

        embed, view = _aki_embed_view(game, self.channel_id)
        if aki.finished:
            if self.channel_id in active_akinator:
                del active_akinator[self.channel_id]
        await interaction.message.edit(embed=embed, view=view)

    async def _on_yes(self, interaction: discord.Interaction):
        await self._handle_answer(interaction, "yes")

    async def _on_no(self, interaction: discord.Interaction):
        await self._handle_answer(interaction, "no")

    async def _on_back(self, interaction: discord.Interaction):
        if not await self._check_user(interaction):
            return
        await interaction.response.defer()
        self.stop()
        game = self.game
        aki  = game["aki"]
        try:
            await aki.back()
        except Exception as e:
            await interaction.followup.send(f"❌ Can't go back: `{e}`", ephemeral=True)
            return
        embed, view = _aki_embed_view(game, self.channel_id)
        await interaction.message.edit(embed=embed, view=view)

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