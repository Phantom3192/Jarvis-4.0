import discord
from discord.ext import commands
from discord import app_commands
from cogs.admin import is_admin
from cogs.fun import Fun

# ── Category definitions ──────────────────────────────────────────────────────

CATEGORIES = {
    "🤖 AI": {
        "description": "Chat with Jarvis using AI.",
        "commands": [
            ("/chat",        "Chat with Jarvis (supports image attachments)"),
            ("@mention",     "Mention Jarvis or say 'jarvis' in a message to chat"),
            ("reply",        "Reply to any of Jarvis's messages to continue the conversation"),
            ("!mylimit",     "Check how many AI messages you have left today"),
            ("/mylimit",     "Slash version of mylimit"),
        ],
    },
    "📊 Stats": {
        "description": "View usage statistics.",
        "commands": [
            ("!stats",       "View your own Jarvis usage stats"),
            ("!stats @user", "View another user's stats (admins only)"),
            ("/stats",       "Slash version of the stats command"),
        ],
    },
    "💡 Feedback": {
        "description": "Send feedback or bug reports to the developers.",
        "commands": [
            ("!feedback",    "Submit feedback or a suggestion"),
            ("/feedback",    "Slash version of feedback"),
            ("!bugreport",   "Report a bug you encountered"),
            ("/bugreport",   "Slash version of bug report"),
        ],
    },
    "🎨 Prompts": {
        "description": "Customise Jarvis's personality for your server (requires Manage Server).",
        "commands": [
            ("!setprompt",   "Set a custom AI system prompt for this server"),
            ("/setprompt",   "Slash version of setprompt"),
            ("!resetprompt", "Reset this server's prompt to the default"),
            ("/resetprompt", "Slash version of resetprompt"),
            ("!viewprompt",  "View the current system prompt for this server"),
            ("/viewprompt",  "Slash version of viewprompt"),
        ],
    },
    "📢 Announce": {
        "description": "Send announcements or DMs (admins only).",
        "commands": [
            ("!global-announce", "Send a DM announcement to all Jarvis users"),
            ("/global-announce", "Slash version of global-announce"),
            ("!announce @user",  "Send a DM to a specific user"),
            ("/announce",        "Slash version of announce"),
        ],
    },
    "🛡️ Admin": {
        "description": "Bot moderation tools (admins only).",
        "commands": [
            ("!global-ban @user",   "Ban a user from using Jarvis"),
            ("/botban",             "Slash version of global-ban"),
            ("!global-unban <id>",  "Unban a user from Jarvis"),
            ("/botunban",           "Slash version of global-unban"),
            ("!global-bans",        "List all currently banned users"),
            ("/botbans",            "Slash version of global-bans"),
            ("!guild-ban <id>",     "Ban a server from Jarvis"),
            ("/guildban",           "Slash version of guild-ban"),
            ("!guild-unban <id>",   "Unban a server from Jarvis"),
            ("/guildunban",         "Slash version of guild-unban"),
            ("!guild-bans",         "List all guild-banned servers"),
            ("/guildbans",          "Slash version of guild-bans"),
            ("!resetlimit @user",   "Reset a user's daily AI message limit"),
            ("/resetlimit",         "Slash version of resetlimit"),
        ],
    },
    "🖥️ System": {
        "description": "Server and bot system information.",
        "commands": [
            ("!guildinfo",   "Show detailed info about this server (members, channels, roles, boosts…)"),
            ("/guildinfo",   "Slash version of guildinfo"),
            ("!servers",     "List all servers Jarvis is in (admin only)"),
            ("/servers",     "Slash version of servers"),
            ("!ping",        "Check Jarvis latency"),
            ("/ping",        "Slash version of ping"),
            ("!uptime",      "Check how long Jarvis has been online"),
            ("/uptime",      "Slash version of uptime"),
            ("!usage",       "Show CPU, RAM, disk stats (admin only)"),
            ("/usage",       "Slash version of usage"),
        ],
    },

    "🎨 Image Gen": {
        "description": "Generate AI images from text prompts — free, no limits.",
        "commands": [
            ("/imagine",          "Generate an image from a text prompt"),
            ("!imagine <prompt>", "Prefix version of imagine"),
            ("--style anime",     "Add a style flag: anime, realistic, pixel, cartoon, sketch, watercolor, cinematic, fantasy"),
        ],
    },
    "🎉 Fun": {
        "description": "Fun commands to play around with.",
        "commands": [
            ("!trivia", "Get a random trivia question to test your knowledge"),
            ("/trivia", "Slash version of trivia"),
            ("!hangman", "Classic word guessing — type single letters to guess, 6 lives"),
            ("/hangman", "Slash version of hangman"),
            ("/stophangman", "Stop the current hangman game"),
            ("!roast @user", "Roast a user with a witty insult"),
            ("/roast", "Slash version of roast"),
            ("!compliment @user", "Give a user a nice compliment"),
            ("/compliment", "Slash version of compliment"),
            ("!wyr", "Random Would You Rather question"),
            ("/wyr", "Slash version of wyr"),
            ("truth", "Get a random truth question"),
            ("/truth", "Slash version of truth"),
            ("!dare", "Get a random dare to complete"),
            ("/dare", "Slash version of dare"),
            ("!funfact", "Get a random fun fact"),
            ("/funfact", "Slash version of funfact"),
            
        ],
    },
}   # ── end CATEGORIES ──────────────────────────────────────────────────────────


# ── Embed builder ─────────────────────────────────────────────────────────────

def _build_category_embed(category: str, data: dict, page: int, total: int) -> discord.Embed:
    embed = discord.Embed(
        title=category,
        description=data["description"],
        color=discord.Color.blurple(),
    )
    for name, desc in data["commands"]:
        embed.add_field(name=f"`{name}`", value=desc, inline=False)
    embed.set_footer(text=f"Page {page}/{total}  •  Jarvis Help")
    return embed


def _build_overview_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📖 Jarvis — Help",
        description=(
            "Use the buttons below to browse command categories.\n\n"
            + "\n".join(
                f"{cat}  —  {data['description']}"
                for cat, data in CATEGORIES.items()
            )
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Jarvis Help  •  Use /chat or say 'jarvis' to get started")
    return embed


# ── Paginated view ────────────────────────────────────────────────────────────

class HelpView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.keys = list(CATEGORIES.keys())
        self.page = -1  # -1 = overview

        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page <= -1
        self.next_btn.disabled = self.page >= len(self.keys) - 1
        self.home_btn.disabled = self.page == -1

    def current_embed(self) -> discord.Embed:
        if self.page == -1:
            return _build_overview_embed()
        key = self.keys[self.page]
        return _build_category_embed(key, CATEGORIES[key], self.page + 1, len(self.keys))

    async def _update(self, interaction: discord.Interaction):
        self._update_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "⚠️ Start your own `/help` to browse commands.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self._update(interaction)

    @discord.ui.button(label="🏠", style=discord.ButtonStyle.primary)
    async def home_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = -1
        await self._update(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self._update(interaction)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Cog ───────────────────────────────────────────────────────────────────────

class Help(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Prefix command ────────────────────────────────────────────────────────

    @commands.command(name="help")
    async def prefix_help(self, ctx: commands.Context):
        """Show the Jarvis help menu."""
        view = HelpView(author_id=ctx.author.id)
        await ctx.reply(embed=_build_overview_embed(), view=view)

    # ── Slash command ─────────────────────────────────────────────────────────

    @app_commands.command(name="help", description="Browse all Jarvis commands")
    async def slash_help(self, interaction: discord.Interaction):
        view = HelpView(author_id=interaction.user.id)
        await interaction.response.send_message(embed=_build_overview_embed(), view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))