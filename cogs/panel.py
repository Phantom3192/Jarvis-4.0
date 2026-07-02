"""
Panel cog — a live, button-driven admin control panel.

Wraps the existing admin/system commands (!global-ban, !global-unban,
!guild-ban, !guild-unban, !reload, !resetlimit, !set-cooldown, !set-burst)
behind one interactive message so admins never have to remember exact
command syntax. Opened with `!adminpanel` (alias `!apanel`) or `/panel`.

DESIGN NOTES:
- This cog does not duplicate any ban/reload/setting logic. It calls straight
  into the same helpers cogs.admin and cogs.system already use
  (Admin._do_botban, Admin._do_guild_ban, cogs.system._do_reload, etc.), so
  behaviour (DMs on ban, temp-ban scheduling, Turso persistence...) stays
  identical between the text commands and the panel.
- Every view is author-gated: only the admin who opened the panel (or ran
  the action) can click its buttons/selects. Everyone else gets a quiet
  ephemeral nudge.
- Views time out after 180s of inactivity and disable themselves in place
  rather than erroring on a stale interaction.
"""
import time
import discord
from discord.ext import commands
from discord import app_commands

from cogs.admin import (
    is_admin,
    parse_duration_str,
    bot_bans,
    _guild_bans,
    _format_ban_lines,
    _build_ban_list_embed,
    _build_guild_ban_embed,
)
from cogs.state import get_setting, set_setting, reset_ai_usage
# NOTE: cogs.system is intentionally NOT imported at module level here.
# cogs.panel loads earlier than cogs.system in main.py's COGS list, and
# discord.py's load_extension() re-executes a cog from scratch rather than
# reusing sys.modules — importing cogs.system up here would pre-import it,
# and then load_extension("cogs.system") would run its module-level code a
# second time (the exact double-init bug this project already worked around
# for cogs.ai). _do_reload is imported lazily inside reload_btn instead,
# same pattern cogs/ai.py already uses for its own cogs.system references.

PANEL_TIMEOUT = 180


# ── Small shared helpers ────────────────────────────────────────────────────

def _get_admin_cog(bot: commands.Bot):
    """The ban/unban core logic lives on the Admin cog instance (it needs
    self.temp_ban_tasks / self.bot), so panel actions borrow it instead of
    reimplementing ban handling."""
    return bot.get_cog("Admin")


async def _resolve_user(bot: commands.Bot, query: str) -> discord.User | None:
    """Resolve a user from an ID, a raw mention, or a name/display-name
    substring match across guilds the bot can see."""
    query = query.strip()
    raw = query.strip("<@!>") if query.startswith("<@") else query
    if raw.isdigit():
        try:
            return await bot.fetch_user(int(raw))
        except discord.NotFound:
            return None
        except discord.HTTPException:
            return None

    q = query.lower()
    seen: set[int] = set()
    for member in bot.get_all_members():
        if member.id in seen:
            continue
        seen.add(member.id)
        if q in member.name.lower() or (member.display_name and q in member.display_name.lower()):
            return member
    return None


def _base_embed(title: str, description: str = "", color=discord.Color.blurple()) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text="Jarvis Control Panel")
    return embed


class _AuthorGatedView(discord.ui.View):
    """Base view: only the admin who opened the panel may interact; the
    view disables itself in place on timeout instead of erroring later."""

    def __init__(self, author_id: int, *, timeout: float = PANEL_TIMEOUT):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "⚠️ Only the admin who opened this panel can use it. Run `!adminpanel` yourself.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ── Modals ───────────────────────────────────────────────────────────────────

class ResetLimitModal(discord.ui.Modal, title="Reset Daily AI Limit"):
    user_query = discord.ui.TextInput(
        label="User ID, @mention, or username",
        placeholder="e.g. 123456789012345678",
        required=True,
        max_length=100,
    )

    def __init__(self, parent_view: "PanelHomeView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        user = await _resolve_user(interaction.client, str(self.user_query))
        if user is None:
            await interaction.response.send_message(
                f"❌ Couldn't find a user matching `{self.user_query}`.", ephemeral=True
            )
            return
        reset_ai_usage(user.id)
        embed = _base_embed(
            "✅ Limit Reset",
            f"Daily AI limit reset for **{user}** (`{user.id}`) — they can chat again.",
            discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class CooldownModal(discord.ui.Modal, title="Set Command Cooldown"):
    seconds = discord.ui.TextInput(label="Cooldown (seconds)", required=True, max_length=10)

    def __init__(self, parent_view: "PanelHomeView"):
        super().__init__()
        self.parent_view = parent_view
        self.seconds.default = str(get_setting("user_command_cooldown", 2.0))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            sec = float(str(self.seconds))
            if sec < 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ Enter a number ≥ 0.", ephemeral=True)
            return
        set_setting("user_command_cooldown", sec)
        embed = _base_embed(
            "✅ Cooldown Updated", f"Command cooldown set to **{sec}** second(s).", discord.Color.green()
        )
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class BurstModal(discord.ui.Modal, title="Configure Burst Protection"):
    limit = discord.ui.TextInput(label="Command limit", required=True, max_length=10)
    window = discord.ui.TextInput(label="Window (seconds)", required=True, max_length=10)
    timeout_field = discord.ui.TextInput(label="Timeout (seconds)", required=True, max_length=10)

    def __init__(self, parent_view: "PanelHomeView"):
        super().__init__()
        self.parent_view = parent_view
        self.limit.default = str(get_setting("burst_limit_count", 20))
        self.window.default = str(get_setting("burst_window_seconds", 60.0))
        self.timeout_field.default = str(get_setting("burst_timeout_seconds", 300.0))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            l = int(str(self.limit))
            w = float(str(self.window))
            t = float(str(self.timeout_field))
            if l < 1 or w <= 0 or t < 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Invalid values — limit must be ≥1, window >0, timeout ≥0.", ephemeral=True
            )
            return
        set_setting("burst_limit_count", l)
        set_setting("burst_window_seconds", w)
        set_setting("burst_timeout_seconds", t)
        embed = _base_embed(
            "✅ Burst Settings Updated",
            f"Limit: **{l}** commands\nWindow: **{w}**s\nTimeout: **{t}**s",
            discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class UserSearchModal(discord.ui.Modal, title="Find a User"):
    user_query = discord.ui.TextInput(
        label="User ID, @mention, or username",
        placeholder="e.g. 123456789012345678 or Phantom",
        required=True,
        max_length=100,
    )

    def __init__(self, panel_cog: "Panel", author_id: int):
        super().__init__()
        self.panel_cog = panel_cog
        self.author_id = author_id

    async def on_submit(self, interaction: discord.Interaction):
        user = await _resolve_user(interaction.client, str(self.user_query))
        if user is None:
            await interaction.response.send_message(
                f"❌ Couldn't find a user matching `{self.user_query}`.", ephemeral=True
            )
            return
        view = UserDetailView(self.panel_cog, self.author_id, user)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class GuildBanReasonModal(discord.ui.Modal, title="Ban Guild"):
    reason = discord.ui.TextInput(
        label="Reason", required=False, max_length=200, default="No reason provided"
    )

    def __init__(self, panel_cog: "Panel", author_id: int, guild: discord.Guild):
        super().__init__()
        self.panel_cog = panel_cog
        self.author_id = author_id
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        admin_cog = _get_admin_cog(interaction.client)
        reason = str(self.reason) or "No reason provided"
        msgs, collect = _collector()
        await admin_cog._do_guild_ban(self.guild.id, reason, collect)
        view = GuildDetailView(self.panel_cog, self.author_id, self.guild)
        embed = view.build_embed()
        embed.add_field(name="Result", value=msgs[0] if msgs else "Done.", inline=False)
        await interaction.response.edit_message(embed=embed, view=view)


class UserBanModal(discord.ui.Modal, title="Ban User"):
    duration = discord.ui.TextInput(
        label="Duration (e.g. 7d, 2h, 30m) — blank = permanent", required=False, max_length=20
    )
    reason = discord.ui.TextInput(
        label="Reason", required=False, max_length=200, default="No reason provided"
    )

    def __init__(self, panel_cog: "Panel", author_id: int, user: discord.User):
        super().__init__()
        self.panel_cog = panel_cog
        self.author_id = author_id
        self.user = user

    async def on_submit(self, interaction: discord.Interaction):
        admin_cog = _get_admin_cog(interaction.client)

        if is_admin(self.user):
            await interaction.response.send_message("❌ You can't ban another admin.", ephemeral=True)
            return

        dur_raw = str(self.duration).strip()
        reason = str(self.reason) or "No reason provided"
        duration, unit = 0, "permanent"
        if dur_raw:
            parsed = parse_duration_str(dur_raw)
            if not parsed:
                await interaction.response.send_message(
                    "❌ Invalid duration — use formats like `30m`, `2h`, `7d`, `1w`.", ephemeral=True
                )
                return
            duration, unit = parsed

        msgs, collect = _collector()
        await admin_cog._do_botban(self.user, reason, duration, unit, collect)
        view = UserDetailView(self.panel_cog, self.author_id, self.user)
        embed = view.build_embed()
        embed.add_field(name="Result", value=msgs[0] if msgs else "Done.", inline=False)
        await interaction.response.edit_message(embed=embed, view=view)


def _collector() -> tuple[list[str], "callable"]:
    """Small helper so panel actions can capture the message that a shared
    _do_botban / _do_guild_ban / etc. helper would normally send via
    ctx.reply, without duplicating any of that logic here."""
    msgs: list[str] = []

    async def collect(text: str) -> None:
        msgs.append(text)

    return msgs, collect


# ── User ban/unban ──────────────────────────────────────────────────────────

class UserDetailView(_AuthorGatedView):
    def __init__(self, panel_cog: "Panel", author_id: int, user: discord.User):
        super().__init__(author_id)
        self.panel_cog = panel_cog
        self.user = user
        self._sync_ban_button()

    def build_embed(self) -> discord.Embed:
        banned = str(self.user.id) in bot_bans
        embed = _base_embed(
            f"👤 {self.user}",
            color=discord.Color.red() if banned else discord.Color.green(),
        )
        embed.set_thumbnail(url=self.user.display_avatar.url)
        embed.add_field(name="ID", value=f"`{self.user.id}`", inline=True)
        embed.add_field(name="Status", value="🚫 Banned" if banned else "✅ Not banned", inline=True)
        if banned:
            data = bot_bans[str(self.user.id)]
            expires = data.get("expires")
            expiry = "Permanent" if not expires else discord.utils.format_dt(
                discord.utils.utcnow().__class__.fromtimestamp(expires), style="R"
            )
            embed.add_field(name="Reason", value=data.get("reason", "None"), inline=False)
            embed.add_field(name="Expires", value=expiry, inline=False)
        return embed

    def _sync_ban_button(self) -> None:
        banned = str(self.user.id) in bot_bans
        self.ban_toggle.label = "Unban User" if banned else "Ban User"
        self.ban_toggle.style = discord.ButtonStyle.success if banned else discord.ButtonStyle.danger

    @discord.ui.button(label="Ban User", style=discord.ButtonStyle.danger, row=0)
    async def ban_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(self.user.id) in bot_bans:
            admin_cog = _get_admin_cog(interaction.client)
            msgs, collect = _collector()
            await admin_cog._do_botunban(self.user.id, collect)
            self._sync_ban_button()
            embed = self.build_embed()
            embed.add_field(name="Result", value=msgs[0] if msgs else "Done.", inline=False)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            if is_admin(self.user):
                await interaction.response.send_message("❌ You can't ban another admin.", ephemeral=True)
                return
            await interaction.response.send_modal(UserBanModal(self.panel_cog, self.author_id, self.user))

    @discord.ui.button(label="🔍 Search Another", style=discord.ButtonStyle.secondary, row=0)
    async def search_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(UserSearchModal(self.panel_cog, self.author_id))

    @discord.ui.button(label="⬅️ Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = PanelHomeView(self.panel_cog, self.author_id)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class UserBanUnbanSelect(discord.ui.Select):
    def __init__(self, users: list[tuple[str, str]]):
        options = [
            discord.SelectOption(label=name[:100], value=uid, description=f"ID: {uid}")
            for uid, name in users[:25]
        ]
        super().__init__(placeholder="Select a banned user to unban…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view: UserBansListView = self.view
        admin_cog = _get_admin_cog(interaction.client)
        uid = int(self.values[0])
        msgs, collect = _collector()
        await admin_cog._do_botunban(uid, collect)
        embed = await view.build_embed(interaction.client)
        embed.add_field(name="Result", value=msgs[0] if msgs else "Done.", inline=False)
        new_view = UserBansListView(view.panel_cog, view.author_id)
        await new_view._load(interaction.client)
        await interaction.response.edit_message(embed=embed, view=new_view)


class UserBansListView(_AuthorGatedView):
    def __init__(self, panel_cog: "Panel", author_id: int):
        super().__init__(author_id)
        self.panel_cog = panel_cog

    async def _load(self, bot: commands.Bot) -> None:
        self.clear_items()
        if bot_bans:
            pairs: list[tuple[str, str]] = []
            for uid_str in list(bot_bans.keys())[:25]:
                try:
                    user = await bot.fetch_user(int(uid_str))
                    pairs.append((uid_str, str(user)))
                except Exception:
                    pairs.append((uid_str, f"Unknown ({uid_str})"))
            self.add_item(UserBanUnbanSelect(pairs))
        self.add_item(self._make_back_button())

    def _make_back_button(self) -> discord.ui.Button:
        btn = discord.ui.Button(label="⬅️ Back", style=discord.ButtonStyle.secondary, row=1)

        async def cb(interaction: discord.Interaction):
            view = PanelHomeView(self.panel_cog, self.author_id)
            await interaction.response.edit_message(embed=view.build_embed(), view=view)

        btn.callback = cb
        return btn

    async def build_embed(self, bot: commands.Bot) -> discord.Embed:
        if not bot_bans:
            return _base_embed("✅ No banned users", "No users are currently bot-banned.", discord.Color.green())
        lines = await _format_ban_lines(bot)
        return _build_ban_list_embed(lines)


# ── Guild list / detail ──────────────────────────────────────────────────────

class GuildSelect(discord.ui.Select):
    def __init__(self, guilds: list[discord.Guild]):
        options = []
        for g in guilds[:25]:
            joined = g.me.joined_at.strftime("%Y-%m-%d") if g.me and g.me.joined_at else "?"
            options.append(
                discord.SelectOption(
                    label=g.name[:100],
                    value=str(g.id),
                    description=f"👥 {g.member_count or '?'} members  •  joined {joined}",
                )
            )
        super().__init__(placeholder="Select a server…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view: GuildListView = self.view
        guild = interaction.client.get_guild(int(self.values[0]))
        if guild is None:
            await interaction.response.send_message("❌ That server is no longer available.", ephemeral=True)
            return
        detail = GuildDetailView(view.panel_cog, view.author_id, guild)
        await interaction.response.edit_message(embed=detail.build_embed(), view=detail)


class GuildListView(_AuthorGatedView):
    """Paginated (25/page) select list of every server Jarvis is in."""

    PAGE_SIZE = 25

    def __init__(self, panel_cog: "Panel", author_id: int, bot: commands.Bot, page: int = 0):
        super().__init__(author_id)
        self.panel_cog = panel_cog
        self.guilds = sorted(bot.guilds, key=lambda g: g.name.lower())
        self.page = page
        self._build()

    @property
    def max_page(self) -> int:
        return max(0, (len(self.guilds) - 1) // self.PAGE_SIZE)

    def _build(self) -> None:
        self.clear_items()
        start = self.page * self.PAGE_SIZE
        chunk = self.guilds[start:start + self.PAGE_SIZE]
        if chunk:
            self.add_item(GuildSelect(chunk))

        prev_btn = discord.ui.Button(
            label="◀", style=discord.ButtonStyle.secondary, row=1, disabled=(self.page == 0)
        )
        next_btn = discord.ui.Button(
            label="▶", style=discord.ButtonStyle.secondary, row=1, disabled=(self.page >= self.max_page)
        )

        async def go_prev(interaction: discord.Interaction):
            self.page = max(0, self.page - 1)
            self._build()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

        async def go_next(interaction: discord.Interaction):
            self.page = min(self.max_page, self.page + 1)
            self._build()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

        prev_btn.callback = go_prev
        next_btn.callback = go_next
        self.add_item(prev_btn)
        self.add_item(next_btn)

        back_btn = discord.ui.Button(label="⬅️ Back", style=discord.ButtonStyle.secondary, row=1)

        async def go_back(interaction: discord.Interaction):
            view = PanelHomeView(self.panel_cog, self.author_id)
            await interaction.response.edit_message(embed=view.build_embed(), view=view)

        back_btn.callback = go_back
        self.add_item(back_btn)

    def build_embed(self) -> discord.Embed:
        embed = _base_embed(
            f"📋 Servers ({len(self.guilds)})",
            "Select a server below to view details, member count, join date, and ban/unban it.",
        )
        embed.set_footer(text=f"Jarvis Control Panel  •  Page {self.page + 1}/{self.max_page + 1}")
        return embed


class GuildDetailView(_AuthorGatedView):
    def __init__(self, panel_cog: "Panel", author_id: int, guild: discord.Guild):
        super().__init__(author_id)
        self.panel_cog = panel_cog
        self.guild = guild
        self._sync_ban_button()

    def build_embed(self) -> discord.Embed:
        g = self.guild
        banned = g.id in _guild_bans
        embed = _base_embed(g.name, color=discord.Color.red() if banned else discord.Color.blurple())
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        embed.add_field(name="ID", value=f"`{g.id}`", inline=True)
        embed.add_field(name="Members", value=str(g.member_count or "?"), inline=True)
        embed.add_field(name="Owner", value=str(g.owner) if g.owner else "Unknown", inline=True)
        if g.me and g.me.joined_at:
            embed.add_field(name="Jarvis joined", value=discord.utils.format_dt(g.me.joined_at, style="R"), inline=True)
        embed.add_field(name="Server created", value=discord.utils.format_dt(g.created_at, style="R"), inline=True)
        embed.add_field(name="Status", value="🚫 Banned" if banned else "✅ Not banned", inline=True)
        if banned:
            data = _guild_bans[g.id]
            embed.add_field(name="Ban reason", value=data.get("reason", "None"), inline=False)
        return embed

    def _sync_ban_button(self) -> None:
        banned = self.guild.id in _guild_bans
        self.ban_toggle.label = "Unban Server" if banned else "Ban Server"
        self.ban_toggle.style = discord.ButtonStyle.success if banned else discord.ButtonStyle.danger

    @discord.ui.button(label="Ban Server", style=discord.ButtonStyle.danger, row=0)
    async def ban_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        admin_cog = _get_admin_cog(interaction.client)
        if self.guild.id in _guild_bans:
            msgs, collect = _collector()
            await admin_cog._do_guild_unban(self.guild.id, collect)
            self._sync_ban_button()
            embed = self.build_embed()
            embed.add_field(name="Result", value=msgs[0] if msgs else "Done.", inline=False)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.send_modal(GuildBanReasonModal(self.panel_cog, self.author_id, self.guild))

    @discord.ui.button(label="⬅️ Back to list", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = GuildListView(self.panel_cog, self.author_id, interaction.client)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class GuildBanUnbanSelect(discord.ui.Select):
    def __init__(self, entries: list[tuple[int, str]]):
        options = [
            discord.SelectOption(label=name[:100], value=str(gid), description=f"ID: {gid}")
            for gid, name in entries[:25]
        ]
        super().__init__(placeholder="Select a banned server to unban…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view: GuildBansListView = self.view
        admin_cog = _get_admin_cog(interaction.client)
        gid = int(self.values[0])
        msgs, collect = _collector()
        await admin_cog._do_guild_unban(gid, collect)
        embed = await _build_guild_ban_embed(interaction.client)
        embed.add_field(name="Result", value=msgs[0] if msgs else "Done.", inline=False)
        new_view = GuildBansListView(view.panel_cog, view.author_id, interaction.client)
        await interaction.response.edit_message(embed=embed, view=new_view)


class GuildBansListView(_AuthorGatedView):
    def __init__(self, panel_cog: "Panel", author_id: int, bot: commands.Bot):
        super().__init__(author_id)
        self.panel_cog = panel_cog
        if _guild_bans:
            entries = []
            for gid in list(_guild_bans.keys())[:25]:
                guild = bot.get_guild(gid)
                entries.append((gid, guild.name if guild else f"Unknown ({gid})"))
            self.add_item(GuildBanUnbanSelect(entries))
        self.add_item(self._make_back_button())

    def _make_back_button(self) -> discord.ui.Button:
        btn = discord.ui.Button(label="⬅️ Back", style=discord.ButtonStyle.secondary, row=1)

        async def cb(interaction: discord.Interaction):
            view = PanelHomeView(self.panel_cog, self.author_id)
            await interaction.response.edit_message(embed=view.build_embed(), view=view)

        btn.callback = cb
        return btn


# ── Home ─────────────────────────────────────────────────────────────────────

class PanelHomeView(_AuthorGatedView):
    def __init__(self, panel_cog: "Panel", author_id: int):
        super().__init__(author_id)
        self.panel_cog = panel_cog

    def build_embed(self) -> discord.Embed:
        bot = self.panel_cog.bot
        embed = _base_embed(
            "🛠️ Jarvis Control Panel",
            "Manage servers, users, and bot settings without typing commands.",
        )
        embed.add_field(name="Guilds", value=str(len(bot.guilds)), inline=True)
        embed.add_field(name="Guild bans", value=str(len(_guild_bans)), inline=True)
        embed.add_field(name="User bans", value=str(len(bot_bans)), inline=True)
        return embed

    # Row 0 — browse / moderate
    @discord.ui.button(label="📋 Guilds", style=discord.ButtonStyle.primary, row=0)
    async def guilds_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = GuildListView(self.panel_cog, self.author_id, interaction.client)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    @discord.ui.button(label="🚫 Guild Bans", style=discord.ButtonStyle.secondary, row=0)
    async def guild_bans_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await _build_guild_ban_embed(interaction.client)
        view = GuildBansListView(self.panel_cog, self.author_id, interaction.client)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="👤 User Bans", style=discord.ButtonStyle.secondary, row=0)
    async def user_bans_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = UserBansListView(self.panel_cog, self.author_id)
        await view._load(interaction.client)
        embed = await view.build_embed(interaction.client)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="🔍 Find User", style=discord.ButtonStyle.secondary, row=0)
    async def find_user_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(UserSearchModal(self.panel_cog, self.author_id))

    # Row 1 — bot settings
    @discord.ui.button(label="🔄 Reload", style=discord.ButtonStyle.success, row=1)
    async def reload_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        from cogs.system import _do_reload  # lazy — see import note at top of file
        await interaction.response.edit_message(
            embed=_base_embed("🔄 Reloading…", color=discord.Color.blurple()), view=self
        )
        result = await _do_reload(interaction.client)
        embed = _base_embed("🔄 Reload Complete", result, discord.Color.green())
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="⏱️ Reset Limit", style=discord.ButtonStyle.secondary, row=1)
    async def reset_limit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ResetLimitModal(self))

    @discord.ui.button(label="⚙️ Cooldown", style=discord.ButtonStyle.secondary, row=1)
    async def cooldown_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CooldownModal(self))

    @discord.ui.button(label="💥 Burst", style=discord.ButtonStyle.secondary, row=1)
    async def burst_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BurstModal(self))


# ── Cog ──────────────────────────────────────────────────────────────────────

class Panel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="adminpanel", aliases=["apanel"])
    async def prefix_panel(self, ctx: commands.Context):
        """Admin only — open the live control panel."""
        if not is_admin(ctx.author):
            await ctx.reply("🚫 You don't have permission to use this command.")
            return
        view = PanelHomeView(self, ctx.author.id)
        msg = await ctx.reply(embed=view.build_embed(), view=view)
        view.message = msg

    @app_commands.command(name="panel", description="Open the live admin control panel (admin only)")
    async def slash_panel(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "🚫 You don't have permission to use this command.", ephemeral=True
            )
            return
        view = PanelHomeView(self, interaction.user.id)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot):
    await bot.add_cog(Panel(bot))