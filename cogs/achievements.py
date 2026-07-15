"""
Achievement badges & earned titles.

This module has no commands of its own — it's a toolbox other cogs call
into right after something achievement-relevant happens (a chess win, a
song finishing, a streak bump, etc). It never touches Discord roles or
permissions, so it behaves identically in every server the bot is in.

Each achievement is:
  - a badge  (always shown in a user's /profile badge list once unlocked)
  - optionally also a title (an equip-able cosmetic label — granted
    automatically the moment the badge unlocks, no purchase needed)

To add a new achievement: add one entry to ACHIEVEMENTS below. Nothing
else needs to change — check_achievements() reads the list generically.
"""
from __future__ import annotations

from cogs.state import (
    get_game_stats, get_streak, get_stats,
    unlock_badge, grant_title, get_badges,
)

# ── Achievement catalog ───────────────────────────────────────────────────────
# id: {name, emoji, description, title (optional), metric(user_id) -> int, threshold}

ACHIEVEMENTS: dict[str, dict] = {
    "chess_novice": {
        "name": "Chess Novice", "emoji": "♟️",
        "description": "Win your first chess game.",
        "title": "♟️ Chess Novice",
        "metric": lambda uid: get_game_stats(uid)["chess_wins"], "threshold": 1,
    },
    "chess_master": {
        "name": "Chess Master", "emoji": "🏆",
        "description": "Win 10 chess games.",
        "title": "🏆 Chess Master",
        "metric": lambda uid: get_game_stats(uid)["chess_wins"], "threshold": 10,
    },
    "chess_grandmaster": {
        "name": "Chess Grandmaster", "emoji": "👑",
        "description": "Win 50 chess games.",
        "title": "👑 Chess Grandmaster",
        "metric": lambda uid: get_game_stats(uid)["chess_wins"], "threshold": 50,
    },
    "mafia_survivor": {
        "name": "Mafia Survivor", "emoji": "🎭",
        "description": "Win 5 Mafia games.",
        "title": "🎭 Mafia MVP",
        "metric": lambda uid: get_game_stats(uid)["mafia_wins"], "threshold": 5,
    },
    "mafia_kingpin": {
        "name": "Mafia Kingpin", "emoji": "🔫",
        "description": "Win 20 Mafia games.",
        "title": "🔫 Mafia Kingpin",
        "metric": lambda uid: get_game_stats(uid)["mafia_wins"], "threshold": 20,
    },
    "hangman_hero": {
        "name": "Hangman Hero", "emoji": "🪢",
        "description": "Solve 25 hangman rounds.",
        "title": "🪢 Hangman Hero",
        "metric": lambda uid: get_game_stats(uid)["hangman_wins"], "threshold": 25,
    },
    "loyal_week": {
        "name": "Week-Long Regular", "emoji": "🔥",
        "description": "Keep a 7-day chat streak going.",
        "title": None,
        "metric": lambda uid: get_streak(uid), "threshold": 7,
    },
    "loyal_month": {
        "name": "Monthly Loyalist", "emoji": "⭐",
        "description": "Keep a 30-day chat streak going.",
        "title": "⭐ Monthly Loyalist",
        "metric": lambda uid: get_streak(uid), "threshold": 30,
    },
    "loyal_100": {
        "name": "Centurion", "emoji": "🏵️",
        "description": "Keep a 100-day chat streak going.",
        "title": "🏵️ Centurion",
        "metric": lambda uid: get_streak(uid), "threshold": 100,
    },
    "loyal_year": {
        "name": "Year One", "emoji": "🎇",
        "description": "Keep a 365-day chat streak going.",
        "title": "🎇 Year One",
        "metric": lambda uid: get_streak(uid), "threshold": 365,
    },
    "chatterbox": {
        "name": "Chatterbox", "emoji": "💬",
        "description": "Send 500 messages to Jarvis.",
        "title": "💬 Chatterbox",
        "metric": lambda uid: (get_stats(uid) or {}).get("messages", 0), "threshold": 500,
    },
}


def check_achievements(user_id: int) -> list[dict]:
    """Re-check every achievement for `user_id` and unlock any that are now
    met but weren't before. Automatically grants the matching title (if
    any) the moment a badge unlocks. Returns the list of newly-unlocked
    achievement dicts (each with an added "id" key) so the caller can
    announce them — empty list if nothing new."""
    newly_unlocked = []
    already = set(get_badges(user_id))
    for badge_id, ach in ACHIEVEMENTS.items():
        if badge_id in already:
            continue
        try:
            if ach["metric"](user_id) >= ach["threshold"]:
                if unlock_badge(user_id, badge_id):
                    if ach.get("title"):
                        # Store the stable badge_id, not the display label —
                        # display strings (with emoji) are fragile to match
                        # exactly when a user types them back in /title.
                        grant_title(user_id, badge_id)
                    newly_unlocked.append({**ach, "id": badge_id})
        except Exception:
            continue  # never let a bad metric crash the caller's flow
    return newly_unlocked


# id -> display label, for every achievement that grants a title.
# Used by /profile, /titles, and /title to resolve a stored title id back
# to something human-readable (and to accept either form when equipping).
TITLE_LABELS: dict[str, str] = {
    badge_id: ach["title"] for badge_id, ach in ACHIEVEMENTS.items() if ach.get("title")
}

# ── System Breach Event Badge Titles ────────────────────────────────────────
# These are added here so event badges display properly on /profile even when
# the event cog is removed. The actual badge data is stored in state.py.

SYSTEM_BREACH_BADGE_LABELS: dict[str, str] = {
    "process_hunter": "🔎 Process Hunter",
    "daemon_wrangler": "🧩 Daemon Wrangler",
    "system_purger": "🔥 System Purger",
    "core_guardian": "👑 Core Guardian",
    "full_compliance": "📜 Full Compliance",
    "registry_complete": "🗂️ Registry Complete",
    "breach_contained": "🕸️ Breach Contained",
}

# Add to TITLE_LABELS so event titles resolve properly even without the cog
TITLE_LABELS.update(SYSTEM_BREACH_BADGE_LABELS)


def unlocked_announcement(user_display_name: str, achievements: list[dict]) -> str:
    """Build a short public message for newly unlocked achievements."""
    lines = [f"{a['emoji']} **{a['name']}**" for a in achievements]
    plural = "s" if len(achievements) != 1 else ""
    return (
        f"🎉 **{user_display_name}** unlocked new achievement{plural}!\n" + "\n".join(lines)
    )