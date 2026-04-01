import discord
from discord.ext import commands
from discord import app_commands
from cogs.state import get_guild_prompt, set_guild_prompt, reset_guild_prompt

MAX_PROMPT_LEN = 1000  # characters


def _has_manage_guild(user: discord.Member | discord.User) -> bool:
    """Return True if the user has Manage Server permission (guild context only)."""
    if not isinstance(user, discord.Member):
        return False
    return user.guild_permissions.manage_guild


class Prompts(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Prefix commands ───────────────────────────────────────────────────────

    @commands.command(name="setprompt")
    @commands.guild_only()
    async def prefix_setprompt(self, ctx: commands.Context, *, prompt: str = None):
        """Set a custom system prompt for this server. Requires Manage Server."""
        if not _has_manage_guild(ctx.author):
            await ctx.reply("🚫 You need the **Manage Server** permission to set a custom prompt.")
            return
        if not prompt:
            await ctx.reply(
                "**Usage:** `!setprompt <your custom system prompt>`\n"
                "**Example:** `!setprompt You are a helpful pirate assistant. Arr!`"
            )
            return
        if len(prompt) > MAX_PROMPT_LEN:
            await ctx.reply(f"❌ Prompt is too long. Maximum is {MAX_PROMPT_LEN} characters.")
            return
        set_guild_prompt(ctx.guild.id, prompt)
        await ctx.reply(
            f"✅ Custom system prompt set for **{ctx.guild.name}**.\n"
            f"```\n{prompt[:200]}{'…' if len(prompt) > 200 else ''}\n```"
        )

    @commands.command(name="resetprompt")
    @commands.guild_only()
    async def prefix_resetprompt(self, ctx: commands.Context):
        """Reset this server's system prompt to the default. Requires Manage Server."""
        if not _has_manage_guild(ctx.author):
            await ctx.reply("🚫 You need the **Manage Server** permission to reset the prompt.")
            return
        existed = reset_guild_prompt(ctx.guild.id)
        if existed:
            await ctx.reply(f"✅ Custom prompt removed. **{ctx.guild.name}** is back to the default Jarvis personality.")
        else:
            await ctx.reply("ℹ️ This server has no custom prompt — already using the default.")

    @commands.command(name="viewprompt")
    @commands.guild_only()
    async def prefix_viewprompt(self, ctx: commands.Context):
        """View the current system prompt for this server."""
        prompt = get_guild_prompt(ctx.guild.id)
        if prompt:
            await ctx.reply(
                f"📝 **Custom prompt for {ctx.guild.name}:**\n```\n{prompt}\n```"
            )
        else:
            await ctx.reply(
                "📝 This server is using the **default Jarvis prompt**.\n"
                "Use `!setprompt <text>` to customise it (requires Manage Server)."
            )

    # ── Slash commands ────────────────────────────────────────────────────────

    @app_commands.command(name="setprompt", description="Set a custom AI personality for this server")
    @app_commands.describe(prompt="The system prompt Jarvis will use in this server")
    @app_commands.guild_only()
    async def slash_setprompt(self, interaction: discord.Interaction, prompt: str):
        if not _has_manage_guild(interaction.user):
            await interaction.response.send_message(
                "🚫 You need the **Manage Server** permission to set a custom prompt.",
                ephemeral=True,
            )
            return
        if len(prompt) > MAX_PROMPT_LEN:
            await interaction.response.send_message(
                f"❌ Prompt is too long. Maximum is {MAX_PROMPT_LEN} characters.",
                ephemeral=True,
            )
            return
        set_guild_prompt(interaction.guild_id, prompt)
        await interaction.response.send_message(
            f"✅ Custom system prompt set for **{interaction.guild.name}**.\n"
            f"```\n{prompt[:200]}{'…' if len(prompt) > 200 else ''}\n```"
        )

    @app_commands.command(name="resetprompt", description="Reset this server's AI prompt to the default")
    @app_commands.guild_only()
    async def slash_resetprompt(self, interaction: discord.Interaction):
        if not _has_manage_guild(interaction.user):
            await interaction.response.send_message(
                "🚫 You need the **Manage Server** permission to reset the prompt.",
                ephemeral=True,
            )
            return
        existed = reset_guild_prompt(interaction.guild_id)
        if existed:
            await interaction.response.send_message(
                f"✅ Custom prompt removed. **{interaction.guild.name}** is back to the default Jarvis personality."
            )
        else:
            await interaction.response.send_message(
                "ℹ️ This server has no custom prompt — already using the default."
            )

    @app_commands.command(name="viewprompt", description="View the current AI system prompt for this server")
    @app_commands.guild_only()
    async def slash_viewprompt(self, interaction: discord.Interaction):
        prompt = get_guild_prompt(interaction.guild_id)
        if prompt:
            await interaction.response.send_message(
                f"📝 **Custom prompt for {interaction.guild.name}:**\n```\n{prompt}\n```"
            )
        else:
            await interaction.response.send_message(
                "📝 This server is using the **default Jarvis prompt**.\n"
                "Use `/setprompt` to customise it (requires Manage Server)."
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Prompts(bot))