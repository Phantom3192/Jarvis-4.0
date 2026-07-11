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
            ("!clearhistory  /clearhistory", "Clear your Jarvis conversation history"),
            ("!setmodel <model>  /setmodel", "Choose which AI model Jarvis uses for your messages"),
            ("!mymodel  /mymodel",    "Check which AI model you're currently using"),
            ("!mylimit  /mylimit",    "Check how many AI messages you have left today"),
            ("!summary  /summary",   "Summarise your chat — e.g. `!summary 1h`, `!summary 30m`"),
            ("!clearsummary  /clearsummary", "Clear your summary log"),
        ],
    },
    "🧠 Memory": {
        "description": "Jarvis remembers things about you across sessions.",
        "color": 0x9B59B6,
        "commands": [
            ("@Jarvis call me X",           "Tell Jarvis what to call you"),
            ("@Jarvis remember that …",     "Explicitly save a fact about yourself"),
            ("!nickname <name>",            "Set the name Jarvis uses for you"),
            ("!mymemory  /mymemory",        "View everything Jarvis remembers about you"),
            ("!forgetme  /forgetme",        "Wipe all your saved memory"),
            ("!dnd on/off  /dnd",           "Toggle Do Not Disturb mode (blocks all commands except !dnd off and !settings)"),
        ],
    },
    "⏰ Reminders": {
        "description": "Set DM reminders and manage active reminders.",
        "color": 0xE74C3C,
        "commands": [
            ("!remindme <duration> <message>", "Create a DM reminder — e.g. `!remindme 15m Stretch your legs`"),
            ("!myreminders",               "Show your active reminders"),
            ("!cancelreminder <id>",       "Cancel a reminder by its ID"),
        ],
    },
    "🎨 Image Gen": {
        "description": "Generate AI images from text prompts — free, no limits.",
        "color": 0xE91E63,
        "commands": [
            ("!image  /image <name>",  "Sends an Image from Web based on your prompt"),
        ],
    },
    "♟️ Chess & Hangman": {
        "description": "Play chess or hangman.",
        "color": 0xF1C40F,
        "commands": [
            ("♟️ **CHESS**", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
            ("!chess @user / /chess", "Challenge someone to a game of chess"),
            ("!move <e2e4>", "Make a move using algebraic notation"),
            ("!chessboard / !board  /chessboard", "Re-show the current board"),
            ("!resign / /resign", "Resign your current chess game"),
            ("!draw / /draw", "Offer or accept a draw"),
            ("!hint / /hint", "Get an AI hint (1 free per turn, then costs JC)"),
            ("!undo / /undo", "Request to undo the last move (needs opponent's agreement)"),
            ("!stopchess / /stopchess", "Stop the current chess game"),
            ("", ""),
            ("🪢 **HANGMAN**", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
            ("!hangman / /hangman", "Start a game of hangman"),
            ("/stophangman", "Stop the current hangman game"),
        ],
    },
    "🎭 Mafia & Akinator": {
        "description": "Play Mafia or Akinator.",
        "color": 0xF1C40F,
        "commands": [
            ("🎭 **MAFIA**", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
            ("!mafia / /mafia", "Start a Mafia game in this channel"),
            ("!mafvote @user", "Vote to eliminate someone"),
            ("!mafaction @user", "Submit night action (mafia/detective/doctor)"),
            ("!lastwill <text>", "Save a last will, DM'd to you and revealed if you're eliminated"),
            ("!startmafia", "Start the game after players join"),
            ("!stopmafia", "Stop the current Mafia game"),
            ("!mafguide  /mafguide", "How to play Mafia — roles, phases, and win conditions"),
            ("", ""),
            ("🔮 **AKINATOR**", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
            ("!akinator / /akinator", "Think of a character — I'll guess it!"),
            ("!stopaki / /stopaki", "Stop the current Akinator game"),
        ],
    },
    "🎉 Fun": {
        "description": "Fun commands to mess around with.",
        "color": 0xF1C40F,
        "commands": [
            ("🔮 **COMPLIMENT**", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
            ("!compliment  /compliment @user", "Send someone a nice compliment"),
            ("", ""),
            ("🎲 **ROLL**", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
            ("!roll / !dice  /roll", "Roll a random number between 1 and 100"),
            ("", ""),
            ("🔢 **COUNTING GAME**", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
            ("!countsetup / /countsetup", "Set the counting channel for this server"),
            ("!countstats / /countstats", "Show current count and high score"),
            ("!countreset / /countreset", "Reset the count (admin only)"),
            ("!countremove / /countremove", "Remove counting channel setup"),
        ],
    },
    # "🎵 Music": {  # TEMPORARILY DISABLED — cogs.music isn't loaded right now
    #     "description": "Play music in voice channels via YouTube.",
    #     "color": 0x1DB954,
    #     "commands": [
    #         ("!controls / !control  /controls", "Open the interactive music control panel"),
    #         ("!play / !p <song>  /play",      "Join VC and play a song or YouTube URL"),
    #         ("!skip / !s  /skip",             "Skip the current track"),
    #         ("!stop  /stop",                  "Stop music and disconnect"),
    #         ("!pause  /pause",                "Pause the current track"),
    #         ("!resume  /resume",              "Resume a paused track"),
    #         ("!queue / !q  /queue",           "Show the current song queue"),
    #         ("!np  /nowplaying",              "Show what's currently playing"),
    #         ("!volume / !vol <0-100>  /volume", "Set playback volume"),
    #         ("!playlist ...",                 "Manage your saved playlists"),
    #         ("!autoplay on/off",              "Auto-play similar songs when the queue ends"),
    #     ],
    # },
    "🔍 YouTube": {
        "description": "Search YouTube, browse trending, and get video info.",
        "color": 0xFF0000,
        "commands": [
            ("!youtube / !yt <query>  /youtube", "Search YouTube and pick from results"),
            ("/reel <query>", "Search YouTube Shorts and play one directly in Discord"),
            ("!yttrend / !trending    /yttrend",  "Browse trending YouTube videos by category"),
            ("!ytinfo / !ytvideo <url>  /ytinfo", "Get detailed info about a YouTube video"),
        ],
    },
    "🪙 Jarvis Credits": {
        "description": "Earn and spend Jarvis Credits (JC).",
        "color": 0xF1C40F,
        "commands": [
            ("!balance / !jc / !credits  /balance", "Check your (or someone else's) JC balance"),
            ("!leaderboard / !jcleaderboard / !jctop  /leaderboard", "View the top JC holders"),
            ("!streak / !jcstreak  /streak", "Check your daily chat streak and milestone progress"),
            ("!invite / !refer  /invite", "Get your personal referral code to share"),
            ("!redeem <code>  /redeem", "Redeem a friend's referral code (must be your first-ever interaction with Jarvis)"),
            ("!shop / !jcshop  /shop", "Browse the JC shop (buy with !shop buy <item_id>)"),
            ("!shop buy <item_id>", "Buy a shop item directly without opening the menu"),
            ("🔍 View Perks button", "In !shop, see exactly what each title (VIP, Elite...) grants while equipped"),
            ("!titleperks / !perks  /titleperks", "See title perks directly, without opening the shop"),
            ("!mysterybox / !mbox  /mysterybox", "Open a Mystery Box (150 JC) for a random 10–300 JC reward"),
            ("!deluxebox / !dbox  /deluxebox", "Open a pricier Deluxe Mystery Box for a bigger reward — VIP/Elite titles boost the payout"),
            ("!transferjc @user <amount>  /transferjc", "Send JC to another user (they must accept)"),
            ("!giftsub @user <vip|elite>  /giftsub", "Gift someone a week of VIP/Elite using your own JC (they must accept)"),
            ("!autorenew <vip|elite> [on|off]  /autorenew", "Check or toggle auto-renew for your VIP/Elite subscription"),
            ("Daily check-in",        "Send your first message of the day to earn JC automatically"),
            ("Daily streak",          "Chatting on consecutive days builds a streak — 7 days: +200 JC, 30 days: +500 JC"),
            ("Chat with Jarvis",      "Earn a small amount of JC for chatting with the AI (daily cap)"),
            ("Save a counting streak", "Spend JC to save the count when someone messes up"),
            ("Reset AI limit",        "Spend JC to reset your daily AI message limit"),
        ],
    },
    "🪪 Profile": {
        "description": "Your Jarvis rank card — stats, streak, game records, badges, titles, and banners.",
        "color": 0x9B59B6,
        "commands": [
            ("!profile / !rank  /profile", "View your (or someone else's) profile card"),
            ("!titles  /titles", "See the titles you own and which one is equipped"),
            ("!title <name>  /title", "Equip an owned title (or `none` to unequip)"),
            ("!banners  /banners", "See the profile banners you own"),
            ("!banner <name>  /banner", "Equip an owned banner (or `none` to unequip)"),
            ("!unequip title|banner|all  /unequip", "Unequip your current title and/or banner"),
            ("!profilehide <field>  /profile-hide", "Hide a stat (e.g. balance, badges) from other people's view of your profile"),
            ("!profileshow <field>  /profile-show", "Make a previously hidden stat visible again"),
            ("!profilesettings  /profile-settings", "See which of your profile fields are currently hidden"),
            ("🏅 Badges", "Unlocked automatically — win games, chat, and play music to earn them"),
        ],
    },
    "📊 Stats": {
        "description": "View your Jarvis usage stats.",
        "color": 0x2ECC71,
        "commands": [
            ("!stats  /stats",        "View your own usage stats"),
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
            ("!adminhelp",            "Show the admin command menu for moderators and bot owners"),
            ("!settings  /config",    "View channel Jarvis settings (admins can modify)"),
            ("!settings restrict_mode on/off", "Stop Jarvis from responding in this channel (admin only)"),
            ("!settings auto_respond on/off",  "Make Jarvis respond to every message (admin only)"),
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
    embed.set_footer(text="Jarvis — Use /chat or @mention to get started")
    return embed

ADMIN_CATEGORIES = {
    "🔒 User Bans": {
        "description": "Ban and unban individual users from using Jarvis.",
        "color": 0xC0392B,
        "commands": [
            ("!global-ban @user [reason]",          "Ban a user from Jarvis globally (permanent or temp)"),
            ("!global-unban <user_id>",             "Unban a user from Jarvis"),
            ("!global-bans  /global-bans",          "View all currently banned users"),
        ],
    },
    "🚫 Server Bans": {
        "description": "Ban and unban entire servers from using Jarvis.",
        "color": 0xE74C3C,
        "commands": [
            ("!guild-ban <guild_id> [reason]",      "Ban a server from using Jarvis (bot owner)"),
            ("!guild-unban <guild_id>",             "Unban a server from Jarvis (bot owner)"),
            ("!guild-bans  /guild-bans",            "View all currently banned servers (bot owner)"),
        ],
    },
    "📊 Rate Limiting": {
        "description": "Manage cooldowns and burst limits for the bot.",
        "color": 0xF39C12,
        "commands": [
            ("!resetlimit @user",                   "Reset a user's daily AI message limit"),
            ("!set-cooldown <seconds>",             "Set cooldown between AI requests (bot owner)"),
            ("!set-burst <limit> <window> <timeout>", "Configure burst rate limiting (bot owner)"),
        ],
    },
    "🪙 Jarvis Credits": {
        "description": "Manage the Jarvis Credit (JC) economy.",
        "color": 0xF1C40F,
        "commands": [
            ("!givecredits @user <amount>",         "Grant (or remove, if negative) JC for a user (bot owner)"),
            ("!removejc @user <amount>",             "Remove JC from a user (bot owner)"),
            ("!grantsub @user <vip|elite> [days]  /grantsub", "Grant or extend a VIP/Elite subscription for free (bot owner)"),
        ],
    },
    "🎛️ Bot Control": {
        "description": "Operational controls for the bot owner.",
        "color": 0x34495E,
        "commands": [
            ("!panel / !apanel  /panel",   "Open the live, button-driven admin control panel"),
            ("!reload  /reload",           "Reload all cogs and clear memory cache (bot owner)"),
            ("!status",                    "Live bot status dashboard (bot owner)"),
            ("!apistatus  /apistatus",     "Show AI provider status, latency, and token usage"),
            ("!global-announce  /global-announce", "DM an announcement to every Jarvis user (bot owner)"),
            ("!announce  /announce",       "DM a specific user as Jarvis (bot owner)"),
            ("!backfill_servers",          "Backfill server-join logs to the webhook for every current server (bot owner)"),
        ],
    },
    # "🎵 Music Overrides": {  # TEMPORARILY DISABLED — cogs.music isn't loaded right now
    #     "description": "Force-control music playback, bypassing normal checks (bot owner).",
    #     "color": 0x1DB954,
    #     "commands": [
    #         ("!forcejoin / !fjoin  /forcejoin",     "Force the bot to join your voice channel"),
    #         ("!forceplay / !fplay  /forceplay",     "Force-play a track, bypassing feature-down state"),
    #         ("!forcestop / !fstop  /forcestop",     "Force stop and disconnect"),
    #         ("!forcepause / !fpause  /forcepause",  "Force pause/resume toggle"),
    #         ("!forceresume / !fresume",             "Force resume playback"),
    #         ("!forceskip / !fskip  /forceskip",     "Force skip the current track"),
    #         ("!forcequeue / !fqueue / !fq",         "Force-show the queue"),
    #         ("!forcenp / !fnp",                     "Force-show now playing"),
    #         ("!forcevolume / !fvol",                "Force-set volume"),
    #         ("!forcecontrols / !fctrl / !fcp",      "Force-open the music control panel"),
    #     ],
    # },
}


def _build_admin_overview_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🛠️ Admin Commands",
        description=(
            "Browse admin and moderator utilities. Pick a category below.\n"
            "Some commands are bot-owner only — they won't work for regular moderators.\n\u200b"
        ),
        color=0xE74C3C,
    )
    for cat, data in ADMIN_CATEGORIES.items():
        embed.add_field(
            name=cat,
            value=data["description"],
            inline=True,
        )
    embed.set_footer(text="Admin Menu")
    return embed


def _build_admin_category_embed(key: str) -> discord.Embed:
    data = ADMIN_CATEGORIES[key]
    embed = discord.Embed(
        title=key,
        description=f"_{data['description']}_\n\u200b",
        color=data["color"],
    )
    for name, desc in data["commands"]:
        embed.add_field(name=f"`{name}`", value=f"↳ {desc}", inline=False)
    keys = list(ADMIN_CATEGORIES.keys())
    idx = keys.index(key)
    embed.set_footer(text=f"Category {idx + 1}/{len(keys)}  •  Admin Help")
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


class AdminCategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=key.split(" ", 1)[-1], emoji=key.split(" ", 1)[0], value=key)
            for key in ADMIN_CATEGORIES
        ]
        super().__init__(placeholder="Choose a category…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view: AdminHelpView = self.view
        if interaction.user.id != view.author_id:
            await interaction.response.send_message(
                "⚠️ Run your own `!adminhelp` to browse.", ephemeral=True
            )
            return
        view.current_key = self.values[0]
        view._update_nav()
        await interaction.response.edit_message(
            embed=_build_admin_category_embed(view.current_key), view=view
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


class AdminHelpView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.keys = list(ADMIN_CATEGORIES.keys())
        self.current_key: str | None = None  # None = overview

        self.select = AdminCategorySelect()
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
            return _build_admin_overview_embed()
        return _build_admin_category_embed(self.current_key)

    async def _update(self, interaction: discord.Interaction):
        self._update_nav()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "⚠️ Run your own `!adminhelp` to browse.", ephemeral=True
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