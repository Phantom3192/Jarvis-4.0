import discord
from discord.ext import commands
from discord import app_commands

# ── Category definitions ──────────────────────────────────────────────────────
# Each command is listed ONCE. Both ! and / variants are shown on one line.

CATEGORIES = {
    "🤖 AI": {
        "description": "Chat with Jarvis using AI.",
        "color": 0x5865F2,
        "commands": [
            ("!chat  /chat",          "Chat with Jarvis — supports image attachments"),
            ("@mention / reply",      "Mention Jarvis or reply to his messages to chat"),
            ("!mylimit  /mylimit",    "Check how many AI messages you have left today"),
        ],
    },
    "🧠 Memory": {
        "description": "Jarvis remembers things about you across sessions.",
        "color": 0x9B59B6,
        "commands": [
            ("@Jarvis call me X",           "Tell Jarvis what to call you"),
            ("@Jarvis remember that …",     "Explicitly save a fact about yourself"),
            ("!mymemory  /mymemory",        "View everything Jarvis remembers about you"),
            ("!forgetme  /forgetme",        "Wipe all your saved memory"),
        ],
    },
    "🎨 Image Gen": {
        "description": "Generate AI images from text prompts — free, no limits.",
        "color": 0xE91E63,
        "commands": [
            ("!imagine  /imagine <prompt>",  "Generate an image from a text prompt"),
            ("--style <name>",               "Style flag: anime · realistic · pixel · cartoon · sketch · watercolor · cinematic · fantasy"),
        ],
    },
    "🎉 Fun": {
        "description": "Fun commands to mess around with.",
        "color": 0xF1C40F,
        "commands": [
            ("!trivia  /trivia",             "Random trivia question to test your knowledge"),
            ("!hangman  /hangman",           "Classic word guessing — 6 lives, type letters to guess"),
            ("/stophangman",                 "Stop the current hangman game in this channel"),
            ("!roast  /roast @user",         "Roast someone with a witty insult"),
            ("!compliment  /compliment @user", "Send someone a nice compliment"),
            ("!wyr  /wyr",                   "Random Would You Rather question"),
            ("!truth  /truth",               "Get a random truth question"),
            ("!dare  /dare",                 "Get a random dare"),
            ("!funfact  /funfact",           "Drop a random fun fact"),
        ],
    },
    "📊 Stats": {
        "description": "View your Jarvis usage stats.",
        "color": 0x2ECC71,
        "commands": [
            ("!stats  /stats",        "View your own usage stats"),
            ("!stats @user",          "View another user's stats (admins only)"),
        ],
    },
    "💡 Feedback": {
        "description": "Send feedback or bug reports to the devs.",
        "color": 0x1ABC9C,
        "commands": [
            ("!feedback  /feedback",      "Submit a suggestion or feedback"),
            ("!bugreport  /bugreport",    "Report a bug you found"),
        ],
    },
    "🎨 Prompts": {
        "description": "Customise Jarvis's personality for your server.",
        "color": 0xE67E22,
        "commands": [
            ("!setprompt  /setprompt",       "Set a custom AI system prompt (Manage Server)"),
            ("!resetprompt  /resetprompt",   "Reset back to the default prompt"),
            ("!viewprompt  /viewprompt",     "View the current system prompt"),
        ],
    },
    "🖥️ System": {
        "description": "Bot and server system info.",
        "color": 0x95A5A6,
        "commands": [
            ("!ping  /ping",          "Check Jarvis latency"),
            ("!uptime  /uptime",      "See how long Jarvis has been online"),
            ("!guildinfo  /guildinfo","Detailed info about this server"),
            ("!servers  /servers",    "List all servers Jarvis is in (admin only)"),
            ("!usage  /usage",        "CPU, RAM and disk stats (admin only)"),
        ],
    },
    "📢 Announce": {
        "description": "Send announcements or DMs (admins only).",
        "color": 0x3498DB,
        "commands": [
            ("!global-announce  /global-announce", "DM announcement to all Jarvis users"),
            ("!announce  /announce @user",         "Send a DM to a specific user"),
        ],
    },
    "🛡️ Admin": {
        "description": "Bot moderation tools (admins only).",
        "color": 0xE74C3C,
        "commands": [
            ("!global-ban  /botban @user",      "Ban a user from using Jarvis"),
            ("!global-unban  /botunban <id>",   "Unban a user"),
            ("!global-bans  /botbans",          "List all banned users"),
            ("!guild-ban  /guildban <id>",      "Ban an entire server from Jarvis"),
            ("!guild-unban  /guildunban <id>",  "Unban a server"),
            ("!guild-bans  /guildbans",         "List all guild-banned servers"),
            ("!resetlimit  /resetlimit @user",  "Reset a user's daily message limit"),
        ],
    },
}


# ── Embed builders ────────────────────────────────────────────────────────────

def _build_overview_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📖 Jarvis — Command Help",
        description=(
            "Hey! Pick a category below to see its commands.\n"
            "Both `!prefix` and `/slash` versions are shown together — no duplicates.\n\u200b"
        ),
        color=0x5865F2,
    )
    for cat, data in CATEGORIES.items():
        embed.add_field(
            name=cat,
            value=data["description"],
            inline=True,
        )
    embed.set_footer(text="Jarvis  •  Use /chat or @mention to get started")
    return embed


def _build_category_embed(key: str) -> discord.Embed:
    data = CATEGORIES[key]
    embed = discord.Embed(
        title=key,
        description=f"_{data['description']}_\n\u200b",
        color=data["color"],
    )
    for name, desc in data["commands"]:
        embed.add_field(name=f"`{name}`", value=f"↳ {desc}", inline=False)
    keys = list(CATEGORIES.keys())
    idx = keys.index(key)
    embed.set_footer(text=f"Category {idx + 1}/{len(keys)}  •  Jarvis Help")
    return embed


# ── Dropdown ──────────────────────────────────────────────────────────────────

class CategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=key.split(" ", 1)[-1], emoji=key.split(" ", 1)[0], value=key)
            for key in CATEGORIES
        ]
        super().__init__(placeholder="Choose a category…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view: HelpView = self.view
        if interaction.user.id != view.author_id:
            await interaction.response.send_message(
                "⚠️ Run your own `/help` to browse.", ephemeral=True
            )
            return
        view.current_key = self.values[0]
        view._update_nav()
        await interaction.response.edit_message(
            embed=_build_category_embed(view.current_key), view=view
        )


# ── View ──────────────────────────────────────────────────────────────────────

class HelpView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.keys = list(CATEGORIES.keys())
        self.current_key: str | None = None  # None = overview

        self.select = CategorySelect()
        self.add_item(self.select)
        self._update_nav()

    def _update_nav(self):
        idx = self.keys.index(self.current_key) if self.current_key else -1
        self.prev_btn.disabled = idx <= 0 and self.current_key is not None or self.current_key is None
        self.next_btn.disabled = idx >= len(self.keys) - 1
        self.home_btn.disabled = self.current_key is None

        # Fix prev disabled logic
        if self.current_key is None:
            self.prev_btn.disabled = True
        else:
            self.prev_btn.disabled = idx <= 0

    def current_embed(self) -> discord.Embed:
        if self.current_key is None:
            return _build_overview_embed()
        return _build_category_embed(self.current_key)

    async def _update(self, interaction: discord.Interaction):
        self._update_nav()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "⚠️ Run your own `/help` to browse.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, _):
        idx = self.keys.index(self.current_key)
        self.current_key = self.keys[idx - 1]
        await self._update(interaction)

    @discord.ui.button(label="🏠 Home", style=discord.ButtonStyle.primary, row=1)
    async def home_btn(self, interaction: discord.Interaction, _):
        self.current_key = None
        await self._update(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, interaction: discord.Interaction, _):
        if self.current_key is None:
            self.current_key = self.keys[0]
        else:
            idx = self.keys.index(self.current_key)
            self.current_key = self.keys[idx + 1]
        await self._update(interaction)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Cog ───────────────────────────────────────────────────────────────────────

class Help(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="help")
    async def prefix_help(self, ctx: commands.Context):
        """Show the Jarvis help menu."""
        view = HelpView(author_id=ctx.author.id)
        await ctx.reply(embed=_build_overview_embed(), view=view)

    @app_commands.command(name="help", description="Browse all Jarvis commands")
    async def slash_help(self, interaction: discord.Interaction):
        view = HelpView(author_id=interaction.user.id)
        await interaction.response.send_message(embed=_build_overview_embed(), view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))