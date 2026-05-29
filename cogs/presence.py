"""
Presence cog — rotates Jarvis's Discord activity/status on a clean schedule.

Statuses pull live data from:
  - seen_users  (same source as !usage → Seen Users field)
  - bot.guilds  (same source as !usage → Guilds field & !servers)

Cycle order (every INTERVAL seconds):
  1. Watching over {n} users          — CustomActivity-style watching
  2. In {n} servers                   — server count
  3. Listening to your questions       — personality touch
  4. Ready to help • /help            — call-to-action
"""

import asyncio
import discord
from discord.ext import commands, tasks
from cogs.state import seen_users

# ── Config ────────────────────────────────────────────────────────────────────

INTERVAL   = 20      # seconds per status slide
STATUS     = discord.Status.online   # online / idle / dnd

# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt(n: int) -> str:
    """Format large numbers with commas: 12345 → '12,345'."""
    return f"{n:,}"


def _build_activities(bot: commands.Bot) -> list[discord.BaseActivity]:
    """Build the rotation list fresh on every tick so counts stay accurate."""
    user_count   = len(seen_users)
    guild_count  = len(bot.guilds)

    return [
        # Slide 1 — user reach (Watching … gives the best professional look)
        discord.Activity(
            type=discord.ActivityType.watching,
            name=f"Over {_fmt(user_count)} users",
        ),
        # Slide 2 — server footprint
        discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{_fmt(guild_count)} server{'s' if guild_count != 1 else ''}",
        ),
        # Slide 3 — personality / brand
        discord.Activity(
            type=discord.ActivityType.listening,
            name="your questions",
        ),
        # Slide 4 — call-to-action
        discord.Activity(
            type=discord.ActivityType.playing,
            name="Ready to help • /help",
        ),
    ]


# ── Cog ───────────────────────────────────────────────────────────────────────

class Presence(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot   = bot
        self._idx  = 0
        self._rotation.start()

    def cog_unload(self):
        self._rotation.cancel()

    @tasks.loop(seconds=INTERVAL)
    async def _rotation(self):
        activities = _build_activities(self.bot)
        activity   = activities[self._idx % len(activities)]
        self._idx += 1
        try:
            await self.bot.change_presence(status=STATUS, activity=activity)
        except Exception as e:
            print(f"[Presence] Failed to update presence: {e}")

    @_rotation.before_loop
    async def _before_rotation(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Presence(bot))