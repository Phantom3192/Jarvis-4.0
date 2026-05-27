import discord
from discord.ext import commands
from discord import app_commands
import random
import asyncio
import html
from cogs.ai import generate_ai_response
from cogs.http_session import get_session

# ── Constants ─────────────────────────────────────────────────────────────────

HANGMAN_STAGES = [
    "```\n  +---+\n  |   |\n      |\n      |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n      |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n  |   |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|   |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n /    |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n / \\  |\n      |\n=========```",
]

HANGMAN_WORDS = [
    "python", "discord", "robot", "galaxy", "jarvis", "keyboard", "asteroid",
    "phantom", "thunder", "wizard", "castle", "penguin", "lantern", "tornado",
    "dolphin", "volcano", "muffin", "crystal", "dragon", "shadow", "mirror",
    "pirate", "jungle", "rocket", "cobalt", "marble", "falcon", "puzzle",
]

TRUTH_QUESTIONS = [
    "What's the most embarrassing thing you've done online?",
    "Have you ever ghosted someone? Why?",
    "What's a secret talent you've never told anyone?",
    "What's the weirdest dream you've ever had?",
    "Have you ever lied to get out of plans? What did you say?",
    "What's the most childish thing you still do?",
    "Who was your first crush?",
    "What's the worst gift you've ever received?",
    "Have you ever cheated on a test?",
    "What's one thing you'd change about yourself?",
    "What's the most useless talent you have?",
    "What show do you watch but pretend you don't?",
    "What's the pettiest thing you've ever done?",
    "Have you ever pretended to be busy to avoid someone?",
    "What's the last lie you told?",
]

DARE_CHALLENGES = [
    "Type a message using only your elbows.",
    "Send the last photo in your camera roll (no deleting first!).",
    "Write a 3-sentence love poem about your least favourite food.",
    "Do your best impression of a famous person in voice chat.",
    "Change your nickname to whatever the next person says for 10 minutes.",
    "Speak in rhymes for the next 5 minutes.",
    "Send a compliment to the last person you texted.",
    "Try to lick your elbow and describe the experience.",
    "Write a haiku about someone in this server.",
    "Say the alphabet backwards as fast as you can.",
    "Describe yourself using only emoji for the next 3 messages.",
    "Type your next message with your eyes closed.",
    "Sing the chorus of a song in voice chat.",
    "Do 10 push-ups and come back with proof.",
    "Send a voice message saying 'I am a golden retriever' three times.",
]

FUN_FACTS = [
    "🐙 Octopuses have three hearts, nine brains, and blue blood.",
    "🍯 Honey never spoils — archaeologists found 3,000-year-old honey in Egyptian tombs that was still edible.",
    "🌙 The Moon is slowly drifting away from Earth at about 3.8 cm per year.",
    "🐧 Penguins propose to their mates with a pebble.",
    "⚡ A bolt of lightning is five times hotter than the surface of the Sun.",
    "🦈 Sharks are older than trees — they've existed for over 400 million years.",
    "🧠 Your brain generates enough electricity to power a small LED light.",
    "🐘 Elephants are the only animals that can't jump.",
    "🌊 More than 80% of Earth's oceans remain unexplored.",
    "🦋 Butterflies taste with their feet.",
    "🍕 The world's most expensive pizza costs $12,000 and takes 72 hours to make.",
    "🚀 In space, astronauts can grow up to 2 inches taller due to spine decompression.",
    "🐝 A single bee will produce only 1/12th of a teaspoon of honey in its entire lifetime.",
    "🌍 There are more possible chess games than atoms in the observable universe.",
    "🦜 African grey parrots can have the emotional intelligence of a 5-year-old child.",
    "🔥 Hot water can freeze faster than cold water — this is called the Mpemba effect.",
    "🧬 You share 60% of your DNA with a banana.",
    "🐢 A group of flamingos is called a 'flamboyance'.",
    "💤 Humans spend about 26 years of their life sleeping.",
    "🎵 Music can help plants grow faster.",
]

WYR_QUESTIONS = [
    ("be able to fly", "be invisible"),
    ("never use social media again", "never watch movies/TV again"),
    ("know when you'll die", "know how you'll die"),
    ("have unlimited money", "have unlimited time"),
    ("live in the past", "live in the future"),
    ("be the funniest person in the room", "be the smartest person in the room"),
    ("lose all your memories", "never make new ones"),
    ("be able to talk to animals", "speak every human language"),
    ("always be 10 minutes late", "always be 20 minutes early"),
    ("give up the internet", "give up all cooked food"),
    ("fight 100 duck-sized horses", "fight 1 horse-sized duck"),
    ("have no fingers", "have no elbows"),
    ("always have to whisper", "always have to shout"),
    ("be famous but broke", "be rich but completely unknown"),
    ("know all the world's languages", "know how to play every instrument"),
]

# ── Active game tracking ──────────────────────────────────────────────────────

active_hangman: dict[int, dict] = {}
active_trivia:  dict[int, dict] = {}


# ── Trivia helper ─────────────────────────────────────────────────────────────

async def _fetch_trivia() -> dict | None:
    url = "https://opentdb.com/api.php?amount=1&type=multiple"
    try:
        session = get_session()
        async with session.get(url, timeout=5) as resp:
            data = await resp.json()
            if data["response_code"] == 0:
                return data["results"][0]
    except Exception:
        pass
    return None


# ── Trivia view ───────────────────────────────────────────────────────────────

class TriviaView(discord.ui.View):
    def __init__(self, correct: str, options: list[str], channel_id: int):
        super().__init__(timeout=20)
        self.correct    = correct
        self.channel_id = channel_id
        self.answered   = False

        labels = ["A", "B", "C", "D"]
        for i, opt in enumerate(options):
            btn = discord.ui.Button(label=f"{labels[i]}. {opt[:60]}", style=discord.ButtonStyle.primary, custom_id=opt)
            btn.callback = self._make_callback(opt)
            self.add_item(btn)

    def _make_callback(self, choice: str):
        async def callback(interaction: discord.Interaction):
            if self.answered:
                await interaction.response.send_message("⚡ Someone already answered!", ephemeral=True)
                return
            self.answered = True
            for item in self.children:
                item.disabled = True
                if isinstance(item, discord.ui.Button):
                    if item.custom_id == self.correct:
                        item.style = discord.ButtonStyle.success
                    elif item.custom_id == choice and choice != self.correct:
                        item.style = discord.ButtonStyle.danger
            if choice == self.correct:
                msg = f"✅ **{interaction.user.display_name}** got it! The answer was **{self.correct}**."
            else:
                msg = f"❌ **{interaction.user.display_name}** guessed wrong. The answer was **{self.correct}**."
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(msg)
            active_trivia.pop(self.channel_id, None)
            self.stop()
        return callback

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        active_trivia.pop(self.channel_id, None)


# ── Cog ───────────────────────────────────────────────────────────────────────

class Fun(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /trivia ───────────────────────────────────────────────────────────────

    @app_commands.command(name="trivia", description="Start a trivia question — first to answer wins!")
    async def slash_trivia(self, interaction: discord.Interaction):
        if interaction.channel_id in active_trivia:
            await interaction.response.send_message("⚠️ A trivia game is already running in this channel!", ephemeral=True)
            return
        await interaction.response.defer()
        q = await _fetch_trivia()
        if not q:
            await interaction.followup.send("⚠️ Couldn't fetch a trivia question. Try again in a moment.")
            return
        question  = html.unescape(q["question"])
        correct   = html.unescape(q["correct_answer"])
        incorrect = [html.unescape(a) for a in q["incorrect_answers"]]
        options   = incorrect + [correct]
        random.shuffle(options)
        embed = discord.Embed(title="🧠 Trivia Time!", description=question, color=discord.Color.blurple())
        embed.add_field(name="Category",   value=q["category"],              inline=True)
        embed.add_field(name="Difficulty", value=q["difficulty"].capitalize(), inline=True)
        embed.set_footer(text="You have 20 seconds — first to answer wins!")
        view = TriviaView(correct, options, interaction.channel_id)
        active_trivia[interaction.channel_id] = True
        await interaction.followup.send(embed=embed, view=view)

    @commands.command(name="trivia")
    async def prefix_trivia(self, ctx: commands.Context):
        if ctx.channel.id in active_trivia:
            await ctx.reply("⚠️ A trivia game is already running in this channel!")
            return
        async with ctx.typing():
            q = await _fetch_trivia()
        if not q:
            await ctx.reply("⚠️ Couldn't fetch a trivia question. Try again in a moment.")
            return
        question  = html.unescape(q["question"])
        correct   = html.unescape(q["correct_answer"])
        incorrect = [html.unescape(a) for a in q["incorrect_answers"]]
        options   = incorrect + [correct]
        random.shuffle(options)
        embed = discord.Embed(title="🧠 Trivia Time!", description=question, color=discord.Color.blurple())
        embed.add_field(name="Category",   value=q["category"],              inline=True)
        embed.add_field(name="Difficulty", value=q["difficulty"].capitalize(), inline=True)
        embed.set_footer(text="You have 20 seconds — first to answer wins!")
        view = TriviaView(correct, options, ctx.channel.id)
        active_trivia[ctx.channel.id] = True
        await ctx.reply(embed=embed, view=view)

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

    # ── /roast ────────────────────────────────────────────────────────────────

    @app_commands.command(name="roast", description="Roast a user with a witty AI burn 🔥")
    @app_commands.describe(user="The user to roast")
    async def slash_roast(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer()
        prompt = (
            f"Give a single witty, creative, and funny roast of a Discord user named '{user.display_name}'. "
            f"Keep it playful and not genuinely hurtful — more like a comedy roast. "
            f"One short paragraph max. Don't start with 'Oh' or 'Ah'."
        )
        reply = await generate_ai_response(interaction.user.id, prompt, interaction.guild_id)
        embed = discord.Embed(description=f"🔥 {reply}", color=discord.Color.orange())
        embed.set_author(name=f"Roasting {user.display_name}", icon_url=user.display_avatar.url)
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    @commands.command(name="roast")
    async def prefix_roast(self, ctx: commands.Context, user: discord.Member = None):
        if not user:
            await ctx.reply("Usage: `!roast @user`")
            return
        async with ctx.typing():
            prompt = (
                f"Give a single witty, creative, and funny roast of a Discord user named '{user.display_name}'. "
                f"Keep it playful and not genuinely hurtful — more like a comedy roast. "
                f"One short paragraph max. Don't start with 'Oh' or 'Ah'."
            )
            reply = await generate_ai_response(ctx.author.id, prompt, ctx.guild.id if ctx.guild else None)
        embed = discord.Embed(description=f"🔥 {reply}", color=discord.Color.orange())
        embed.set_author(name=f"Roasting {user.display_name}", icon_url=user.display_avatar.url)
        await ctx.reply(embed=embed)

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

    # ── /wyr ──────────────────────────────────────────────────────────────────

    @app_commands.command(name="wyr", description="Would you rather…?")
    async def slash_wyr(self, interaction: discord.Interaction):
        a, b = random.choice(WYR_QUESTIONS)
        embed = discord.Embed(title="🤔 Would You Rather…", color=discord.Color.purple())
        embed.add_field(name="Option A", value=f"🅰️ {a.capitalize()}", inline=False)
        embed.add_field(name="Option B", value=f"🅱️ {b.capitalize()}", inline=False)
        embed.set_footer(text="Reply with A or B!")
        await interaction.response.send_message(embed=embed)

    @commands.command(name="wyr")
    async def prefix_wyr(self, ctx: commands.Context):
        a, b = random.choice(WYR_QUESTIONS)
        embed = discord.Embed(title="🤔 Would You Rather…", color=discord.Color.purple())
        embed.add_field(name="Option A", value=f"🅰️ {a.capitalize()}", inline=False)
        embed.add_field(name="Option B", value=f"🅱️ {b.capitalize()}", inline=False)
        embed.set_footer(text="Reply with A or B!")
        await ctx.reply(embed=embed)

    # ── /truth & /dare ────────────────────────────────────────────────────────

    @app_commands.command(name="truth", description="Get a truth question 👀")
    async def slash_truth(self, interaction: discord.Interaction):
        q = random.choice(TRUTH_QUESTIONS)
        embed = discord.Embed(title="🫣 Truth!", description=q, color=discord.Color.teal())
        embed.set_footer(text=f"Asked to {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)

    @commands.command(name="truth")
    async def prefix_truth(self, ctx: commands.Context):
        q = random.choice(TRUTH_QUESTIONS)
        embed = discord.Embed(title="🫣 Truth!", description=q, color=discord.Color.teal())
        embed.set_footer(text=f"Asked to {ctx.author.display_name}")
        await ctx.reply(embed=embed)

    @app_commands.command(name="dare", description="Get a dare challenge 😈")
    async def slash_dare(self, interaction: discord.Interaction):
        d = random.choice(DARE_CHALLENGES)
        embed = discord.Embed(title="😈 Dare!", description=d, color=discord.Color.red())
        embed.set_footer(text=f"Dared to {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)

    @commands.command(name="dare")
    async def prefix_dare(self, ctx: commands.Context):
        d = random.choice(DARE_CHALLENGES)
        embed = discord.Embed(title="😈 Dare!", description=d, color=discord.Color.red())
        embed.set_footer(text=f"Dared to {ctx.author.display_name}")
        await ctx.reply(embed=embed)

    # ── /funfact ──────────────────────────────────────────────────────────────

    @app_commands.command(name="funfact", description="Get a random fun fact 🤯")
    async def slash_funfact(self, interaction: discord.Interaction):
        fact = random.choice(FUN_FACTS)
        embed = discord.Embed(title="🤯 Fun Fact!", description=fact, color=discord.Color.green())
        await interaction.response.send_message(embed=embed)

    @commands.command(name="funfact")
    async def prefix_funfact(self, ctx: commands.Context):
        fact = random.choice(FUN_FACTS)
        embed = discord.Embed(title="🤯 Fun Fact!", description=fact, color=discord.Color.green())
        await ctx.reply(embed=embed)


# ── Hangman embed helper ──────────────────────────────────────────────────────

def _hangman_embed(state: dict, finished: bool = False) -> discord.Embed:
    word    = state["word"]
    guessed = state["guessed"]
    wrong   = state["wrong"]
    display = " ".join(c if c in guessed else "\\_" for c in word)
    wrong_letters = ", ".join(sorted(g for g in guessed if g not in word)) or "None"
    embed = discord.Embed(
        title="🪢 Hangman",
        color=discord.Color.blurple() if not finished else discord.Color.greyple(),
    )
    embed.description = HANGMAN_STAGES[min(wrong, 6)]
    embed.add_field(name="Word",          value=f"`{display}`",      inline=False)
    embed.add_field(name="Wrong guesses", value=f"`{wrong_letters}`", inline=True)
    embed.add_field(name="Lives left",    value=f"`{6 - wrong}/6`",  inline=True)
    if not finished:
        embed.set_footer(text="Type a single letter to guess!")
    return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(Fun(bot))