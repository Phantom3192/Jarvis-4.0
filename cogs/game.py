import discord
from discord.ext import commands
from discord import app_commands
import random
import asyncio
from cogs.ai import generate_ai_response
from cogs.http_session import get_session

# ── Constants ─────────────────────────────────────────────────────────────────

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
    discord.Color.green(),
    discord.Color.green(),
    discord.Color.from_rgb(144, 238, 144),
    discord.Color.yellow(),
    discord.Color.orange(),
    discord.Color.from_rgb(255, 80, 0),
    discord.Color.red(),
]


HANGMAN_WORDS = [
    "python", "discord", "robot", "galaxy", "jarvis", "keyboard", "asteroid",
    "phantom", "thunder", "wizard", "castle", "penguin", "lantern", "tornado",
    "dolphin", "volcano", "muffin", "crystal", "dragon", "shadow", "mirror",
    "pirate", "jungle", "rocket", "cobalt", "marble", "falcon", "puzzle",
]


# ── Active game tracking ──────────────────────────────────────────────────────

active_hangman: dict[int, dict] = {}



import chess as _chess
import io as _io
import os as _os
from PIL import Image as _Image, ImageDraw as _ImageDraw, ImageFont as _ImageFont

# ═══════════════════════════════════════════════════════════════════════════════
#  CHESS
# ═══════════════════════════════════════════════════════════════════════════════

# ── Board image rendering — pure shapes, no font dependency ──────────────────

_SQ     = 110
_BORDER = 52
_BSIZE  = _SQ * 8 + _BORDER * 2

_COL_LIGHT  = (240, 217, 181)
_COL_DARK   = (181, 136,  99)
_COL_BG     = ( 22,  21,  28)
_COL_BORDER = ( 58,  46,  36)
_COL_LABEL  = (255, 220, 160)


def _label_font(size: int):
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/crosextra/Carlito-Bold.ttf",
        "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
    ]:
        if _os.path.exists(p):
            return _ImageFont.truetype(p, size)
    return _ImageFont.load_default()


def _draw_piece(draw, piece_type: int, is_white: bool, cx: int, cy: int, sq: int):
    import math as _math
    s       = sq
    fill    = (252, 252, 248) if is_white else (18,  18,  22)
    mid     = (200, 195, 185) if is_white else (60,  58,  65)
    outline = (50,  45,  40)  if is_white else (230, 225, 220)
    lw      = max(2, s // 28)

    def rect(x0,y0,x1,y1):
        draw.rectangle([x0,y0,x1,y1], fill=fill, outline=outline, width=lw)
    def ellipse(x0,y0,x1,y1):
        draw.ellipse([x0,y0,x1,y1], fill=fill, outline=outline, width=lw)
    def poly(pts):
        draw.polygon(pts, fill=fill, outline=outline)

    def base(w=0.62):
        bw = int(s*w); bh = int(s*0.13); by = cy+int(s*0.32)
        rect(cx-bw//2, by, cx+bw//2, by+bh)
        draw.line([cx-bw//2+lw, by+lw, cx+bw//2-lw, by+lw], fill=mid, width=max(1,lw-1))

    if piece_type == _chess.PAWN:
        base(0.46)
        nw = int(s*0.16)
        rect(cx-nw//2, cy+int(s*0.08), cx+nw//2, cy+int(s*0.32))
        r = int(s*0.22); hy = cy-int(s*0.10)
        ellipse(cx-r, hy-r, cx+r, hy+r)

    elif piece_type == _chess.ROOK:
        base(0.58)
        bw = int(s*0.44)
        rect(cx-bw//2, cy-int(s*0.20), cx+bw//2, cy+int(s*0.32))
        draw.rectangle([cx-bw//2+lw*2, cy-int(s*0.20)+lw*2,
                        cx+bw//2-lw*2, cy-int(s*0.20)+lw*4], fill=mid)
        tw=int(s*0.13); th=int(s*0.14); ty=cy-int(s*0.20)-th
        for tx in [cx-int(s*0.16), cx, cx+int(s*0.16)]:
            rect(tx-tw//2, ty, tx+tw//2, ty+th)

    elif piece_type == _chess.KNIGHT:
        base(0.54)
        pts = [
            (cx-int(s*0.20), cy+int(s*0.32)),
            (cx+int(s*0.22), cy+int(s*0.32)),
            (cx+int(s*0.22), cy+int(s*0.04)),
            (cx+int(s*0.32), cy-int(s*0.18)),
            (cx+int(s*0.20), cy-int(s*0.36)),
            (cx+int(s*0.00), cy-int(s*0.30)),
            (cx-int(s*0.10), cy-int(s*0.16)),
            (cx-int(s*0.20), cy-int(s*0.04)),
        ]
        poly(pts); draw.polygon(pts, outline=outline)
        ellipse(cx+int(s*0.04), cy-int(s*0.10), cx+int(s*0.26), cy+int(s*0.08))
        nr=max(2,int(s*0.03)); nx,ny=cx+int(s*0.18),cy+int(s*0.00)
        draw.ellipse([nx-nr,ny-nr,nx+nr,ny+nr], fill=outline)
        er=max(2,int(s*0.05)); ex,ey=cx+int(s*0.10),cy-int(s*0.22)
        draw.ellipse([ex-er,ey-er,ex+er,ey+er], fill=outline)

    elif piece_type == _chess.BISHOP:
        base(0.52)
        poly([(cx-int(s*0.22),cy+int(s*0.32)),(cx+int(s*0.22),cy+int(s*0.32)),
              (cx+int(s*0.12),cy-int(s*0.02)),(cx-int(s*0.12),cy-int(s*0.02))])
        r1=int(s*0.16)
        ellipse(cx-r1, cy-int(s*0.20)-r1, cx+r1, cy-int(s*0.02))
        r2=int(s*0.12); hy=cy-int(s*0.34)
        ellipse(cx-r2, hy-r2, cx+r2, hy+r2)
        draw.polygon([(cx,hy-r2-int(s*0.12)),(cx-int(s*0.04),hy-r2),(cx+int(s*0.04),hy-r2)],
                     fill=fill, outline=outline)
        draw.line([cx-int(s*0.06),hy,cx+int(s*0.06),hy], fill=mid, width=max(1,lw-1))

    elif piece_type == _chess.QUEEN:
        base(0.62)
        ellipse(cx-int(s*0.24), cy-int(s*0.16), cx+int(s*0.24), cy+int(s*0.32))
        cr=int(s*0.24); top_y=cy-int(s*0.16)
        draw.arc([cx-cr,top_y-int(s*0.04),cx+cr,top_y+int(s*0.08)],180,360,fill=outline,width=lw)
        ball_r=max(3,int(s*0.08))
        for i in range(5):
            angle=_math.pi+(i/4)*_math.pi
            bx2=cx+int(cr*_math.cos(angle)); by2=top_y+int((s*0.06)*_math.sin(angle))-int(s*0.02)
            draw.ellipse([bx2-ball_r,by2-ball_r,bx2+ball_r,by2+ball_r],
                         fill=fill, outline=outline, width=max(1,lw-1))

    elif piece_type == _chess.KING:
        base(0.62)
        ellipse(cx-int(s*0.22), cy-int(s*0.12), cx+int(s*0.22), cy+int(s*0.32))
        cr=int(s*0.22); top_y=cy-int(s*0.12)
        for angle in [_math.pi+0.3, _math.pi*1.5, _math.pi*2-0.3]:
            px=cx+int(cr*_math.cos(angle)); py=top_y+int((s*0.14)*_math.sin(angle))
            draw.line([cx,top_y,px,py], fill=outline, width=lw*2)
            ball_r=max(3,int(s*0.07))
            draw.ellipse([px-ball_r,py-ball_r,px+ball_r,py+ball_r],
                         fill=fill, outline=outline, width=max(1,lw-1))
        arm=int(s*0.13); cw=max(2,int(s*0.05)); cross_y=top_y-int(s*0.16)
        rect(cx-cw, cross_y-arm, cx+cw, cross_y+arm)
        rect(cx-arm, cross_y-cw, cx+arm, cross_y+cw)


def _render_board_image(board: "_chess.Board", flipped: bool = False, last_move=None) -> bytes:
    img  = _Image.new("RGB", (_BSIZE, _BSIZE), _COL_BG)
    draw = _ImageDraw.Draw(img, "RGBA")
    lf   = _label_font(24)

    # Wood border
    draw.rectangle([0, 0, _BSIZE, _BSIZE], fill=_COL_BORDER)
    draw.rectangle([_BORDER-4, _BORDER-4, _BSIZE-_BORDER+4, _BSIZE-_BORDER+4],
                   fill=(22,21,28), outline=(80,65,50), width=3)

    ranks = range(7, -1, -1) if not flipped else range(8)
    files = range(8)          if not flipped else range(7, -1, -1)

    # Pass 1 — squares + highlights
    for ri, rank in enumerate(ranks):
        for fi, file in enumerate(files):
            sq    = _chess.square(file, rank)
            x     = _BORDER + fi * _SQ
            y     = _BORDER + ri * _SQ
            color = _COL_LIGHT if (rank + file) % 2 == 1 else _COL_DARK
            draw.rectangle([x, y, x+_SQ, y+_SQ], fill=color)
            if last_move and sq in (last_move.from_square, last_move.to_square):
                ov = _Image.new("RGBA", (_SQ,_SQ), (30,130,50,130))
                img.paste(ov, (x,y), ov)
            piece = board.piece_at(sq)
            if (piece and piece.piece_type == _chess.KING
                    and piece.color == board.turn and board.is_check()):
                ov = _Image.new("RGBA", (_SQ,_SQ), (220,40,40,160))
                img.paste(ov, (x,y), ov)

    # Pass 2 — pieces
    draw2 = _ImageDraw.Draw(img, "RGBA")
    for ri, rank in enumerate(ranks):
        for fi, file in enumerate(files):
            sq    = _chess.square(file, rank)
            x     = _BORDER + fi * _SQ
            y     = _BORDER + ri * _SQ
            piece = board.piece_at(sq)
            if piece:
                _draw_piece(draw2, piece.piece_type, piece.color == _chess.WHITE,
                            x + _SQ//2, y + _SQ//2, _SQ)

    # Labels — gold on wood border
    rank_labels = "87654321" if not flipped else "12345678"
    file_labels = "abcdefgh"  if not flipped else "hgfedcba"
    for ri, label in enumerate(rank_labels):
        y = _BORDER + ri * _SQ + _SQ // 2
        draw2.text((_BORDER//2, y), label, font=lf, fill=_COL_LABEL, anchor="mm")
        draw2.text((_BSIZE-_BORDER//2, y), label, font=lf, fill=_COL_LABEL, anchor="mm")
    for fi, label in enumerate(file_labels):
        x = _BORDER + fi * _SQ + _SQ // 2
        draw2.text((x, _BORDER//2), label, font=lf, fill=_COL_LABEL, anchor="mm")
        draw2.text((x, _BSIZE-_BORDER//2), label, font=lf, fill=_COL_LABEL, anchor="mm")

    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


def _chess_file(game: dict) -> discord.File:
    """Render board as a discord.File ready to send."""
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
        status = "🤝 Stalemate — it\'s a draw!"
    elif board.is_insufficient_material():
        status = "🤝 Draw — insufficient material!"
    elif board.is_check():
        status = f"⚠️ **{turn_name}** is in CHECK!"

    if status:
        e.add_field(name="📢 Status", value=status, inline=False)
    if msg:
        e.add_field(name="📢", value=msg, inline=False)

    e.add_field(name="⬜ White", value=white.mention, inline=True)
    e.add_field(name="⬛ Black", value=black.mention, inline=True)
    e.add_field(name=f"{turn_emoji} Turn", value=f"**{turn_name}** to move", inline=True)
    e.set_footer(text=f"Move {board.fullmove_number} • Use the dropdowns below • !resign • !draw • !hint")
    return e


# ── Active chess games: channel_id → game dict ────────────────────────────────

active_chess: dict[int, dict] = {}


# ── Chess piece display constants ─────────────────────────────────────────────
_PIECE_NAMES = {
    _chess.PAWN: "Pawn", _chess.ROOK: "Rook", _chess.KNIGHT: "Knight",
    _chess.BISHOP: "Bishop", _chess.QUEEN: "Queen", _chess.KING: "King"
}
_PIECE_EMOJI = {
    _chess.PAWN:   ("♙", "♟"), _chess.ROOK:   ("♖", "♜"),
    _chess.KNIGHT: ("♘", "♞"), _chess.BISHOP: ("♗", "♝"),
    _chess.QUEEN:  ("♕", "♛"), _chess.KING:   ("♔", "♚"),
}

def _piece_label(piece: "_chess.Piece", square: int) -> str:
    """e.g. '♘ Knight on F3'"""
    color_idx = 0 if piece.color == _chess.WHITE else 1
    emoji     = _PIECE_EMOJI[piece.piece_type][color_idx]
    name      = _PIECE_NAMES[piece.piece_type]
    sq_name   = _chess.square_name(square).upper()
    return f"{emoji} {name} on {sq_name}"

def _square_label(board: "_chess.Board", move: "_chess.Move") -> str:
    """e.g. '→ F3' or '→ F7 (capture ♟)' or '→ G1 (castle)'"""
    to_sq  = move.to_square
    sq_name = _chess.square_name(to_sq).upper()
    target  = board.piece_at(to_sq)
    
    # Special move labels
    if board.is_castling(move):
        side = "Kingside" if _chess.square_file(to_sq) > 4 else "Queenside"
        return f"→ Castle {side}"
    if board.is_en_passant(move):
        return f"→ {sq_name} (en passant)"
    if move.promotion:
        promo = _PIECE_NAMES.get(move.promotion, "")
        return f"→ {sq_name} (promote to {promo})"
    if target:
        color_idx = 0 if target.color == _chess.WHITE else 1
        t_emoji   = _PIECE_EMOJI[target.piece_type][color_idx]
        return f"→ {sq_name} (capture {t_emoji})"
    return f"→ {sq_name}"

def _movable_pieces(board: "_chess.Board") -> list:
    """Return [(square, piece, [moves])] for all pieces with legal moves."""
    result = []
    for sq in _chess.SQUARES:
        piece = board.piece_at(sq)
        if not piece or piece.color != board.turn:
            continue
        moves = [m for m in board.legal_moves if m.from_square == sq]
        if moves:
            result.append((sq, piece, moves))
    return result



# ── Chess Views ───────────────────────────────────────────────────────────────

class ChessPieceSelect(discord.ui.View):
    """Step 1 — player picks which piece to move."""

    def __init__(self, game: dict, player: discord.Member):
        super().__init__(timeout=120)
        self.game   = game
        self.player = player
        self._build()

    def _build(self):
        board   = self.game["board"]
        pieces  = _movable_pieces(board)
        options = []
        for sq, piece, moves in pieces[:25]:
            label = _piece_label(piece, sq)
            options.append(discord.SelectOption(label=label, value=str(sq)))

        select = discord.ui.Select(
            placeholder="Select a piece to move…",
            options=options,
            custom_id="piece_select",
        )
        select.callback = self._on_piece_select
        self.add_item(select)

    async def _on_piece_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("❌ It\'s not your turn!", ephemeral=True)
            return

        from_sq = int(interaction.data["values"][0])
        board   = self.game["board"]
        piece   = board.piece_at(from_sq)
        moves   = [m for m in board.legal_moves if m.from_square == from_sq]

        view = ChessDestSelect(self.game, self.player, from_sq, moves)
        embed = _chess_embed(
            self.game,
            msg=f"Selected: **{_piece_label(piece, from_sq)}**\nNow choose where to move it:"
        )
        await interaction.response.edit_message(embed=embed, view=view)

    async def on_timeout(self):
        pass


class ChessDestSelect(discord.ui.View):
    """Step 2 — player picks the destination square."""

    def __init__(self, game: dict, player: discord.Member, from_sq: int, moves: list):
        super().__init__(timeout=120)
        self.game    = game
        self.player  = player
        self.from_sq = from_sq
        self.moves   = moves
        self._build()

    def _build(self):
        board   = self.game["board"]
        options = []
        seen    = set()
        for move in self.moves[:25]:
            label = _square_label(board, move)
            val   = move.uci()
            if val not in seen:
                seen.add(val)
                options.append(discord.SelectOption(label=label, value=val))

        select = discord.ui.Select(
            placeholder="Choose destination…",
            options=options,
            custom_id="dest_select",
        )
        select.callback = self._on_dest_select
        self.add_item(select)

        # Back button
        back = discord.ui.Button(label="← Back", style=discord.ButtonStyle.secondary)
        back.callback = self._on_back
        self.add_item(back)

    async def _on_dest_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("❌ It\'s not your turn!", ephemeral=True)
            return

        move_uci = interaction.data["values"][0]
        board    = self.game["board"]
        move     = _chess.Move.from_uci(move_uci)
        board.push(move)
        self.game["draw_offered_by"] = None

        # Check end conditions
        if board.is_checkmate():
            winner = self.game["black"] if board.turn == _chess.WHITE else self.game["white"]
            embed  = _chess_embed(self.game, title="♟️ Checkmate!")
            del active_chess[interaction.channel_id]
            await interaction.response.edit_message(
                content=f"🏆 **{winner.display_name}** wins by checkmate!",
                embed=embed, attachments=[_chess_file(self.game)], view=None
            )
            return

        if board.is_stalemate() or board.is_insufficient_material() or board.is_seventyfive_moves():
            embed = _chess_embed(self.game, title="♟️ Draw!")
            del active_chess[interaction.channel_id]
            await interaction.response.edit_message(content="🤝 The game is a draw!", embed=embed, attachments=[_chess_file(self.game)], view=None)
            return

        # Next player\'s turn
        next_player = self.game["white"] if board.turn == _chess.WHITE else self.game["black"]
        status_msg  = "⚠️ Check! Your king is under attack." if board.is_check() else f"Move played. Your turn, {next_player.mention}!"
        embed       = _chess_embed(self.game, msg=status_msg)
        next_view   = ChessPieceSelect(self.game, next_player)
        await interaction.response.edit_message(
            content=f"{next_player.mention} — it\'s your turn!",
            embed=embed,
            attachments=[_chess_file(self.game)],
            view=next_view,
        )

    async def _on_back(self, interaction: discord.Interaction):
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("❌ It\'s not your turn!", ephemeral=True)
            return
        view  = ChessPieceSelect(self.game, self.player)
        embed = _chess_embed(self.game, msg="Choose a piece to move:")
        await interaction.response.edit_message(embed=embed, view=view)

    async def on_timeout(self):
        pass


# game dict keys:
#   board   : chess.Board
#   white   : discord.Member
#   black   : discord.Member
#   flipped : bool  (black sees flipped board)
#   draw_offered_by : int | None  (user_id)


# ── Cog ───────────────────────────────────────────────────────────────────────

class Fun(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /hangman ──────────────────────────────────────────────────────────────

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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        cid = message.channel.id
        if cid not in active_hangman:
            return
        content = message.content.strip().lower()
        if len(content) != 1 or not content.isalpha():
            return
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

    @app_commands.command(name="stophangman", description="Stop the current hangman game")
    async def slash_stophangman(self, interaction: discord.Interaction):
        if interaction.channel_id not in active_hangman:
            await interaction.response.send_message("⚠️ No hangman game is running here.", ephemeral=True)
            return
        state = active_hangman.pop(interaction.channel_id)
        await interaction.response.send_message(f"🛑 Hangman stopped. The word was **{state['word']}**.")


    # ╔══════════════════════════════════════════════════════════════╗
    # ║  CHESS                                                       ║
    # ╚══════════════════════════════════════════════════════════════╝

    @app_commands.command(name="chess", description="Challenge someone to a game of chess ♟️")
    @app_commands.describe(opponent="The user you want to play against")
    async def slash_chess(self, interaction: discord.Interaction, opponent: discord.Member):
        await self._start_chess(interaction.channel, interaction.user, opponent,
                                 reply_fn=lambda **kw: interaction.response.send_message(**kw))

    @commands.command(name="chess")
    async def prefix_chess(self, ctx: commands.Context, opponent: discord.Member = None):
        if not opponent:
            await ctx.reply("Usage: `!chess @user`")
            return
        await self._start_chess(ctx.channel, ctx.author, opponent,
                                 reply_fn=lambda **kw: ctx.reply(**kw))

    async def _start_chess(self, channel, challenger, opponent, reply_fn):
        if channel.id in active_chess:
            await reply_fn(content="⚠️ A chess game is already running in this channel. Use `!stopchess` to end it.", ephemeral=True)
            return
        if opponent.bot:
            await reply_fn(content="❌ You can\'t play chess against a bot (yet).", ephemeral=True)
            return
        if opponent.id == challenger.id:
            await reply_fn(content="❌ You can\'t play against yourself.", ephemeral=True)
            return

        # Randomly assign colours
        if random.random() < 0.5:
            white, black = challenger, opponent
        else:
            white, black = opponent, challenger

        game = {
            "board":            _chess.Board(),
            "white":            white,
            "black":            black,
            "flipped":          False,
            "draw_offered_by":  None,
        }
        active_chess[channel.id] = game

        embed = _chess_embed(game, title="♟️ Chess — Game Start",
                             msg=f"{white.mention} ⬜ vs {black.mention} ⬛\n\n**{white.display_name}** goes first!\nUse the menu below to pick a piece, then choose where to move it.")
        view = ChessPieceSelect(game, white)
        await reply_fn(content=f"🎮 {white.mention} vs {black.mention} — {white.mention} it\'s your turn!", embed=embed, file=_chess_file(game), view=view)

    @commands.command(name="move")
    async def prefix_move(self, ctx: commands.Context, move: str = None):
        """Advanced: !move e2e4 — for players who know chess notation."""
        if not move:
            await ctx.reply("Usage: `!move e2e4` (UCI notation) — or just use the dropdown menus!")
            return
        game = active_chess.get(ctx.channel.id)
        if not game:
            await ctx.reply("❌ No chess game running here.")
            return
        board      = game["board"]
        whose_turn = game["white"] if board.turn == _chess.WHITE else game["black"]
        if ctx.author.id != whose_turn.id:
            await ctx.reply(f"❌ It\'s **{whose_turn.display_name}\'s** turn.")
            return
        try:
            chess_move = _chess.Move.from_uci(move.lower())
            if chess_move not in board.legal_moves:
                raise ValueError()
        except Exception:
            try:
                chess_move = board.parse_san(move)
            except Exception:
                await ctx.reply(f"❌ Invalid move `{move}`. Use the dropdown menus or UCI notation like `e2e4`.")
                return
        board.push(chess_move)
        game["draw_offered_by"] = None
        if board.is_checkmate():
            winner = game["black"] if board.turn == _chess.WHITE else game["white"]
            embed  = _chess_embed(game, title="♟️ Checkmate!")
            del active_chess[ctx.channel.id]
            await ctx.reply(content=f"🏆 **{winner.display_name}** wins!", embed=embed)
            return
        if board.is_stalemate() or board.is_insufficient_material():
            embed = _chess_embed(game, title="♟️ Draw!")
            del active_chess[ctx.channel.id]
            await ctx.reply(content="🤝 Draw!", embed=embed)
            return
        next_player = game["white"] if board.turn == _chess.WHITE else game["black"]
        status_msg  = "⚠️ Check!" if board.is_check() else f"Move `{chess_move.uci()}` played."
        embed       = _chess_embed(game, msg=status_msg)
        view        = ChessPieceSelect(game, next_player)
        await ctx.reply(content=f"{next_player.mention} your turn!", embed=embed, file=_chess_file(game), view=view)

    @app_commands.command(name="resign", description="Resign your current chess game")
    async def slash_resign(self, interaction: discord.Interaction):
        await self._handle_resign(interaction.channel, interaction.user,
                                   reply_fn=lambda **kw: interaction.response.send_message(**kw))

    @commands.command(name="resign")
    async def prefix_resign(self, ctx: commands.Context):
        await self._handle_resign(ctx.channel, ctx.author,
                                   reply_fn=lambda **kw: ctx.reply(**kw))

    async def _handle_resign(self, channel, user, reply_fn):
        game = active_chess.get(channel.id)
        if not game:
            await reply_fn(content="❌ No chess game running here.", ephemeral=True)
            return
        if user.id not in (game["white"].id, game["black"].id):
            await reply_fn(content="❌ You\'re not in this game.", ephemeral=True)
            return
        winner = game["black"] if user.id == game["white"].id else game["white"]
        del active_chess[channel.id]
        embed = _chess_embed(game, title="♟️ Resignation")
        await reply_fn(content=f"🏳️ **{user.display_name}** resigned. **{winner.display_name}** wins!", embed=embed, file=_chess_file(game))

    @app_commands.command(name="draw", description="Offer or accept a draw in chess")
    async def slash_draw(self, interaction: discord.Interaction):
        await self._handle_draw(interaction.channel, interaction.user,
                                 reply_fn=lambda **kw: interaction.response.send_message(**kw))

    @commands.command(name="draw")
    async def prefix_draw(self, ctx: commands.Context):
        await self._handle_draw(ctx.channel, ctx.author,
                                 reply_fn=lambda **kw: ctx.reply(**kw))

    async def _handle_draw(self, channel, user, reply_fn):
        game = active_chess.get(channel.id)
        if not game:
            await reply_fn(content="❌ No chess game running here.", ephemeral=True)
            return
        if user.id not in (game["white"].id, game["black"].id):
            await reply_fn(content="❌ You\'re not in this game.", ephemeral=True)
            return

        offered = game["draw_offered_by"]
        if offered is None:
            game["draw_offered_by"] = user.id
            opponent = game["black"] if user.id == game["white"].id else game["white"]
            await reply_fn(content=f"🤝 **{user.display_name}** offers a draw. {opponent.mention} type `!draw` to accept.")
            return

        if offered == user.id:
            await reply_fn(content="⏳ You already offered a draw. Waiting for your opponent to accept.", ephemeral=True)
            return

        # Opponent accepts
        del active_chess[channel.id]
        embed = _chess_embed(game, title="♟️ Draw Agreed")
        await reply_fn(content="🤝 Both players agreed to a draw!", embed=embed, file=_chess_file(game))

    @app_commands.command(name="hint", description="Get an AI hint for your chess position")
    async def slash_hint(self, interaction: discord.Interaction):
        await self._handle_hint(interaction.channel, interaction.user,
                                 reply_fn=lambda **kw: interaction.response.send_message(**kw),
                                 defer_fn=lambda: interaction.response.defer(ephemeral=True))

    @commands.command(name="hint")
    async def prefix_hint(self, ctx: commands.Context):
        await self._handle_hint(ctx.channel, ctx.author,
                                 reply_fn=lambda **kw: ctx.reply(**kw),
                                 defer_fn=ctx.typing)

    async def _handle_hint(self, channel, user, reply_fn, defer_fn):
        game = active_chess.get(channel.id)
        if not game:
            await reply_fn(content="❌ No chess game running here.", ephemeral=True)
            return
        whose_turn = game["white"] if game["board"].turn == _chess.WHITE else game["black"]
        if user.id != whose_turn.id:
            await reply_fn(content="❌ It\'s not your turn.", ephemeral=True)
            return

        async with channel.typing():
            board = game["board"]
            legal = [m.uci() for m in list(board.legal_moves)[:20]]
            colour = "White" if board.turn == _chess.WHITE else "Black"
            prompt = (
                f"You are a chess coach. The current position FEN is: {board.fen()}\n"
                f"It is {colour}\'s turn. Legal moves include: {', '.join(legal)}.\n"
                f"Suggest the best move and explain why in 2-3 sentences. Be concise."
            )
            hint = await generate_ai_response(user.id, prompt, channel.guild.id if channel.guild else None)
        await reply_fn(content=f"💡 **Chess Hint:**\n{hint}")

    @app_commands.command(name="stopchess", description="Stop the current chess game")
    async def slash_stopchess(self, interaction: discord.Interaction):
        await self._handle_stopchess(interaction.channel, interaction.user,
                                      reply_fn=lambda **kw: interaction.response.send_message(**kw))

    @commands.command(name="stopchess")
    async def prefix_stopchess(self, ctx: commands.Context):
        await self._handle_stopchess(ctx.channel, ctx.author,
                                      reply_fn=lambda **kw: ctx.reply(**kw))

    async def _handle_stopchess(self, channel, user, reply_fn):
        game = active_chess.get(channel.id)
        if not game:
            await reply_fn(content="❌ No chess game running here.", ephemeral=True)
            return
        is_player  = user.id in (game["white"].id, game["black"].id)
        is_admin   = channel.permissions_for(user).manage_messages
        if not (is_player or is_admin):
            await reply_fn(content="❌ Only players or admins can stop the game.", ephemeral=True)
            return
        del active_chess[channel.id]
        await reply_fn(content="🛑 Chess game stopped.")

    @app_commands.command(name="chessboard", description="Show the current chess board")
    async def slash_chessboard(self, interaction: discord.Interaction):
        game = active_chess.get(interaction.channel_id)
        if not game:
            await interaction.response.send_message("❌ No chess game running here.", ephemeral=True)
            return
        await interaction.response.send_message(embed=_chess_embed(game), file=_chess_file(game))

    @commands.command(name="chessboard", aliases=["board"])
    async def prefix_chessboard(self, ctx: commands.Context):
        game = active_chess.get(ctx.channel.id)
        if not game:
            await ctx.reply("❌ No chess game running here.")
            return
        whose_turn = game["white"] if game["board"].turn == _chess.WHITE else game["black"]
        view = ChessPieceSelect(game, whose_turn)
        await ctx.reply(embed=_chess_embed(game), file=_chess_file(game), view=view)


    # ── /compliment ───────────────────────────────────────────────────────────

    @app_commands.command(name="compliment", description="Send someone a genuine AI compliment 💛")
    @app_commands.describe(user="The user to compliment")
    async def slash_compliment(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer()
        prompt = (
            f"Give a single warm, genuine, and creative compliment for a Discord user named '{user.display_name}'. "
            f"Make it feel heartfelt and unique — not generic. One short paragraph max."
        )
        reply = await generate_ai_response(interaction.user.id, prompt, interaction.guild_id)
        embed = discord.Embed(description=f"💛 {reply}", color=discord.Color.yellow())
        embed.set_author(name=f"A compliment for {user.display_name}", icon_url=user.display_avatar.url)
        embed.set_footer(text=f"Sent by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    @commands.command(name="compliment")
    async def prefix_compliment(self, ctx: commands.Context, user: discord.Member = None):
        if not user:
            await ctx.reply("Usage: `!compliment @user`")
            return
        async with ctx.typing():
            prompt = (
                f"Give a single warm, genuine, and creative compliment for a Discord user named '{user.display_name}'. "
                f"Make it feel heartfelt and unique — not generic. One short paragraph max."
            )
            reply = await generate_ai_response(ctx.author.id, prompt, ctx.guild.id if ctx.guild else None)
        embed = discord.Embed(description=f"💛 {reply}", color=discord.Color.yellow())
        embed.set_author(name=f"A compliment for {user.display_name}", icon_url=user.display_avatar.url)
        await ctx.reply(embed=embed)


# ── Hangman embed helper ──────────────────────────────────────────────────────

def _hangman_embed(state: dict, finished: bool = False) -> discord.Embed:
    word    = state["word"]
    guessed = state["guessed"]
    wrong   = state["wrong"]
    lives_left = 6 - wrong

    # Word display — spaced letters, revealed in bold
    display = "  ".join(f"**{c.upper()}**" if c in guessed else "﹏" for c in word)

    # Lives as hearts
    hearts = "❤️" * lives_left + "🖤" * wrong

    # Wrong letters as red cross emoji badges
    wrong_letters = guessed - set(word)
    wrong_display = "  ".join(f"~~{l.upper()}~~" for l in sorted(wrong_letters)) or "None yet"

    # Progress bar
    total     = len(word)
    revealed  = sum(1 for c in word if c in guessed)
    filled    = round((revealed / total) * 10)
    bar       = "█" * filled + "░" * (10 - filled)
    progress  = f"`[{bar}]` {revealed}/{total}"

    color = HANGMAN_COLORS[min(wrong, 6)] if not finished else discord.Color.greyple()

    embed = discord.Embed(title="🪢  H A N G M A N", color=color)
    embed.description = HANGMAN_STAGES[min(wrong, 6)]
    embed.add_field(name="Word", value=display, inline=False)
    embed.add_field(name=" Lives", value=hearts, inline=True)
    embed.add_field(name="📏 Length", value=f"`{len(word)}` letters", inline=True)
    embed.add_field(name="❌ Wrong Guesses", value=wrong_display, inline=False)
    embed.add_field(name="📊 Progress", value=progress, inline=False)
    if not finished:
        embed.set_footer(text="✏️  Type a single letter to guess!")
    return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(Fun(bot))