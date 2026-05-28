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