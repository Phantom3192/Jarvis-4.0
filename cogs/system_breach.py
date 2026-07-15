"""
System Breach Event — Full Implementation (PARTS 1-12)

This is the main System Breach cog that consolidates all parts:
- PART 1: State layer (handled by cogs/state.py)
- PART 2: Spawning & Catching
- PART 3: Fusion & Scrap
- PART 4: Equip, Combine, and Perks
- PART 5: Trading
- PART 6: Quests
- PART 7: Power Mechanics
- PART 8: Badges, Engagement Days, and Leaderboard
- PART 9: The !event Hub
- PART 10: Raid Boss
- PART 11: Event Lifecycle & Closing Ceremony
- PART 12: Polish Pass
"""

import random
import time
import asyncio
import re
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone

# Import ONLY what we need from state.py (badge storage and base state functions)
from cogs.state import (
    add_credits,
    spend_credits,
    get_credits,
    _data,
    grant_system_breach_badge,
    get_system_breach_badges,
)

# ── For emoji display ──
JC_EMOJI = "🪙"

# ══════════════════════════════════════════════════════════════════════════════
# POWER & EVENT CONTROL FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def get_jarvis_power() -> float:
    """Return the current bot-wide Jarvis Power value (0.0–200.0)."""
    return max(0.0, min(200.0, _data.get("jarvis_power", {}).get("value", 0.0)))

def add_jarvis_power(amount: float) -> None:
    """Increment Jarvis Power (capped at 200). Auto-activates mid-boss at 50% and final boss at 100%."""
    if "jarvis_power" not in _data:
        _data["jarvis_power"] = {"value": 0.0, "last_snapshot": 0.0, "last_snapshot_time": 0.0, "dip_count": 0}
    current = _data["jarvis_power"].get("value", 0.0)
    new_value = min(200.0, max(0.0, current + amount))
    _data["jarvis_power"]["value"] = new_value
    
    # ── Auto-activate mid-boss when power reaches 50% ──
    if new_value >= 50.0 and not get_mid_boss_active() and not get_mid_boss_defeated():
        set_mid_boss_active(True)
        # No channel announcement - silent activation
    
    # ── Auto-activate final raid boss when power reaches 100% ──
    if new_value >= 100.0 and not get_raid_active():
        # Only activate final boss if mid-boss is defeated
        if get_mid_boss_defeated() or get_mid_boss_hp() <= 0:
            set_raid_active(True)
    
    from cogs.state import _schedule_save
    _schedule_save("jarvis_power")

def reset_jarvis_power() -> None:
    """Reset Jarvis Power to 0 (called when event starts)."""
    _data["jarvis_power"] = {"value": 0.0, "last_snapshot": 0.0, "last_snapshot_time": 0.0, "dip_count": 0}
    from cogs.state import _schedule_save
    _schedule_save("jarvis_power")

def set_event_active(active: bool, ends_at: float | None = None) -> None:
    """Set the event active/inactive."""
    old_event = _data.get("event_data", {})
    _data["event_data"] = {
        "active": active,
        "ends_at": ends_at,
        "start_time": time.time() if active else old_event.get("start_time", 0.0),
        "dip_count": old_event.get("dip_count", 0),
    }
    from cogs.state import _schedule_save
    _schedule_save("event_data")

def get_event_data() -> dict:
    """Return event metadata from state."""
    return _data.get("event_data", {})

def get_daemon_collection(user_id: int) -> dict:
    """Return a copy of the user's daemon collection."""
    entry = _data.get("daemons", {}).get(str(user_id), {})
    return {
        "owned": dict(entry.get("owned", {})),
        "equipped": entry.get("equipped"),
        "combine_slots": list(entry.get("combine_slots", [None, None, None])),
    }

def add_daemon(user_id: int, species_id: str) -> str:
    """Add a new Daemon instance to the user's collection. Returns the instance id."""
    uid = str(user_id)
    if "daemons" not in _data:
        _data["daemons"] = {}
    entry = _data["daemons"].setdefault(uid, {"owned": {}, "equipped": None, "combine_slots": [None, None, None], "next_id": 1})
    instance_id = str(entry["next_id"])
    entry["next_id"] += 1
    entry["owned"][instance_id] = {"species": species_id, "caught_at": time.time()}
    from cogs.state import _schedule_save
    _schedule_save("daemons")
    return instance_id

def remove_daemon(user_id: int, instance_id: str) -> bool:
    """Remove a Daemon instance. Unequips it if it was equipped."""
    uid = str(user_id)
    entry = _data.get("daemons", {}).get(uid)
    if not entry or instance_id not in entry.get("owned", {}):
        return False
    del entry["owned"][instance_id]
    if entry.get("equipped") == instance_id:
        entry["equipped"] = None
    entry["combine_slots"] = [slot if slot != instance_id else None for slot in entry.get("combine_slots", [None, None, None])]
    from cogs.state import _schedule_save
    _schedule_save("daemons")
    return True

def equip_daemon(user_id: int, instance_id: str | None) -> bool:
    """Equip a single Daemon (or None to unequip)."""
    uid = str(user_id)
    if "daemons" not in _data:
        _data["daemons"] = {}
    entry = _data["daemons"].setdefault(uid, {"owned": {}, "equipped": None, "combine_slots": [None, None, None], "next_id": 1})
    if instance_id is not None and instance_id not in entry.get("owned", {}):
        return False
    entry["equipped"] = instance_id
    from cogs.state import _schedule_save
    _schedule_save("daemons")
    return True

def get_equipped_daemon(user_id: int) -> dict | None:
    """Return the currently equipped Daemon, or None."""
    uid = str(user_id)
    entry = _data.get("daemons", {}).get(uid, {})
    instance_id = entry.get("equipped")
    if instance_id is None or instance_id not in entry.get("owned", {}):
        return None
    data = dict(entry["owned"][instance_id])
    data["instance_id"] = instance_id
    return data

def set_combine_slots(user_id: int, slot0: str | None, slot1: str | None, slot2: str | None) -> bool:
    """Set the 3 combine slots. All must be owned or None."""
    uid = str(user_id)
    if "daemons" not in _data:
        _data["daemons"] = {}
    entry = _data["daemons"].setdefault(uid, {"owned": {}, "equipped": None, "combine_slots": [None, None, None], "next_id": 1})
    for slot in [slot0, slot1, slot2]:
        if slot is not None and slot not in entry.get("owned", {}):
            return False
    entry["combine_slots"] = [slot0, slot1, slot2]
    from cogs.state import _schedule_save
    _schedule_save("daemons")
    return True

def get_combine_slots(user_id: int) -> list[str | None]:
    """Return the 3 combine slots (may contain None)."""
    entry = _data.get("daemons", {}).get(str(user_id), {})
    return list(entry.get("combine_slots", [None, None, None]))

def get_daemon_quest(user_id: int) -> dict:
    """Get a copy of the user's quest progress."""
    return dict(_data.get("daemon_quests", {}).get(str(user_id), {}))

def bump_daemon_quest(user_id: int, quest_id: str, amount: int = 1) -> int:
    """Increment a quest progress counter. Returns the new progress value."""
    uid = str(user_id)
    if "daemon_quests" not in _data:
        _data["daemon_quests"] = {}
    q = _data["daemon_quests"].setdefault(uid, {})
    if quest_id not in q:
        q[quest_id] = {"progress": 0, "claimed": False}
    q[quest_id]["progress"] = q[quest_id].get("progress", 0) + amount
    from cogs.state import _schedule_save
    _schedule_save("daemon_quests")
    return q[quest_id]["progress"]

def mark_engagement_action(user_id: int) -> None:
    """Record that the user did a daemon-related action today."""
    from datetime import datetime, timezone
    uid = str(user_id)
    if "engagement_days" not in _data:
        _data["engagement_days"] = {}
    entry = _data["engagement_days"].setdefault(uid, {"active_days": [], "last_action": 0.0})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today not in entry["active_days"]:
        entry["active_days"].append(today)
    entry["last_action"] = time.time()
    from cogs.state import _schedule_save
    _schedule_save("engagement_days")

def get_engagement_days_count(user_id: int) -> int:
    """Return the number of distinct days the user has been active during the event."""
    entry = _data.get("engagement_days", {}).get(str(user_id), {})
    return len(entry.get("active_days", []))

def add_raid_damage(user_id: int, damage: int) -> int:
    """Add raid damage for a user. Returns new total damage."""
    uid = str(user_id)
    if "raid_stats" not in _data:
        _data["raid_stats"] = {}
    stats = _data["raid_stats"].setdefault(uid, {"damage": 0})
    stats["damage"] = stats.get("damage", 0) + damage
    from cogs.state import _schedule_save
    _schedule_save("raid_stats")
    return stats["damage"]

def get_raid_damage(user_id: int) -> int:
    """Get total raid damage for a user."""
    return _data.get("raid_stats", {}).get(str(user_id), {}).get("damage", 0)

def get_top_raid_contributors(limit: int = 10) -> list[tuple[int, int]]:
    """Get top N raid damage contributors as [(user_id, damage), ...]."""
    raid_stats = _data.get("raid_stats", {})
    sorted_users = sorted(
        [(int(uid), data.get("damage", 0)) for uid, data in raid_stats.items()],
        key=lambda x: x[1],
        reverse=True
    )
    return sorted_users[:limit]

def clear_system_breach_data(user_id: int) -> None:
    """Wipe all System Breach data for a user (called at event end)."""
    uid = str(user_id)
    _data.get("daemons", {}).pop(uid, None)
    _data.get("daemon_quests", {}).pop(uid, None)
    _data.get("engagement_days", {}).pop(uid, None)
    from cogs.state import _schedule_save
    _schedule_save("daemons")
    _schedule_save("daemon_quests")
    _schedule_save("engagement_days")

def get_participant_count() -> int:
    """Get number of unique users who have participated in the event."""
    return len(_data.get("daemon_quests", {}))

def get_time_remaining() -> str:
    """Get a human-readable time remaining string."""
    event = get_event_data()
    ends_at = event.get("ends_at", 0)
    if not ends_at:
        return "Unknown"
    
    remaining = ends_at - time.time()
    if remaining <= 0:
        return "Ended"
    
    days = int(remaining // 86400)
    hours = int((remaining % 86400) // 3600)
    minutes = int((remaining % 3600) // 60)
    
    if days > 0:
        return f"{days}d {hours}h"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m"


# ══════════════════════════════════════════════════════════════════════════════
# RAID STATE FUNCTIONS (for persistence)
# ══════════════════════════════════════════════════════════════════════════════

RAID_BOSS_HP = 10000  # Shared HP pool across all servers
RAID_DAMAGE_PER_HIT_MIN = 5
RAID_DAMAGE_PER_HIT_MAX = 15
RAID_HIT_COST = 25  # JC cost per raid hit
RAID_COOLDOWN_SECONDS = 300  # 5 minutes between hits per user

def get_raid_state() -> dict:
    """Get the current raid state from database."""
    return _data.get("raid_state", {
        "hp": RAID_BOSS_HP,
        "active": False,
        "last_hit": {},
        "start_time": 0,
        "final_blow_user": None
    })

def save_raid_state(hp: int, active: bool, last_hit: dict, start_time: int, final_blow_user: int = None) -> None:
    """Save raid state to database."""
    _data["raid_state"] = {
        "hp": hp,
        "active": active,
        "last_hit": last_hit,
        "start_time": start_time,
        "final_blow_user": final_blow_user
    }
    from cogs.state import _schedule_save
    _schedule_save("raid_state")

def reset_raid_state() -> None:
    """Reset raid state to default."""
    _data["raid_state"] = {
        "hp": RAID_BOSS_HP,
        "active": False,
        "last_hit": {},
        "start_time": 0,
        "final_blow_user": None
    }
    from cogs.state import _schedule_save
    _schedule_save("raid_state")

# ── Convenience functions for raid state ──
def get_raid_hp() -> int:
    return get_raid_state().get("hp", RAID_BOSS_HP)

def set_raid_hp(value: int) -> None:
    state = get_raid_state()
    state["hp"] = value
    save_raid_state(state["hp"], state["active"], state["last_hit"], state["start_time"], state["final_blow_user"])

def get_raid_active() -> bool:
    return get_raid_state().get("active", False)

def set_raid_active(value: bool) -> None:
    state = get_raid_state()
    state["active"] = value
    save_raid_state(state["hp"], state["active"], state["last_hit"], state["start_time"], state["final_blow_user"])

def get_raid_last_hit() -> dict:
    return get_raid_state().get("last_hit", {})

def set_raid_last_hit(user_id: int, timestamp: float) -> None:
    state = get_raid_state()
    if "last_hit" not in state:
        state["last_hit"] = {}
    state["last_hit"][str(user_id)] = timestamp
    save_raid_state(state["hp"], state["active"], state["last_hit"], state["start_time"], state["final_blow_user"])

def get_raid_start_time() -> int:
    return get_raid_state().get("start_time", 0)

def set_raid_start_time(value: int) -> None:
    state = get_raid_state()
    state["start_time"] = value
    save_raid_state(state["hp"], state["active"], state["last_hit"], state["start_time"], state["final_blow_user"])

def get_raid_final_blow_user() -> int | None:
    return get_raid_state().get("final_blow_user", None)

def set_raid_final_blow_user(value: int | None) -> None:
    state = get_raid_state()
    state["final_blow_user"] = value
    save_raid_state(state["hp"], state["active"], state["last_hit"], state["start_time"], state["final_blow_user"])

# ── Fusion Streak Persistence ──
def get_fusion_streak(user_id: int) -> int:
    return _data.get("fusion_streaks", {}).get(str(user_id), 0)

def set_fusion_streak(user_id: int, streak: int) -> None:
    if "fusion_streaks" not in _data:
        _data["fusion_streaks"] = {}
    _data["fusion_streaks"][str(user_id)] = streak
    from cogs.state import _schedule_save
    _schedule_save("fusion_streaks")

def reset_fusion_streak(user_id: int) -> None:
    if "fusion_streaks" in _data:
        _data["fusion_streaks"].pop(str(user_id), None)
        from cogs.state import _schedule_save
    _schedule_save("fusion_streaks")

# ── Stabilizer Used Persistence ──
def get_stabilizer_used(user_id: int) -> bool:
    return _data.get("stabilizer_used", {}).get(str(user_id), False)

def set_stabilizer_used(user_id: int, value: bool) -> None:
    if "stabilizer_used" not in _data:
        _data["stabilizer_used"] = {}
    _data["stabilizer_used"][str(user_id)] = value
    from cogs.state import _schedule_save
    _schedule_save("stabilizer_used")

# ── Raid Damage Bonus ──
def get_raid_damage_bonus() -> float:
    """Get damage bonus based on power above 100%."""
    power = get_jarvis_power()
    if power <= 100:
        return 1.0
    # Each 1% above 100 gives 2% bonus damage, max 200% bonus at 200% power
    bonus = 1.0 + ((power - 100) / 100) * 2
    return min(3.0, bonus)  # Cap at 3x damage (200% bonus)


# ══════════════════════════════════════════════════════════════════════════════
# CHANNEL WHITELIST FUNCTIONS (Per Server)
# ══════════════════════════════════════════════════════════════════════════════

def get_spawn_channels(guild_id: int) -> list[int]:
    """Get list of channel IDs where spawns are allowed for a specific guild."""
    guild_data = _data.get("spawn_channels", {}).get(str(guild_id), {})
    return guild_data.get("channels", [])

def add_spawn_channel(guild_id: int, channel_id: int) -> None:
    """Add a channel to the spawn whitelist for a specific guild."""
    if "spawn_channels" not in _data:
        _data["spawn_channels"] = {}
    
    guild_str = str(guild_id)
    if guild_str not in _data["spawn_channels"]:
        _data["spawn_channels"][guild_str] = {"channels": []}
    
    channels = _data["spawn_channels"][guild_str].get("channels", [])
    if channel_id not in channels:
        channels.append(channel_id)
        _data["spawn_channels"][guild_str]["channels"] = channels
        from cogs.state import _schedule_save
        _schedule_save("spawn_channels")

def remove_spawn_channel(guild_id: int, channel_id: int) -> None:
    """Remove a channel from the spawn whitelist for a specific guild."""
    guild_str = str(guild_id)
    if guild_str in _data.get("spawn_channels", {}):
        channels = _data["spawn_channels"][guild_str].get("channels", [])
        if channel_id in channels:
            channels.remove(channel_id)
            _data["spawn_channels"][guild_str]["channels"] = channels
            from cogs.state import _schedule_save
            _schedule_save("spawn_channels")

def is_spawn_channel(guild_id: int, channel_id: int) -> bool:
    """Check if a channel is whitelisted for spawns in a specific guild."""
    channels = get_spawn_channels(guild_id)
    # If no channels are whitelisted, spawns work everywhere
    if not channels:
        return True
    return channel_id in channels


# ══════════════════════════════════════════════════════════════════════════════
# MID-BOSS FIGHT (Activates at 50% Power)
# ══════════════════════════════════════════════════════════════════════════════

MID_BOSS_HP = 3500
MID_BOSS_DAMAGE_PER_HIT_MIN = 5
MID_BOSS_DAMAGE_PER_HIT_MAX = 10
MID_BOSS_HIT_COST = 10
MID_BOSS_COOLDOWN_SECONDS = 120  # 2 minutes between hits

def get_mid_boss_state() -> dict:
    """Get the current mid-boss state from database."""
    return _data.get("mid_boss_state", {
        "hp": MID_BOSS_HP,
        "active": False,
        "defeated": False,
        "last_hit": {},
        "final_blow_user": None
    })

def save_mid_boss_state(hp: int, active: bool, defeated: bool, last_hit: dict, final_blow_user: int = None) -> None:
    """Save mid-boss state to database."""
    _data["mid_boss_state"] = {
        "hp": hp,
        "active": active,
        "defeated": defeated,
        "last_hit": last_hit,
        "final_blow_user": final_blow_user
    }
    from cogs.state import _schedule_save
    _schedule_save("mid_boss_state")

def reset_mid_boss_state() -> None:
    """Reset mid-boss state to default."""
    _data["mid_boss_state"] = {
        "hp": MID_BOSS_HP,
        "active": False,
        "defeated": False,
        "last_hit": {},
        "final_blow_user": None
    }
    from cogs.state import _schedule_save
    _schedule_save("mid_boss_state")

def get_mid_boss_hp() -> int:
    return get_mid_boss_state().get("hp", MID_BOSS_HP)

def set_mid_boss_hp(value: int) -> None:
    state = get_mid_boss_state()
    state["hp"] = max(0, value)
    save_mid_boss_state(state["hp"], state["active"], state["defeated"], state["last_hit"], state["final_blow_user"])

def get_mid_boss_active() -> bool:
    return get_mid_boss_state().get("active", False)

def set_mid_boss_active(value: bool) -> None:
    state = get_mid_boss_state()
    state["active"] = value
    save_mid_boss_state(state["hp"], state["active"], state["defeated"], state["last_hit"], state["final_blow_user"])

def get_mid_boss_defeated() -> bool:
    return get_mid_boss_state().get("defeated", False)

def set_mid_boss_defeated(value: bool) -> None:
    state = get_mid_boss_state()
    state["defeated"] = value
    save_mid_boss_state(state["hp"], state["active"], state["defeated"], state["last_hit"], state["final_blow_user"])

def get_mid_boss_last_hit() -> dict:
    return get_mid_boss_state().get("last_hit", {})

def set_mid_boss_last_hit(user_id: int, timestamp: float) -> None:
    state = get_mid_boss_state()
    if "last_hit" not in state:
        state["last_hit"] = {}
    state["last_hit"][str(user_id)] = timestamp
    save_mid_boss_state(state["hp"], state["active"], state["defeated"], state["last_hit"], state["final_blow_user"])

def get_mid_boss_final_blow_user() -> int | None:
    return get_mid_boss_state().get("final_blow_user", None)

def set_mid_boss_final_blow_user(value: int | None) -> None:
    state = get_mid_boss_state()
    state["final_blow_user"] = value
    save_mid_boss_state(state["hp"], state["active"], state["defeated"], state["last_hit"], state["final_blow_user"])


# ══════════════════════════════════════════════════════════════════════════════
# GLOBAL RAID QUEST TRACKING
# ══════════════════════════════════════════════════════════════════════════════

def get_raid_global_progress() -> dict:
    """Get global raid progress for raid quests."""
    return _data.get("raid_global_progress", {
        "total_damage": 0,
        "participants": [],
        "defeated": False,
    })

def add_raid_global_damage(amount: int) -> int:
    """Add to global raid damage. Returns new total."""
    progress = get_raid_global_progress()
    progress["total_damage"] = progress.get("total_damage", 0) + amount
    _data["raid_global_progress"] = progress
    from cogs.state import _schedule_save
    _schedule_save("raid_global_progress")
    return progress["total_damage"]

def add_raid_participant(user_id: int) -> None:
    """Add a user to raid participants."""
    progress = get_raid_global_progress()
    if "participants" not in progress:
        progress["participants"] = []
    if user_id not in progress["participants"]:
        progress["participants"].append(user_id)
    _data["raid_global_progress"] = progress
    from cogs.state import _schedule_save
    _schedule_save("raid_global_progress")

def get_raid_participant_count() -> int:
    """Get number of unique raid participants."""
    progress = get_raid_global_progress()
    return len(progress.get("participants", []))

def set_raid_defeated(value: bool) -> None:
    """Set whether the raid has been defeated globally."""
    progress = get_raid_global_progress()
    progress["defeated"] = value
    _data["raid_global_progress"] = progress
    from cogs.state import _schedule_save
    _schedule_save("raid_global_progress")

def get_raid_defeated() -> bool:
    """Check if the raid has been defeated globally."""
    return get_raid_global_progress().get("defeated", False)


# ══════════════════════════════════════════════════════════════════════════════
# CHECK INVITE QUEST
# ══════════════════════════════════════════════════════════════════════════════

def _check_invite_quest(user_id: int) -> None:
    """Check if user has referred someone and update invite quest progress."""
    from cogs.state import get_referral_count
    
    event = get_event_data()
    if not event.get("active"):
        return
    
    quest_data = get_daemon_quest(user_id)
    if quest_data.get("invite_user", {}).get("claimed", False):
        return
    
    referral_count = get_referral_count(user_id)
    if referral_count > 0:
        current_progress = quest_data.get("invite_user", {}).get("progress", 0)
        if current_progress < referral_count:
            bump_daemon_quest(user_id, "invite_user", referral_count - current_progress)


# ══════════════════════════════════════════════════════════════════════════════
# PERK TRIGGERS (UPDATED with reduced chances)
# ══════════════════════════════════════════════════════════════════════════════

def _trigger_glitch(user_id: int, channel) -> None:
    """#2: Glitch's Echo Chance Perk — 4% chance for +5 JC."""
    if random.random() < 0.04:
        add_credits(user_id, 5)
        asyncio.create_task(channel.send(f"🌀 **Glitch** echoed your message! +5 {JC_EMOJI}"))

def _trigger_saboteur(user_id: int, channel) -> None:
    """#6: Saboteur Drain Effect — 10% chance to drain (SILENT when nothing happens)."""
    if random.random() < 0.10:
        drain = random.randint(1, 5)
        if spend_credits(user_id, drain):
            asyncio.create_task(channel.send(f"🔴 **Saboteur** drained **{drain}** {JC_EMOJI}!"))
        else:
            asyncio.create_task(channel.send(f"🔴 **Saboteur** tried to drain you, but you had no {JC_EMOJI}!"))
    # Removed the "nothing happened" message - now SILENT

def _trigger_wildcard(user_id: int, channel) -> None:
    """#7: Wildcard Gamble Effect — 30% chance of +10 JC, 70% chance of -5 JC."""
    if random.random() < 0.30:
        add_credits(user_id, 10)
        asyncio.create_task(channel.send(f"🎲 **Wildcard** rolled high! You gained +10 {JC_EMOJI}!"))
    else:
        if spend_credits(user_id, 5):
            asyncio.create_task(channel.send(f"🎲 **Wildcard** rolled low! You lost 5 {JC_EMOJI}!"))
        else:
            asyncio.create_task(channel.send(f"🎲 **Wildcard** rolled low, but you had no {JC_EMOJI} to lose!"))


# ══════════════════════════════════════════════════════════════════════════════
# PERK DESCRIPTION HELPER (UPDATED with new rates)
# ══════════════════════════════════════════════════════════════════════════════

def _get_perk_description(perk_name: str) -> str:
    """Get a short description of what a perk does."""
    perk_descriptions = {
        "echo_chance": "4% chance to echo messages for +5 JC",
        "catch_priority": "10% chance to get priority on spawns",
        "catch_discount": "15% chance for free catch attempts",
        "duplicate_catch": "12% chance to duplicate caught Daemons",
        "fee_waive": "20% chance catch attempts are free",
        "catch_refund": "18% chance to refund JC on failed catches",
        "fail_reroll": "25% chance to reroll a failed catch",
        "ai_bonus": "+60 AI messages per day",
        "fusion_discount": "Fusion costs 2 duplicates instead of 3",
        "stabilizer": "Next fusion is guaranteed (one-time per equip)",
        "saboteur_drain": "10% chance to randomly drain small JC",
        "wildcard_gamble": "30% chance of +10 JC, 70% chance of -5 JC",
        "perk_amplify": "+20% to all other active perks",
    }
    return perk_descriptions.get(perk_name, perk_name.replace("_", " ").title() or "No perk")


# ══════════════════════════════════════════════════════════════════════════════
# END-OF-EVENT PAYOUT (UPDATED)
# ══════════════════════════════════════════════════════════════════════════════

PAYOUT_RATES = {
    "common": 25,
    "rare": 100,
    "legendary": 1000,
    "mythic": 2000,
}

def _calculate_payout(user_id: int) -> int:
    """Calculate JC payout for remaining daemons at event end."""
    coll = get_daemon_collection(user_id)
    owned = coll.get("owned", {})
    
    total = 0
    for daemon_data in owned.values():
        species_id = daemon_data.get("species")
        daemon = DAEMON_POOL.get(species_id, {})
        rarity = daemon.get("rarity", "common")
        total += PAYOUT_RATES.get(rarity, 10)
    
    return total


# ══════════════════════════════════════════════════════════════════════════════
# DAEMON POOL — 10 species + 2 mystery WITH PERKS
# ══════════════════════════════════════════════════════════════════════════════

RARITY_ORDER = ["common", "rare", "legendary", "mythic"]

RARITY_COLOR = {
    "common":    0x95A5A6,
    "rare":      0x3498DB,
    "legendary": 0xF1C40F,
    "mythic":    0xE74C3C,
}

CATCH_SUCCESS_RATE = {
    "common": 0.70,
    "rare":   0.40,
    "legendary": 0.15,
}

SPAWN_WEIGHT = {
    "common": 70,
    "rare":   25,
    "legendary": 5,
}

CATCH_COST = 50
SCRAP_VALUE = 10
COMBINE_COST_PER_SLOT = 100

DAEMON_POOL = {
    "glitch_common": {
        "name": "Glitch",
        "emoji": "🌀",
        "rarity": "common",
        "flavor": "A stuttering visual artifact — harmless, but it won't stop repeating itself.",
        "perk": "echo_chance",
    },
    "imp_common": {
        "name": "Imp",
        "emoji": "👹",
        "rarity": "common",
        "flavor": "A minor devil-process, mischievous and everywhere.",
        "perk": "catch_priority",
    },
    "cipher_rare": {
        "name": "Cipher",
        "emoji": "🔐",
        "rarity": "rare",
        "flavor": "Encrypts everything it touches — including its own price tag.",
        "perk": "catch_discount",
    },
    "mirror_rare": {
        "name": "Mirror",
        "emoji": "🪞",
        "rarity": "rare",
        "flavor": "Duplicates whatever data it's near.",
        "perk": "duplicate_catch",
    },
    "wraith_rare": {
        "name": "Wraith",
        "emoji": "👻",
        "rarity": "rare",
        "flavor": "A ghost process — deletes its own traces, fees included.",
        "perk": "fee_waive",
    },
    "reaper_rare": {
        "name": "Reaper",
        "emoji": "⚰️",
        "rarity": "rare",
        "flavor": "`kill -9` incarnate — cleanly terminates what it touches.",
        "perk": "catch_refund",
    },
    "sentinel_legendary": {
        "name": "Sentinel",
        "emoji": "🛡️",
        "rarity": "legendary",
        "flavor": "A corrupted guard process — some of its old protective instincts survived.",
        "perk": "fail_reroll",
    },
    "corebreaker_legendary": {
        "name": "Corebreaker",
        "emoji": "💥",
        "rarity": "legendary",
        "flavor": "Tore straight into Jarvis's core. Visibly destabilizes things.",
        "perk": "ai_bonus",
    },
    "possessor_legendary": {
        "name": "Possessor",
        "emoji": "👁️",
        "rarity": "legendary",
        "flavor": "Corrupts permissions, not data. Infiltrates without leaving traces.",
        "perk": "fusion_discount",
    },
    "stabilizer_legendary": {
        "name": "Stabilizer",
        "emoji": "💠",
        "rarity": "legendary",
        "flavor": "The one process that never destabilizes — Jarvis borrows its integrity to hold a fusion together.",
        "perk": "stabilizer",
    },
    "architect_mythic": {
        "name": "Architect",
        "emoji": "🕸️",
        "rarity": "mythic",
        "flavor": "The one orchestrating the whole breach. Raid-exclusive — never spawns in the wild.",
        "perk": "perk_amplify",
    },
    "saboteur_rare": {
        "name": "Saboteur",
        "emoji": "🔴",
        "rarity": "rare",
        "flavor": "Unknown. Equip to identify.",
        "perk": "saboteur_drain",
        "mystery": True,
    },
    "wildcard_rare": {
        "name": "Wildcard",
        "emoji": "🎲",
        "rarity": "rare",
        "flavor": "Unknown. Equip to identify.",
        "perk": "wildcard_gamble",
        "mystery": True,
    },
}

WILD_SPECIES = [sid for sid, d in DAEMON_POOL.items() if d["rarity"] != "mythic"]

# ══════════════════════════════════════════════════════════════════════════════
# SPAWN STATE (in-memory - these reset on restart, but that's OK)
# ══════════════════════════════════════════════════════════════════════════════

_spawn_streak: dict[int, int] = {}  # channel_id → spawn streak (resets on restart)
_last_spawn_time: dict[int, float] = {}  # channel_id → last spawn time (resets on restart)
_pending_spawns = {}  # Resets on restart (daemons despawn anyway)
_last_spawn_attempt = {}  # Resets on restart
_mystery_revealed = {}  # Resets on restart (users can re-identify)
_pending_trades = {}  # Resets on restart (trades expire)
_pending_sales = {}  # Resets on restart (sales expire)
_event_hub_messages = {}  # Resets on restart


# ══════════════════════════════════════════════════════════════════════════════
# QUESTS — Power, Combat, and Raid Quests
# ══════════════════════════════════════════════════════════════════════════════

POWER_QUESTS = [
    "chat_ai",
    "say_breach",
    "mystery_box",
    "chat_streak_3",
    "invite_user",
]

COMBAT_QUESTS = [
    "hit_raid",
    "trade_daemon",
    "catch_chain_1",
    "catch_chain_2",
    "catch_chain_3",
    "catch_chain_4",
    "catch_chain_5",
    "catch_chain_6",
    "catch_mystery",
    "fuse_once",
    "combine_3",
]

# ── RAID QUESTS (Global - Bot-wide progression) ──
RAID_QUESTS = [
    "raid_damage_2500",
    "raid_damage_5000",
    "raid_damage_7500",
    "raid_damage_10000",
    "raid_defeat",
    "raid_participate_25",
    "raid_participate_50",
]

# ── INDIVIDUAL RAID QUESTS (Personal progression) ──
INDIVIDUAL_RAID_QUESTS = [
    "personal_damage_100",
    "personal_damage_500",
    "personal_damage_1000",
    "personal_damage_5000",
    "personal_hits_10",
    "personal_hits_50",
    "personal_final_blow",
]

QUEST_DEFS = {
    # POWER QUESTS
    "chat_ai": {
        "name": "Chat with Jarvis",
        "tier": "power",
        "type": "daily",
        "description": "Send 50 messages to Jarvis AI",
        "goal": 50,
        "reward_jc": 100,
        "reward_power": 0.1,
    },
    "say_breach": {
        "name": "Speak of the Breach",
        "tier": "power",
        "type": "daily",
        "description": "Say 'system breach' in any channel",
        "goal": 1,
        "reward_jc": 50,
        "reward_power": 0.3,
    },
    "mystery_box": {
        "name": "Open a Mystery Box",
        "tier": "power",
        "type": "daily",
        "description": "Open a mystery box for a random reward",
        "goal": 1,
        "reward_jc": 50,
        "reward_power": 0.1,
    },
    "chat_streak_3": {
        "name": "Chat Streak (3 days)",
        "tier": "power",
        "type": "set",
        "description": "Chat with Jarvis AI on 3 different days",
        "goal": 3,
        "reward_jc": 200,
        "reward_power": 1.0, 
    },
    "invite_user": {
        "name": "Invite a New User",
        "tier": "power",
        "type": "one_time",
        "description": "Invite a new user to Jarvis via your referral code",
        "goal": 1,
        "reward_jc": 500,
        "reward_power": 1.0,  
    },
    
    # COMBAT QUESTS
    "hit_raid": {
        "name": "Strike the Breach",
        "tier": "combat",
        "type": "daily",
        "description": "Hit the raid boss once",
        "goal": 1,
        "reward_jc": 50,
        "reward_power": 0.5,
        "unlock_power": 25,
    },
    "trade_daemon": {
        "name": "Trade a Daemon",
        "tier": "combat",
        "type": "daily",
        "description": "Complete a successful daemon trade",
        "goal": 1,
        "reward_jc": 50,
        "reward_power": 0.5,
        "unlock_power": 25,
    },
    "catch_chain_1": {
        "name": "Catch 1 Daemon",
        "tier": "combat",
        "type": "chain",
        "description": "Catch 1 daemon (Chain 1/3)",
        "goal": 1,
        "reward_jc": 30,
        "reward_power": 0.5, 
        "unlock_power": 25,
        "next_quest": "catch_chain_2",
    },
    "catch_chain_2": {
        "name": "Catch 5 Daemons",
        "tier": "combat",
        "type": "chain",
        "description": "Catch 5 daemons (Chain 2/3)",
        "goal": 5,
        "reward_jc": 50,
        "reward_power": 1.0,
        "unlock_power": 25,
        "next_quest": "catch_chain_3",
    },
    "catch_chain_3": {
        "name": "Catch 10 Daemons",
        "tier": "combat",
        "type": "chain",
        "description": "Catch 10 daemons (Chain 3/3)",
        "goal": 10,
        "reward_jc": 80,
        "reward_power": 1.0, 
        "unlock_power": 25,
        "next_quest": "catch_chain_4",
    },
    "catch_chain_4": {
        "name": "Catch 25 Daemons",
        "tier": "combat",
        "type": "chain",
        "description": "Catch 25 daemons",
        "goal": 25,
        "reward_jc": 100,
        "reward_power": 1.5,
        "unlock_power": 25,
        "next_quest": "catch_chain_5",
    },
    "catch_chain_5": {
        "name": "Catch 50 Daemons",
        "tier": "combat",
        "type": "chain",
        "description": "Catch 50 daemons",
        "goal": 50,
        "reward_jc": 150,
        "reward_power": 2.0,
        "unlock_power": 25,
        "next_quest": "catch_chain_6",
    },
    "catch_chain_6": {
        "name": "Catch 100 Daemons",
        "tier": "combat",
        "type": "chain",
        "description": "Catch 100 daemons",
        "goal": 100,
        "reward_jc": 200,
        "reward_power": 3.0,
        "unlock_power": 25,
        "next_quest": None,
    },
    "catch_mystery": {
        "name": "Catch a Mystery",
        "tier": "combat",
        "type": "set",
        "description": "Catch Saboteur or Wildcard",
        "goal": 1,
        "reward_jc": 100,
        "reward_power": 1.0, 
        "unlock_power": 25,
    },
    "fuse_once": {
        "name": "Fuse a Daemon",
        "tier": "combat",
        "type": "set",
        "description": "Complete one fusion",
        "goal": 1,
        "reward_jc": 50,
        "reward_power": 0.8,
        "unlock_power": 25,
    },
    "combine_3": {
        "name": "Combine 3 Daemons",
        "tier": "combat",
        "type": "set",
        "description": "Combine all 3 slots (requires 50% Power)",
        "goal": 1,
        "reward_jc": 50,
        "reward_power": 1.2,
        "unlock_power": 50,
    },
    
    # ── GLOBAL RAID QUESTS ──
    "raid_damage_2500": {
        "name": "Raid Damage (2,500)",
        "tier": "raid_global",
        "type": "raid_global",
        "description": "Deal 2,500 total damage to the raid boss (global)",
        "goal": 2500,
        "reward_jc": 50,
        "reward_power": 1.0,
        "unlock_power": 100,
        "next_quest": "raid_damage_5000",
    },
    "raid_damage_5000": {
        "name": "Raid Damage (5,000)",
        "tier": "raid_global",
        "type": "raid_global",
        "description": "Deal 5,000 total damage to the raid boss (global)",
        "goal": 5000,
        "reward_jc": 100,
        "reward_power": 2.0,
        "unlock_power": 100,
        "next_quest": "raid_damage_7500",
    },
    "raid_damage_7500": {
        "name": "Raid Damage (7,500)",
        "tier": "raid_global",
        "type": "raid_global",
        "description": "Deal 7,500 total damage to the raid boss (global)",
        "goal": 7500,
        "reward_jc": 200,
        "reward_power": 3.0,
        "unlock_power": 100,
        "next_quest": "raid_damage_10000",
    },
    "raid_damage_10000": {
        "name": "Raid Damage (10,000)",
        "tier": "raid_global",
        "type": "raid_global",
        "description": "Deal 10,000 total damage to the raid boss (global)",
        "goal": 10000,
        "reward_jc": 400,
        "reward_power": 5.0,
        "unlock_power": 100,
        "next_quest": "raid_defeat",
    },
    "raid_defeat": {
        "name": "Defeat the Architect",
        "tier": "raid_global",
        "type": "raid_global",
        "description": "Defeat the raid boss (global)",
        "goal": 1,
        "reward_jc": 1000,
        "reward_power": 10.0,
        "unlock_power": 100,
        "next_quest": "raid_participate_25",
    },
    "raid_participate_25": {
        "name": "Raid Participation (25)",
        "tier": "raid_global",
        "type": "raid_global",
        "description": "25 unique players must participate in the raid (global)",
        "goal": 25,
        "reward_jc": 1000,
        "reward_power": 8.0,
        "unlock_power": 100,
        "next_quest": "raid_participate_50",
    },
    "raid_participate_50": {
        "name": "Raid Participation (50)",
        "tier": "raid_global",
        "type": "raid_global",
        "description": "50 unique players must participate in the raid (global)",
        "goal": 50,
        "reward_jc": 2000,
        "reward_power": 15.0,
        "unlock_power": 100,
        "next_quest": None,
    },
    
    # ── INDIVIDUAL RAID QUESTS ──
    "personal_damage_100": {
        "name": "Personal Damage (100)",
        "tier": "raid_individual",
        "type": "raid_individual",
        "description": "Deal 100 damage to the raid boss personally",
        "goal": 100,
        "reward_jc": 50,
        "reward_power": 1.0,
        "unlock_power": 100,
        "next_quest": "personal_damage_500",
    },
    "personal_damage_500": {
        "name": "Personal Damage (500)",
        "tier": "raid_individual",
        "type": "raid_individual",
        "description": "Deal 500 damage to the raid boss personally",
        "goal": 500,
        "reward_jc": 150,
        "reward_power": 2.0,
        "unlock_power": 100,
        "next_quest": "personal_damage_1000",
    },
    "personal_damage_1000": {
        "name": "Personal Damage (1,000)",
        "tier": "raid_individual",
        "type": "raid_individual",
        "description": "Deal 1,000 damage to the raid boss personally",
        "goal": 1000,
        "reward_jc": 300,
        "reward_power": 3.0,
        "unlock_power": 100,
        "next_quest": "personal_damage_5000",
    },
    "personal_damage_5000": {
        "name": "Personal Damage (5,000)",
        "tier": "raid_individual",
        "type": "raid_individual",
        "description": "Deal 5,000 damage to the raid boss personally",
        "goal": 5000,
        "reward_jc": 800,
        "reward_power": 5.0,
        "unlock_power": 100,
        "next_quest": None,
    },
    "personal_hits_10": {
        "name": "Personal Hits (10)",
        "tier": "raid_individual",
        "type": "raid_individual",
        "description": "Hit the raid boss 10 times",
        "goal": 10,
        "reward_jc": 100,
        "reward_power": 1.0,
        "unlock_power": 100,
        "next_quest": "personal_hits_50",
    },
    "personal_hits_50": {
        "name": "Personal Hits (50)",
        "tier": "raid_individual",
        "type": "raid_individual",
        "description": "Hit the raid boss 50 times",
        "goal": 50,
        "reward_jc": 400,
        "reward_power": 3.0,
        "unlock_power": 100,
        "next_quest": None,
    },
    "personal_final_blow": {
        "name": "Final Blow",
        "tier": "raid_individual",
        "type": "raid_individual",
        "description": "Land the final blow on the raid boss",
        "goal": 1,
        "reward_jc": 1000,
        "reward_power": 10.0,
        "unlock_power": 100,
        "next_quest": None,
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# BADGE DEFINITIONS (PART 8) with #6: Raid Participant Badge
# ══════════════════════════════════════════════════════════════════════════════

BADGE_DEFS = {
    # Engagement Ladder (by catch count)
    "process_hunter": {
        "name": "Process Hunter",
        "emoji": "🔎",
        "description": "Caught 10+ Daemons",
        "requirement": "catch_count",
        "threshold": 10,
    },
    "daemon_wrangler": {
        "name": "Daemon Wrangler",
        "emoji": "🧩",
        "description": "Caught 25+ Daemons",
        "requirement": "catch_count",
        "threshold": 25,
    },
    "system_purger": {
        "name": "System Purger",
        "emoji": "🔥",
        "description": "Caught 50+ Daemons",
        "requirement": "catch_count",
        "threshold": 50,
    },
    "core_guardian": {
        "name": "Core Guardian",
        "emoji": "👑",
        "description": "Caught 100+ Daemons",
        "requirement": "catch_count",
        "threshold": 100,
    },
    # Other Badges
    "full_compliance": {
        "name": "Full Compliance",
        "emoji": "📜",
        "description": "Completed every quest at least once",
        "requirement": "all_quests_done",
        "threshold": 1,
    },
    "registry_complete": {
        "name": "Registry Complete",
        "emoji": "🗂️",
        "description": "Caught every species at least once",
        "requirement": "registry_complete",
        "threshold": 1,
    },
    "breach_contained": {
        "name": "Breach Contained",
        "emoji": "🕸️",
        "description": "Contributed to the raid boss fight",
        "requirement": "raid_damage",
        "threshold": 1,
    },
    # ── #6: Raid Participant Badge ──
    "raid_participant": {
        "name": "Raid Participant",
        "emoji": "🛡️",
        "description": "Hit the raid boss at least once",
        "requirement": "raid_participant",
        "threshold": 1,
    },
}

# ══════════════════════════════════════════════════════════════════════════════
# CATCH RATE CURVES — Power-driven
# ══════════════════════════════════════════════════════════════════════════════

POWER_BREAKPOINTS = [0, 25, 50, 75, 100]

CATCH_CURVE = {
    "common": [0.05, 0.25, 0.50, 0.65, 0.70],
    "rare": [0.01, 0.10, 0.25, 0.35, 0.40],
    "legendary": [0.00, 0.03, 0.08, 0.12, 0.15],
}

FUSION_CURVE = [0.30, 0.55, 0.80, 0.95, 1.00]

# ══════════════════════════════════════════════════════════════════════════════
# SPAWN SETTINGS (UPDATED)
# ══════════════════════════════════════════════════════════════════════════════

SPAWN_CHANCE_PER_MESSAGE = 0.05
SPAWN_CHANNEL_COOLDOWN = 60   
SPAWN_LIFETIME = 60         

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _interp(values: list[float], power: float) -> float:
    """Linear interpolation across POWER_BREAKPOINTS."""
    power = max(0.0, min(100.0, power))
    for i in range(len(POWER_BREAKPOINTS) - 1):
        lo, hi = POWER_BREAKPOINTS[i], POWER_BREAKPOINTS[i + 1]
        if lo <= power <= hi:
            t = (power - lo) / (hi - lo) if hi != lo else 0.0
            return values[i] * (1 - t) + values[i + 1] * t
    return values[-1]


def catch_success_rate(rarity: str, power: float) -> float:
    """Catch success % at current Power."""
    if rarity not in CATCH_CURVE:
        return 0.0
    return _interp(CATCH_CURVE[rarity], power)


def fusion_success_rate(power: float) -> float:
    """Fusion success % at current Power."""
    return _interp(FUSION_CURVE, power)


def _pick_species() -> str:
    """Choose random species, weighted by rarity."""
    weights = [SPAWN_WEIGHT[DAEMON_POOL[s]["rarity"]] for s in WILD_SPECIES]
    return random.choices(WILD_SPECIES, weights=weights, k=1)[0]


def _species_embed(species_id: str, spawned: bool = False, revealed_perk: str = None) -> discord.Embed:
    """Create embed for a Daemon."""
    d = DAEMON_POOL[species_id]
    title = f"{d['emoji']} A rogue process has manifested!" if spawned else f"{d['emoji']} {d['name']}"
    embed = discord.Embed(
        title=title,
        description=f"**{d['name']}** ({d['rarity'].title()})\n_{d['flavor']}_",
        color=RARITY_COLOR[d["rarity"]],
    )
    if revealed_perk:
        embed.add_field(name="🔍 Identified", value=f"Perk: **{revealed_perk}**", inline=False)
    if spawned:
        power = get_jarvis_power()
        rate = catch_success_rate(d["rarity"], power)
        embed.add_field(
            name="Catch it!",
            value=f"Type `!catch` — costs **{CATCH_COST} 🪙**, success rate: **{round(rate*100)}%**",
            inline=False,
        )
        embed.set_footer(text=f"Jarvis Power: {round(power)}% | You have {SPAWN_LIFETIME}s")
    return embed


def get_live_daemon_perks(user_id: int) -> dict:
    """Get active daemon perks that flow through get_active_perks()."""
    equipped = get_equipped_daemon(user_id)
    if equipped is None:
        return {}
    
    species_id = equipped["species"]
    daemon = DAEMON_POOL.get(species_id, {})
    perk = daemon.get("perk")
    
    if perk == "ai_bonus":
        return {"daily_ai_limit_bonus": 60}
    
    return {}


def get_power_tier(power: float) -> str:
    """Get power tier name from power percentage."""
    if power < 25:
        return "fragmented"
    elif power < 50:
        return "stabilizing"
    elif power < 75:
        return "reinforced"
    elif power < 100:
        return "counter_offensive"
    else:
        return "contained"


def get_power_tier_emoji(power: float) -> str:
    """Get emoji for power tier."""
    if power < 25:
        return "🌀"
    elif power < 50:
        return "🔧"
    elif power < 75:
        return "⚡"
    elif power < 100:
        return "👑"
    else:
        return "✅"


def _resolve_user(ctx: commands.Context, user_input: str) -> discord.User | None:
    """Resolve a user from mention, ID, or name."""
    mention_match = re.match(r"<@!?(\d+)>", user_input.strip())
    if mention_match:
        user_id = int(mention_match.group(1))
        try:
            return ctx.bot.get_user(user_id) or ctx.guild.get_member(user_id)
        except:
            return None
    
    if user_input.strip().isdigit():
        user_id = int(user_input.strip())
        try:
            return ctx.bot.get_user(user_id) or ctx.guild.get_member(user_id)
        except:
            return None
    
    if ctx.guild:
        for member in ctx.guild.members:
            if member.name.lower() == user_input.lower() or member.display_name.lower() == user_input.lower():
                return member
    
    return None

def _get_user_catch_count(user_id: int) -> int:
    """Get total catch count for a user."""
    quest_data = get_daemon_quest(user_id)
    return quest_data.get("total_catches", {}).get("progress", 0)

def _get_user_registry_progress(user_id: int) -> tuple[int, int]:
    """Get registry progress (caught_species, total_species)."""
    coll = get_daemon_collection(user_id)
    owned = coll.get("owned", {})
    caught_species = set()
    for daemon_data in owned.values():
        species = daemon_data.get("species")
        if species:
            caught_species.add(species)
    # Exclude mythic from registry (raid-exclusive)
    total_species = len([s for s in DAEMON_POOL if DAEMON_POOL[s]["rarity"] != "mythic"])
    return len(caught_species), total_species


def _check_badges(user_id: int) -> list[str]:
    """Check and award any badges the user qualifies for."""
    catch_count = _get_user_catch_count(user_id)
    caught, total = _get_user_registry_progress(user_id)
    raid_damage = get_raid_damage(user_id)
    
    # Check quest completion
    quest_data = get_daemon_quest(user_id)
    all_quests_done = all(
        quest_data.get(qid, {}).get("claimed", False)
        for qid in QUEST_DEFS
    )
    
    newly_awarded = []
    
    for badge_id, badge_def in BADGE_DEFS.items():
        # Check if already awarded
        if badge_id in get_system_breach_badges(user_id):
            continue
        
        awarded = False
        
        if badge_def["requirement"] == "catch_count":
            if catch_count >= badge_def["threshold"]:
                awarded = True
        
        elif badge_def["requirement"] == "all_quests_done":
            if all_quests_done:
                awarded = True
        
        elif badge_def["requirement"] == "registry_complete":
            if caught >= total and total > 0:
                awarded = True
        
        elif badge_def["requirement"] == "raid_damage":
            if raid_damage >= badge_def["threshold"]:
                awarded = True
        
        # #6: Raid Participant Badge
        elif badge_def["requirement"] == "raid_participant":
            if get_raid_damage(user_id) >= 1:
                awarded = True
        
        if awarded:
            if grant_system_breach_badge(user_id, badge_id):
                newly_awarded.append(badge_def)
    
    return newly_awarded


# ══════════════════════════════════════════════════════════════════════════════
# EVENT COMMANDS HELP (PART 12) - NO OWNER COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

EVENT_COMMANDS = {
    "🔄 Core Commands": {
        "!event / !breach": "Open the System Breach hub",
        "!quests": "Show all available quests and progress",
        "!claimquest <id>": "Claim a completed quest for rewards",
        "!breachstats": "Show overall event statistics",
    },
    "🎯 Daemon Commands": {
        "!catch": "Attempt to catch a spawned Daemon",
        "!equipdaemon [id]": "Equip a Daemon (or unequip with no ID)",
        "!combinedaemon [id1] [id2] [id3]": "Combine up to 3 Daemons (50% Power)",
        "!fusedaemon <id1> <id2> [id3]": "Fuse duplicates into a higher rarity",
        "!scrapdaemon <id>": "Destroy a Daemon for guaranteed JC",
        "!tradedaemon @user <my_id> <their_id>": "Propose a 1:1 trade",
        "!selldaemon <id> <price>": "List a Daemon for sale",
        "!mycombine": "View your currently combined Daemons",
        "!fusionstreak": "View your current fusion streak",
        "!daemon": "View your daemon collection",
    },
    "⚔️ Boss Commands": {
        "!bosshit": "Attack the mid-boss (costs 10 JC)",
        "!bossstatus": "Check mid-boss status",
        "!raidhit": "Attack the final raid boss (costs 25 JC)",
        "!raidstatus": "Check final raid boss HP and your contribution",
    },
    "🏅 Badge Commands": {
        "!mybadges [@user]": "Show your System Breach badges",
        "!breachboard": "View leaderboard (top catchers + raid damage)",
    },
}


def _build_help_embed() -> discord.Embed:
    """Build the event commands help embed."""
    embed = discord.Embed(
        title="📖 System Breach — Command Help",
        description=(
            "All commands for the System Breach event.\n\n"
            "🌀 **Event Type:** Limited-time crisis response.\n"
            "Daemons are temporary — they will disappear when the event ends.\n"
            "🏅 **But your badges, titles, and JC are permanent!**\n\n"
            "💰 **End-of-Event Payout:**\n"
            f"Common: **{PAYOUT_RATES['common']} JC** • Rare: **{PAYOUT_RATES['rare']} JC**\n"
            f"Legendary: **{PAYOUT_RATES['legendary']} JC** • Mythic: **{PAYOUT_RATES['mythic']} JC**"
        ),
        color=discord.Color.blurple(),
    )
    
    for category, commands_dict in EVENT_COMMANDS.items():
        lines = []
        for cmd, desc in commands_dict.items():
            lines.append(f"`{cmd}` — {desc}")
        embed.add_field(
            name=category,
            value="\n".join(lines),
            inline=False,
        )
    
    embed.set_footer(text="Use !event to open the hub • Commands work in any channel")
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# VIEWS FOR BUTTONS
# ══════════════════════════════════════════════════════════════════════════════

class FusionConfirmView(discord.ui.View):
    """Fusion confirmation view with Fuse/Cancel buttons."""
    
    def __init__(self):
        super().__init__(timeout=30)
        self.custom_id = None
    
    @discord.ui.button(label="Fuse", style=discord.ButtonStyle.danger, custom_id="fuse_confirm")
    async def fuse_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.custom_id = "fuse_confirm"
        await interaction.response.defer()
        self.stop()
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="fuse_cancel")
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.custom_id = "fuse_cancel"
        await interaction.response.defer()
        self.stop()


class TradeView(discord.ui.View):
    """Trade accept/decline view."""
    
    def __init__(self, trade_id: str):
        super().__init__(timeout=300)
        self.trade_id = trade_id
        self.custom_id = None
    
    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="trade_accept")
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.custom_id = "trade_accept"
        await interaction.response.defer()
        self.stop()
    
    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, custom_id="trade_decline")
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.custom_id = "trade_decline"
        await interaction.response.defer()
        self.stop()


class SaleView(discord.ui.View):
    """Sale buy/cancel view."""
    
    def __init__(self, sale_id: str, seller_id: int):
        super().__init__(timeout=300)
        self.sale_id = sale_id
        self.seller_id = seller_id
        self.custom_id = None
    
    @discord.ui.button(label="Buy", style=discord.ButtonStyle.success, custom_id="buy_daemon")
    async def buy_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.custom_id = "buy_daemon"
        await interaction.response.defer()
        self.stop()
    
    @discord.ui.button(label="Cancel (seller only)", style=discord.ButtonStyle.danger, custom_id="cancel_sale")
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.custom_id = "cancel_sale"
        await interaction.response.defer()
        self.stop()


# ══════════════════════════════════════════════════════════════════════════════
# PART 8: BADGE PAGINATION VIEW
# ══════════════════════════════════════════════════════════════════════════════

class BadgesView(discord.ui.View):
    """Paginated view for showing System Breach badges."""
    
    def __init__(self, user_id: int, badges: list[str], page: int = 0, per_page: int = 10):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.badges = badges
        self.page = page
        self.per_page = per_page
        self.total_pages = max(1, (len(badges) + per_page - 1) // per_page)
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    def current_embed(self) -> discord.Embed:
        """Build the current page embed."""
        start = self.page * self.per_page
        end = start + self.per_page
        chunk = self.badges[start:end]
        
        if not chunk:
            lines = ["No badges yet."]
        else:
            lines = []
            for badge_id in chunk:
                badge_def = BADGE_DEFS.get(badge_id)
                if badge_def:
                    lines.append(f"{badge_def['emoji']} **{badge_def['name']}** — {badge_def['description']}")
                else:
                    lines.append(f"❓ Unknown badge: {badge_id}")
        
        embed = discord.Embed(
            title="🏅 System Breach Badges",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"Page {self.page + 1}/{self.total_pages} • {len(self.badges)} total")
        return embed

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("⚠️ This menu belongs to someone else!", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ══════════════════════════════════════════════════════════════════════════════
# PART 6: QUEST VIEWS (UPDATED)
# ══════════════════════════════════════════════════════════════════════════════

class QuestMenuView(discord.ui.View):
    """Two-button menu: Power Quests or Combat Quests (changes at 100% power)."""
    
    def __init__(self, user_id: int, cog: "SystemBreach"):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.cog = cog
        self._update_buttons()
    
    def _update_buttons(self):
        """Update button labels and styles based on power level."""
        power = get_jarvis_power()
        
        if power >= 100:
            # ── RAID PHASE: Change button labels ──
            self.power_quests_btn.label = "🎯 Individual Raid Quests"
            self.power_quests_btn.style = discord.ButtonStyle.success
            self.combat_quests_btn.label = "🌍 Global Raid Quests"
            self.combat_quests_btn.style = discord.ButtonStyle.primary
        else:
            # ── Normal phase ──
            self.power_quests_btn.label = "⚡ Power Quests"
            self.power_quests_btn.style = discord.ButtonStyle.primary
            self.combat_quests_btn.label = "⚔️ Combat Quests"
            self.combat_quests_btn.style = discord.ButtonStyle.danger
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "⚠️ This menu belongs to someone else. Run `!quests` yourself.",
                ephemeral=True
            )
            return False
        return True
    
    @discord.ui.button(label="⚡ Power Quests", style=discord.ButtonStyle.primary, emoji="⚡")
    async def power_quests_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show Power Quests or Individual Raid Quests."""
        power = get_jarvis_power()
        
        if power >= 100:
            # ── RAID PHASE: Show Individual Raid Quests ──
            embed = self.cog._build_individual_raid_quests_embed(self.user_id)
        else:
            # ── Normal: Show Power Quests ──
            embed = self.cog._build_power_quests_embed(self.user_id)
        
        await interaction.response.edit_message(embed=embed, view=self)
    
    @discord.ui.button(label="⚔️ Combat Quests", style=discord.ButtonStyle.danger, emoji="⚔️")
    async def combat_quests_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show Combat Quests or Global Raid Quests."""
        power = get_jarvis_power()
        
        if power < 25:
            embed = discord.Embed(
                title="⚔️ Combat Quests — LOCKED",
                description=(
                    "🌀 **Jarvis's core is too unstable to engage in direct combat.**\n\n"
                    "The Daemon incursion has crippled his combat subroutines. Without "
                    "enough stabilization, he can't risk deploying counter-measures against "
                    "the rogue processes.\n\n"
                    f"**Current Stabilization:** `{round(power)}%`\n"
                    f"**Required:** `25%`\n\n"
                    "📋 **Focus on Power Quests first.** Every completed quest strengthens "
                    "Jarvis's core and brings him closer to combat readiness.\n\n"
                    "> _\"I can barely hold myself together — let alone fight back. Give me time.\"_\n"
                    "> — Jarvis, System Breach log 0x7A3F"
                ),
                color=discord.Color.red(),
            )
            embed.set_footer(text="Complete Power Quests to unlock Combat Quests")
            await interaction.response.edit_message(embed=embed, view=self)
            return
        
        if power >= 100:
            # ── RAID PHASE: Show Global Raid Quests ──
            embed = self.cog._build_global_raid_quests_embed(self.user_id)
        else:
            # ── Normal: Show Combat Quests ──
            embed = self.cog._build_combat_quests_embed(self.user_id)
        
        await interaction.response.edit_message(embed=embed, view=self)
    
    @discord.ui.button(label="🏠 Home", style=discord.ButtonStyle.secondary, emoji="🏠")
    async def home_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go back to the event hub homepage."""
        embed = self.cog._build_hub_embed(self.user_id)
        view = EventHubView(self.user_id, self.cog)
        await interaction.response.edit_message(embed=embed, view=view)


# ══════════════════════════════════════════════════════════════════════════════
# PART 9: EVENT HUB VIEWS
# ══════════════════════════════════════════════════════════════════════════════

class EventHubView(discord.ui.View):
    """The main !event hub with all navigation buttons."""
    
    def __init__(self, user_id: int, cog: "SystemBreach"):
        super().__init__(timeout=None)  # Persistent
        self.user_id = user_id
        self.cog = cog
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "⚠️ This hub belongs to someone else. Run `!event` yourself.",
                ephemeral=True
            )
            return False
        return True
    
    @discord.ui.button(label="🗂️ Daemon Types", style=discord.ButtonStyle.secondary)
    async def daemon_types_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show registry progress and species list with perk info."""
        caught, total = _get_user_registry_progress(self.user_id)
        
        embed = discord.Embed(
            title="🗂️ Daemon Registry",
            description=f"You've discovered **{caught}/{total}** species.\n\n"
                        "Each Daemon has a unique **perk** that activates when equipped or combined.",
            color=discord.Color.blurple(),
        )
        
        # Group by rarity
        for rarity in RARITY_ORDER:
            species_list = []
            for sid, daemon in DAEMON_POOL.items():
                if daemon["rarity"] != rarity:
                    continue
                # Skip mythic for registry (raid-exclusive)
                if rarity == "mythic":
                    continue
                    
                # Check if caught by this user
                coll = get_daemon_collection(self.user_id)
                is_caught = any(
                    d.get("species") == sid
                    for d in coll.get("owned", {}).values()
                )
                
                # Mystery daemons show as "???" until caught
                if daemon.get("mystery") and not is_caught:
                    display = "❓ ??? — unidentified"
                else:
                    perk_name = daemon.get("perk", "None")
                    perk_display = _get_perk_description(perk_name)
                    if is_caught:
                        display = f"{daemon['emoji']} **{daemon['name']}** — _{perk_display}_"
                    else:
                        display = f"❌ {daemon['emoji']} **{daemon['name']}** — _{perk_display}_"
                
                if is_caught:
                    display = "✅ " + display
                
                species_list.append(display)
            
            if species_list:
                embed.add_field(
                    name=f"{rarity.title()} ({len(species_list)})",
                    value="\n".join(species_list),
                    inline=False,
                )
        
        embed.set_footer(text="✅ = Caught  ❌ = Not yet discovered  •  Equip a Daemon to use its perk!")
        view = EventHubView(self.user_id, self.cog)
        await interaction.response.edit_message(embed=embed, view=view)
    
    @discord.ui.button(label="📜 Quests", style=discord.ButtonStyle.primary)
    async def quests_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show quest menu."""
        embed = self.cog._build_quest_menu_embed(self.user_id)
        view = QuestMenuView(self.user_id, self.cog)
        await interaction.response.edit_message(embed=embed, view=view)
    
    @discord.ui.button(label="📦 My Daemons", style=discord.ButtonStyle.secondary)
    async def my_daemons_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show user's daemon collection with combine slots."""
        coll = get_daemon_collection(self.user_id)
        owned = coll.get("owned", {})
        equipped = coll.get("equipped")
        combine_slots = coll.get("combine_slots", [None, None, None])
        
        if not owned:
            embed = discord.Embed(
                title="📦 My Daemons",
                description="You haven't caught any Daemons yet. Chat in the server for a chance to spawn one!",
                color=discord.Color.greyple(),
            )
            view = EventHubView(self.user_id, self.cog)
            await interaction.response.edit_message(embed=embed, view=view)
            return
        
        embed = discord.Embed(
            title="📦 My Daemons",
            description=f"You own **{len(owned)}** Daemon(s).",
            color=discord.Color.blurple(),
        )
        
        lines = []
        for daemon_id, data in owned.items():
            species_id = data.get("species")
            daemon = DAEMON_POOL.get(species_id, {})
            emoji = daemon.get("emoji", "❓")
            name = daemon.get("name", "Unknown")
            rarity = daemon.get("rarity", "unknown")
            
            markers = []
            if daemon_id == equipped:
                markers.append("✅ equipped")
            if daemon_id in combine_slots:
                slot_idx = combine_slots.index(daemon_id) + 1
                markers.append(f"🔗 slot {slot_idx}")
            
            marker_str = f" ({', '.join(markers)})" if markers else ""
            lines.append(f"`{daemon_id}` {emoji} **{name}** ({rarity}){marker_str}")
        
        embed.description = "\n".join(lines[:25])
        if len(lines) > 25:
            embed.description += f"\n…and {len(lines) - 25} more."
        
        embed.set_footer(text="Use !equipdaemon <id> to equip, !scrapdaemon <id> to scrap, !mycombine to view slots")
        
        view = EventHubView(self.user_id, self.cog)
        await interaction.response.edit_message(embed=embed, view=view)
    
    @discord.ui.button(label="⚔️ Boss", style=discord.ButtonStyle.danger)
    async def boss_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show boss status."""
        embed = self.cog._build_boss_embed(self.user_id)
        view = EventHubView(self.user_id, self.cog)
        await interaction.response.edit_message(embed=embed, view=view)
    
    @discord.ui.button(label="📖 Help", style=discord.ButtonStyle.secondary)
    async def help_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show event commands help."""
        embed = _build_help_embed()
        view = EventHubView(self.user_id, self.cog)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.success)
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Refresh the hub."""
        embed = self.cog._build_hub_embed(self.user_id)
        view = EventHubView(self.user_id, self.cog)
        await interaction.response.edit_message(embed=embed, view=view)


class EventConfirmationView(discord.ui.View):
    """Confirmation view for dangerous actions."""
    
    def __init__(self, user_id: int, action: str):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.action = action
        self.confirmed = False
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "⚠️ This prompt isn't for you.",
                ephemeral=True
            )
            return False
        return True
    
    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await interaction.response.defer()
        self.stop()
    
    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        await interaction.response.defer()
        self.stop()


# ══════════════════════════════════════════════════════════════════════════════
# LEADERBOARD VIEW (for !breachboard) - FIXED
# ══════════════════════════════════════════════════════════════════════════════

class LeaderboardView(discord.ui.View):
    """Paginated view for leaderboard: Catchers / Raiders toggle."""
    
    def __init__(self, user_id: int, bot: commands.Bot):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.bot = bot
        self.page = "catchers" 
        self.message: discord.Message | None = None 
        self._update_buttons()
    
    def _update_buttons(self):
        self.catchers_btn.style = discord.ButtonStyle.primary if self.page == "catchers" else discord.ButtonStyle.secondary
        self.raiders_btn.style = discord.ButtonStyle.primary if self.page == "raiders" else discord.ButtonStyle.secondary
    
    async def _get_current_embed(self) -> discord.Embed:
        if self.page == "catchers":
            return await self._build_catchers_embed()
        else:
            return await self._build_raiders_embed()
    
    async def _build_catchers_embed(self) -> discord.Embed:
        all_users = {}
        if "daemon_quests" in _data:
            for uid_str, quest_data in _data.get("daemon_quests", {}).items():
                try:
                    uid = int(uid_str)
                    # Get total catches from total_catches quest
                    total = quest_data.get("total_catches", {}).get("progress", 0)
                    if total > 0:
                        all_users[uid] = total
                except (ValueError, TypeError):
                    pass

        top_catchers = sorted(all_users.items(), key=lambda x: x[1], reverse=True)[:10]
        
        embed = discord.Embed(
            title="🔎 Top Daemon Catchers",
            description="Most Daemons caught this event",
            color=0x3498DB,
        )
        
        if top_catchers:
            for rank, (user_id, count) in enumerate(top_catchers, 1):
                try:
                    user = await self.bot.fetch_user(user_id)
                    name = user.display_name
                except:
                    name = f"User {user_id}"
                
                medal = "🥇" if rank == 1 else ("🥈" if rank == 2 else ("🥉" if rank == 3 else f"{rank}."))
                embed.add_field(
                    name=f"{medal} {name}",
                    value=f"**{count}** Daemons caught",
                    inline=False
                )
        else:
            embed.description = "No catches yet."
        
        embed.set_footer(text="Use the buttons below to switch views")
        return embed
    
    async def _build_raiders_embed(self) -> discord.Embed:
        top_raiders = get_top_raid_contributors(10)
        
        embed = discord.Embed(
            title="⚔️ Top Raid Damage",
            description="Most damage dealt to the raid boss",
            color=0xE74C3C,
        )
        
        if top_raiders:
            for rank, (user_id, damage) in enumerate(top_raiders, 1):
                try:
                    user = await self.bot.fetch_user(user_id)
                    name = user.display_name
                except:
                    name = f"User {user_id}"
                
                medal = "🥇" if rank == 1 else ("🥈" if rank == 2 else ("🥉" if rank == 3 else f"{rank}."))
                embed.add_field(
                    name=f"{medal} {name}",
                    value=f"**{damage}** damage",
                    inline=False
                )
        else:
            embed.description = "No raid damage yet."
        
        embed.set_footer(text="Use the buttons below to switch views")
        return embed
    
    @discord.ui.button(label="🔎 Catchers", style=discord.ButtonStyle.primary)
    async def catchers_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = "catchers"
        self._update_buttons()
        embed = await self._get_current_embed()
        await interaction.response.edit_message(embed=embed, view=self)
    
    @discord.ui.button(label="⚔️ Raiders", style=discord.ButtonStyle.secondary)
    async def raiders_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = "raiders"
        self._update_buttons()
        embed = await self._get_current_embed()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("⚠️ This leaderboard belongs to someone else!", ephemeral=True)
            return False
        return True
    
    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ══════════════════════════════════════════════════════════════════════════════
# THE COG
# ══════════════════════════════════════════════════════════════════════════════

class SystemBreach(commands.Cog):
    """System Breach Event — Full Implementation (PARTS 1-12)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
        # ── Initialize event data if not exists ──
        if "event_data" not in _data:
            _data["event_data"] = {
                "active": False,
                "ends_at": 0,
                "start_time": 0,
                "dip_count": 0
            }
            from cogs.state import _schedule_save
            _schedule_save("event_data")
        
        # ── Initialize raid state if not exists ──
        if "raid_state" not in _data:
            _data["raid_state"] = {
                "hp": RAID_BOSS_HP,
                "active": False,
                "last_hit": {},
                "start_time": 0,
                "final_blow_user": None
            }
            from cogs.state import _schedule_save
            _schedule_save("raid_state")
        
        # ── Initialize mid-boss state if not exists ──
        if "mid_boss_state" not in _data:
            _data["mid_boss_state"] = {
                "hp": MID_BOSS_HP,
                "active": False,
                "defeated": False,
                "last_hit": {},
                "final_blow_user": None
            }
            from cogs.state import _schedule_save
            _schedule_save("mid_boss_state")
        
        # ── Initialize global raid progress if not exists ──
        if "raid_global_progress" not in _data:
            _data["raid_global_progress"] = {
                "total_damage": 0,
                "participants": [],
                "defeated": False,
            }
            from cogs.state import _schedule_save
            _schedule_save("raid_global_progress")
        
        # ── Initialize fusion streaks if not exists ──
        if "fusion_streaks" not in _data:
            _data["fusion_streaks"] = {}
            from cogs.state import _schedule_save
            _schedule_save("fusion_streaks")
        
        # ── Initialize stabilizer_used if not exists ──
        if "stabilizer_used" not in _data:
            _data["stabilizer_used"] = {}
            from cogs.state import _schedule_save
            _schedule_save("stabilizer_used")
        
        # ── Initialize spawn channels if not exists ──
        if "spawn_channels" not in _data:
            _data["spawn_channels"] = {}
            from cogs.state import _schedule_save
            _schedule_save("spawn_channels")
        
        self.raid_task = bot.loop.create_task(self._raid_auto_end_check())
        self.raid_regeneration_task = bot.loop.create_task(self._raid_regeneration_loop())

    async def cog_load(self):
        """Called when the cog is loaded - restore state if needed."""
        # ── Check if event was active before restart ──
        event = get_event_data()
        if event.get("active", False):
            print(f"[SystemBreach] Event was active before restart. Resuming...")
            
            # Check if event has expired
            ends_at = event.get("ends_at", 0)
            if ends_at > 0 and time.time() > ends_at:
                print("[SystemBreach] Event has expired. Ending...")
                set_event_active(False)
                return
            
            # ── Restore raid state ──
            state = get_raid_state()
            if state.get("active", False):
                hp = state.get("hp", RAID_BOSS_HP)
                if hp <= 0:
                    set_raid_active(False)
                else:
                    print(f"[SystemBreach] Raid active with {hp} HP remaining")
            
            # ── Restore mid-boss state ──
            mid_state = get_mid_boss_state()
            if mid_state.get("defeated", False) or mid_state.get("hp", MID_BOSS_HP) <= 0:
                set_mid_boss_defeated(True)
            elif mid_state.get("active", False):
                print(f"[SystemBreach] Mid-boss active with {mid_state.get('hp', MID_BOSS_HP)} HP remaining")
            
            print("[SystemBreach] Event resumed successfully!")
        else:
            print("[SystemBreach] No active event found on startup.")

    # ── Global Cog Check ──────────────────────────────────────────────────────

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Global check for all prefix commands in this cog.
        Owner commands (startbreach, endbreach, etc.) bypass this check."""
        if ctx.command and ctx.command.name in [
            "startbreach", "endbreach", "spawndaemon", "powertest", 
            "raidreset", "raidactivate", "midreset", "setpower",
            "spawnchannel"
        ]:
            return True
        
        event = get_event_data()
        if not event.get("active"):
            await ctx.reply("❌ The System Breach event is not active.")
            return False
        
        # ── Check if event has expired ──
        ends_at = event.get("ends_at", 0)
        if ends_at > 0 and time.time() > ends_at:
            set_event_active(False)
            await ctx.reply("❌ The System Breach event has ended.")
            return False
        
        return True

    # ── Quest Completion Check Helper ──────────────────────────────────────

    async def _check_quest_completion(self, message: discord.Message, user_id: int, quest_id: str):
        """Check if a quest was completed and send notification."""
        quest_data = get_daemon_quest(user_id)
        quest = QUEST_DEFS.get(quest_id)
        if not quest:
            return
        
        progress = quest_data.get(quest_id, {}).get("progress", 0)
        claimed = quest_data.get(quest_id, {}).get("claimed", False)
        notified = quest_data.get(quest_id, {}).get("notified", False)
        
        # ── For global raid quests, use global progress ──
        if quest.get("type") == "raid_global":
            if quest_id in ["raid_damage_2500", "raid_damage_5000", "raid_damage_7500", "raid_damage_10000"]:
                progress = get_raid_global_progress().get("total_damage", 0)
            elif quest_id == "raid_defeat":
                progress = 1 if get_raid_defeated() else 0
            elif quest_id in ["raid_participate_25", "raid_participate_50"]:
                progress = get_raid_participant_count()
        
        # ── For personal raid quests ──
        elif quest.get("type") == "raid_individual":
            # progress is already tracked per user via bump_daemon_quest
            pass
        
        if progress >= quest["goal"] and not claimed and not notified:
            user_key = str(user_id)
            if user_key not in _data["daemon_quests"]:
                _data["daemon_quests"][user_key] = {}
            if quest_id not in _data["daemon_quests"][user_key]:
                _data["daemon_quests"][user_key][quest_id] = {}
            _data["daemon_quests"][user_key][quest_id]["notified"] = True
            from cogs.state import _schedule_save
            _schedule_save("daemon_quests")
            
            await self._send_quest_completion_notification(message, user_id, quest_id)

    # ── Quest Embed Builders ─────────────────────────────────────────────────

    def _build_quest_menu_embed(self, user_id: int) -> discord.Embed:
        """Build the quest menu embed."""
        power = get_jarvis_power()
        
        embed = discord.Embed(
            title="📜 System Breach — Quests",
            description=(
                "Complete quests to earn **Jarvis Credits** and **Power**!\n\n"
                f"⚡ **Current Power:** `{round(power)}%`"
            ),
            color=discord.Color.blurple(),
        )
        
        user_quests = get_daemon_quest(user_id)
        
        if power >= 100:
            # ── RAID PHASE ──
            embed.description += "\n💥 **RAID PHASE ACTIVE!**"
            embed.color = 0xFF6B00
            
            # Individual Raid Quests progress
            individual_done = sum(1 for qid in INDIVIDUAL_RAID_QUESTS if user_quests.get(qid, {}).get("claimed", False))
            individual_total = len(INDIVIDUAL_RAID_QUESTS)
            
            # Global Raid Quests progress
            global_done = sum(1 for qid in RAID_QUESTS if user_quests.get(qid, {}).get("claimed", False))
            global_total = len(RAID_QUESTS)
            
            embed.add_field(
                name="📊 Your Raid Progress",
                value=f"🎯 **Individual Raid Quests:** {individual_done}/{individual_total} claimed\n"
                      f"🌍 **Global Raid Quests:** {global_done}/{global_total} claimed",
                inline=False,
            )
            
            # Raid status
            raid_active = get_raid_active()
            raid_defeated = get_raid_defeated()
            raid_hp = get_raid_hp()
            
            if raid_defeated or raid_hp <= 0:
                raid_status = "✅ Defeated!"
            elif raid_active and raid_hp > 0:
                raid_status = f"⚔️ Active! ({raid_hp:,} HP remaining)"
            else:
                raid_status = "🔄 Preparing..."
            
            embed.add_field(
                name="⚔️ Raid Status",
                value=raid_status,
                inline=False,
            )
            
        else:
            # ── Normal phase ──
            power_done = sum(1 for qid in POWER_QUESTS if user_quests.get(qid, {}).get("claimed", False))
            combat_done = sum(1 for qid in COMBAT_QUESTS if user_quests.get(qid, {}).get("claimed", False))
            
            embed.add_field(
                name="📊 Your Progress",
                value=f"⚡ **Power Quests:** {power_done}/{len(POWER_QUESTS)} claimed\n"
                      f"⚔️ **Combat Quests:** {combat_done}/{len(COMBAT_QUESTS)} claimed",
                inline=False,
            )
            
            # Show unlock progress
            if power < 25:
                embed.add_field(
                    name="🔒 Combat Quests Locked",
                    value=f"Reach **25% Power** to unlock Combat Quests!\nCurrent: {round(power)}%",
                    inline=False,
                )
            elif power < 100:
                embed.add_field(
                    name="💥 RAID Phase",
                    value=f"Reach **100% Power** to unlock the RAID PHASE!\nCurrent: {round(power)}%",
                    inline=False,
                )
        
        embed.set_footer(text="Choose a category below to see detailed quests")
        return embed

    def _build_power_quests_embed(self, user_id: int) -> discord.Embed:
        """Build the Power Quests embed."""
        _check_invite_quest(user_id)
        user_quests = get_daemon_quest(user_id)
        
        embed = discord.Embed(
            title="⚡ Power Quests (Always Available)",
            description="Complete these to stabilize Jarvis and unlock Combat Quests!",
            color=0xF39C12,
        )
        
        for quest_id in POWER_QUESTS:
            quest = QUEST_DEFS[quest_id]
            progress = user_quests.get(quest_id, {}).get("progress", 0)
            claimed = user_quests.get(quest_id, {}).get("claimed", False)
            
            status = "✅" if claimed else f"{progress}/{quest['goal']}"
            embed.add_field(
                name=f"{status} {quest['name']}",
                value=f"{quest['description']}\n💰 {quest['reward_jc']} JC + {quest['reward_power']}% Power",
                inline=False,
            )
        
        embed.set_footer(text="Use !claimquest <quest_id> to claim completed quests")
        return embed

    def _build_combat_quests_embed(self, user_id: int) -> discord.Embed:
        """Build the Combat Quests embed with progressive chain."""
        user_quests = get_daemon_quest(user_id)
        total_catches = user_quests.get("total_catches", {}).get("progress", 0)
        
        embed = discord.Embed(
            title="⚔️ Combat Quests",
            description="Jarvis is stable enough for combat operations! Push back the Daemon incursion!",
            color=0xE74C3C,
        )
        
        # ── Define the chain progression ──
        chain_order = [
            {"id": "catch_chain_1", "goal": 1, "display": "Catch 1 Daemon (Chain 1/3)"},
            {"id": "catch_chain_2", "goal": 5, "display": "Catch 5 Daemons (Chain 2/3)"},
            {"id": "catch_chain_3", "goal": 10, "display": "Catch 10 Daemons (Chain 3/3)"},
            {"id": "catch_chain_4", "goal": 25, "display": "Catch 25 Daemons"},
            {"id": "catch_chain_5", "goal": 50, "display": "Catch 50 Daemons"},
            {"id": "catch_chain_6", "goal": 100, "display": "Catch 100 Daemons"},
        ]
        
        # ── Find which chain quest is active ──
        active_chain = None
        for chain in chain_order:
            if user_quests.get(chain["id"], {}).get("claimed", False):
                continue
            if total_catches >= chain["goal"]:
                active_chain = chain["id"]
                break
        
        if active_chain is None:
            has_any = any(user_quests.get(c["id"], {}).get("claimed", False) for c in chain_order)
            if has_any:
                active_chain = "catch_chain_6"
            else:
                active_chain = "catch_chain_1"
        
        # ── Build quest list ──
        for quest_id in COMBAT_QUESTS:
            quest = QUEST_DEFS[quest_id]
            is_chain = quest_id in [c["id"] for c in chain_order]
            if is_chain and quest_id != active_chain:
                continue
            
            progress = user_quests.get(quest_id, {}).get("progress", 0)
            claimed = user_quests.get(quest_id, {}).get("claimed", False)
            
            if quest_id == active_chain:
                chain_info = next((c for c in chain_order if c["id"] == quest_id), None)
                if chain_info:
                    progress_display = min(total_catches, chain_info["goal"])
                    status = "✅" if claimed else f"{progress_display}/{chain_info['goal']}"
                else:
                    status = "✅" if claimed else f"{progress}/{quest['goal']}"
            else:
                status = "✅" if claimed else f"{progress}/{quest['goal']}"
            
            extra_req = quest.get("unlock_power", 25)
            if extra_req > 25 and get_jarvis_power() < extra_req:
                status = f"🔒 (needs {extra_req}% Power)"
            
            embed.add_field(
                name=f"{status} {quest['name']}",
                value=f"{quest['description']}\n💰 {quest['reward_jc']} JC + {quest['reward_power']}% Power",
                inline=False,
            )
        
        embed.set_footer(text="Use !claimquest <quest_id> to claim completed quests")
        return embed

    def _build_individual_raid_quests_embed(self, user_id: int) -> discord.Embed:
        """Build the Individual Raid Quests embed."""
        user_quests = get_daemon_quest(user_id)
        power = get_jarvis_power()
        
        embed = discord.Embed(
            title="🎯 Individual Raid Quests",
            description=(
                "💥 **RAID PHASE ACTIVE!**\n"
                "Complete these personal raid quests to earn rewards!\n\n"
                f"⚡ **Current Power:** `{round(power)}%`"
            ),
            color=0x00FF88,
        )
        
        # ── Show Individual Raid Quests ──
        for quest_id in INDIVIDUAL_RAID_QUESTS:
            quest = QUEST_DEFS.get(quest_id)
            if not quest:
                continue
            progress = user_quests.get(quest_id, {}).get("progress", 0)
            claimed = user_quests.get(quest_id, {}).get("claimed", False)
            
            # Check if personal quest is completed
            if progress >= quest["goal"] and not claimed:
                status = "✅ READY TO CLAIM!"
            elif claimed:
                status = "✅ Claimed"
            else:
                status = f"{progress}/{quest['goal']}"
            
            embed.add_field(
                name=f"{status} {quest['name']}",
                value=f"{quest['description']}\n💰 {quest['reward_jc']} JC + {quest['reward_power']}% Power",
                inline=False,
            )
        
        embed.set_footer(text="Use !claimquest <quest_id> to claim completed quests")
        return embed

    def _build_global_raid_quests_embed(self, user_id: int) -> discord.Embed:
        """Build the Global Raid Quests embed."""
        user_quests = get_daemon_quest(user_id)
        power = get_jarvis_power()
        
        # ── Get global progress ──
        global_progress = get_raid_global_progress()
        global_damage = global_progress.get("total_damage", 0)
        participants = get_raid_participant_count()
        raid_defeated = get_raid_defeated()
        raid_active = get_raid_active()
        raid_hp = get_raid_hp()
        
        # Status message based on raid state
        if raid_defeated or raid_hp <= 0:
            status_msg = "✅ **Raid Defeated!** The Architect has been destroyed! 🎉"
        elif raid_active and raid_hp > 0:
            hp_percent = (raid_hp / RAID_BOSS_HP) * 100
            status_msg = f"⚔️ **Raid Active!** HP: {raid_hp:,}/{RAID_BOSS_HP:,} ({round(hp_percent)}%)"
        else:
            status_msg = "🔄 **Raid Preparing...** The Architect will appear shortly!"
        
        embed = discord.Embed(
            title="🌍 Global Raid Quests",
            description=(
                "💥 **RAID PHASE ACTIVE!**\n"
                "Complete these global raid quests as a community!\n\n"
                f"⚡ **Current Power:** `{round(power)}%`\n"
                f"{status_msg}\n"
                f"Total Damage: **{global_damage:,}**\n"
                f"Participants: **{participants}**"
            ),
            color=0xFF6B00,
        )
        
        # ── Show Global Raid Quests ──
        for quest_id in RAID_QUESTS:
            quest = QUEST_DEFS.get(quest_id)
            if not quest:
                continue
            claimed = user_quests.get(quest_id, {}).get("claimed", False)
            
            # Get global progress for this quest
            if quest_id in ["raid_damage_2500", "raid_damage_5000", "raid_damage_7500", "raid_damage_10000"]:
                progress = global_damage
            elif quest_id == "raid_defeat":
                progress = 1 if raid_defeated else 0
            elif quest_id in ["raid_participate_25", "raid_participate_50"]:
                progress = participants
            else:
                progress = 0
            
            if progress >= quest["goal"] and not claimed:
                status = "✅ READY TO CLAIM!"
            elif claimed:
                status = "✅ Claimed"
            else:
                status = f"{progress}/{quest['goal']}"
            
            embed.add_field(
                name=f"{status} {quest['name']}",
                value=f"{quest['description']}\n💰 {quest['reward_jc']} JC + {quest['reward_power']}% Power",
                inline=False,
            )
        
        embed.set_footer(text="Use !claimquest <quest_id> to claim completed quests")
        return embed

    # ── Spawning ──────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        content = message.content.strip().lower()
        user_id = message.author.id
        guild_id = message.guild.id

        # ── #2: !catch with no spawn ──
        if content == "!catch":
            if message.channel.id not in _pending_spawns:
                await message.reply("❌ No Daemon to catch here! Keep chatting to spawn one. 👀")
                return

        # ── PERK CHECKS (Glitch, Saboteur, Wildcard) ──
        event_active = get_event_data().get("active", False)
        equipped = get_equipped_daemon(user_id)
        
        if equipped and event_active:
            perk = DAEMON_POOL.get(equipped["species"], {}).get("perk")
            
            # #2: Glitch's Echo Chance
            if perk == "echo_chance":
                _trigger_glitch(user_id, message.channel)
            
            # #6: Saboteur Drain Effect
            elif perk == "saboteur_drain":
                _trigger_saboteur(user_id, message.channel)
            
            # #7: Wildcard Gamble Effect
            elif perk == "wildcard_gamble":
                _trigger_wildcard(user_id, message.channel)

        if content == "!catch" and message.channel.id in _pending_spawns:
            await self._attempt_catch(message)
            return

        event = get_event_data()
        if not event.get("active"):
            return

        # ── Track chat_ai quest (ONLY when replying to or mentioning Jarvis) ──
        is_reply_to_bot = False
        if message.reference and message.reference.message_id:
            try:
                referenced_msg = await message.channel.fetch_message(message.reference.message_id)
                if referenced_msg and referenced_msg.author.id == self.bot.user.id:
                    is_reply_to_bot = True
            except:
                pass
        
        mentions_bot = self.bot.user in message.mentions
        
        if is_reply_to_bot or mentions_bot:
            bump_daemon_quest(message.author.id, "chat_ai", 1)
            await self._check_quest_completion(message, message.author.id, "chat_ai")

        # Track "system breach" phrase for quest
        if "system breach" in content:
            bump_daemon_quest(message.author.id, "say_breach", 1)
            await self._check_quest_completion(message, message.author.id, "say_breach")

        # ── CHECK IF CHANNEL IS WHITELISTED FOR SPAWNS ──
        if not is_spawn_channel(guild_id, message.channel.id):
            return

        now = time.time()
        last = _last_spawn_attempt.get(message.channel.id, 0)
        
        if now - last < SPAWN_CHANNEL_COOLDOWN:
            return
        _last_spawn_attempt[message.channel.id] = now

        if message.channel.id in _pending_spawns:
            return

        if random.random() < SPAWN_CHANCE_PER_MESSAGE:
            species_id = _pick_species()
            
            # ── #9: Spawn Hot Streak ──
            _last_time = _last_spawn_time.get(message.channel.id, 0)
            if now - _last_time < 60:  # Within 60 seconds
                streak = _spawn_streak.get(message.channel.id, 0) + 1
                _spawn_streak[message.channel.id] = streak
                
                if streak >= 3:
                    await message.channel.send(f"🔥 **Spawn Hot Streak!** {streak} spawns in 1 minute!")
                    # Bonus: next spawn has higher rarity chance
                    if streak >= 5:
                        # Force rare or legendary
                        species_id = random.choices(
                            ["cipher_rare", "mirror_rare", "wraith_rare", "reaper_rare", 
                             "sentinel_legendary", "corebreaker_legendary", "possessor_legendary"],
                            weights=[15, 15, 15, 15, 20, 10, 10],
                            k=1
                        )[0]
                        await message.channel.send(f"🌟 **Hot Streak Bonus!** A rare spawn appears!")
            else:
                _spawn_streak[message.channel.id] = 0
            
            _last_spawn_time[message.channel.id] = now
            _pending_spawns[message.channel.id] = {"species": species_id, "spawned_at": now}
            await message.channel.send(embed=_species_embed(species_id, spawned=True))
            asyncio.create_task(self._despawn_after(message.channel.id, species_id))

    async def _despawn_after(self, channel_id: int, species_id: str):
        """Despawn after timeout."""
        await asyncio.sleep(SPAWN_LIFETIME)
        pending = _pending_spawns.get(channel_id)
        if pending and pending["species"] == species_id:
            del _pending_spawns[channel_id]

    async def _attempt_catch(self, message: discord.Message):
        """Execute catch attempt WITH PERKS (reduced chances)."""
        pending = _pending_spawns.pop(message.channel.id, None)
        if pending is None:
            return

        species_id = pending["species"]
        species = DAEMON_POOL[species_id]
        user_id = message.author.id
        power = get_jarvis_power()
        
        # ── #1: IMP PERK: Catch Priority (reduced from 15% to 10%) ──
        equipped = get_equipped_daemon(user_id)
        if equipped and DAEMON_POOL.get(equipped["species"], {}).get("perk") == "catch_priority":
            if random.random() < 0.10:
                # Imp gives priority - process the catch immediately
                engagement_days = get_engagement_days_count(user_id)
                engagement_bonus = min(0.14, engagement_days * 0.02)
                
                cost = CATCH_COST
                cost_perk_active = False
                
                # Check perks normally (reduced chances)
                if equipped and DAEMON_POOL.get(equipped["species"], {}).get("perk") == "catch_discount":
                    if random.random() < 0.15:
                        cost = 0
                        cost_perk_active = True
                
                if cost > 0 and not spend_credits(user_id, cost):
                    await message.reply(f"❌ You need **{cost} 🪙** to catch this (Imp gave you priority!)")
                    _pending_spawns[message.channel.id] = pending
                    return
                
                rate = catch_success_rate(species["rarity"], power) + engagement_bonus
                rate = min(0.95, rate)
                
                if random.random() < rate:
                    new_id = add_daemon(user_id, species_id)
                    mark_engagement_action(user_id)
                    
                    # ── Track catches and check for quest completion ──
                    await self._track_catches_and_check_quests(message, user_id, species_id)
                    
                    await message.reply(f"👹 **Imp** gave you priority! You caught {species['emoji']} **{species['name']}** (ID: `{new_id}`)!")
                else:
                    await message.reply(f"💨 The **{species['name']}** slipped away! (Imp gave you priority but it escaped!)")
                return
        
        # ── Normal catch flow ──
        engagement_days = get_engagement_days_count(user_id)
        engagement_bonus = min(0.14, engagement_days * 0.02)
        
        cost = CATCH_COST
        cost_perk_active = False
        
        # Cipher: 15% discount (reduced from 25%)
        if equipped and DAEMON_POOL.get(equipped["species"], {}).get("perk") == "catch_discount":
            if random.random() < 0.15:
                cost = 0
                cost_perk_active = True
        
        # Wraith: 20% fee waive (reduced from 32%)
        if equipped and DAEMON_POOL.get(equipped["species"], {}).get("perk") == "fee_waive":
            if random.random() < 0.20:
                cost = 0
                cost_perk_active = True

        if cost > 0 and not spend_credits(user_id, cost):
            await message.reply(f"❌ You need **{cost} 🪙** to attempt this catch.")
            _pending_spawns[message.channel.id] = pending
            return

        rate = catch_success_rate(species["rarity"], power) + engagement_bonus
        rate = min(0.95, rate)
        
        success = random.random() < rate

        if not success:
            # ── #2: Near Miss Message ──
            diff = rate - random.random()
            near_miss = ""
            if 0 < diff < 0.10:
                diff_percent = round(diff * 100)
                near_miss = f" (you were only {diff_percent}% short!)"
            
            # Reaper: 18% chance to refund on fail (reduced from 30%)
            refunded = False
            if equipped and DAEMON_POOL.get(equipped["species"], {}).get("perk") == "catch_refund":
                if random.random() < 0.18 and cost > 0:
                    add_credits(user_id, cost)
                    refunded = True
            
            perk_text = ""
            if cost_perk_active:
                perk_text = " (Cipher/Wraith saved your JC!)"
            elif refunded:
                perk_text = " (Reaper refunded your JC!)"
            
            await message.reply(f"💨 The **{species['name']}** slipped away!{near_miss}{perk_text}")
            return

        new_id = add_daemon(user_id, species_id)
        mark_engagement_action(user_id)

        # ── Track catches and check for quest completion ──
        await self._track_catches_and_check_quests(message, user_id, species_id)
        
        # Mirror: 12% duplicate (reduced from 22%)
        bonus_daemon = None
        if equipped and DAEMON_POOL.get(equipped["species"], {}).get("perk") == "duplicate_catch":
            if random.random() < 0.12:
                bonus_id = add_daemon(user_id, species_id)
                bonus_daemon = bonus_id

        new_badges = _check_badges(user_id)
        for badge in new_badges:
            await message.channel.send(f"🏅 **{message.author.display_name}** earned the **{badge['emoji']} {badge['name']}** badge!")

        perk_text = ""
        if cost_perk_active:
            perk_text += " (free catch!)"
        if bonus_daemon:
            perk_text += " (Mirror duplicated it!)"

        if species["rarity"] == "common":
            await message.reply(f"✅ **{message.author.display_name}** caught {species['emoji']} **{species['name']}** (ID: `{new_id}`){perk_text}!")
        else:
            flair = "⚡ " if species["rarity"] == "rare" else ("👑 " if species["rarity"] == "legendary" else "🏆 ")
            await message.channel.send(f"{flair}**{message.author.display_name}** caught a {species['rarity'].title()}: **{species['name']}**! 🎉")

    # ── Quest Tracking & Notifications ──────────────────────────────────────

    async def _track_catches_and_check_quests(self, message: discord.Message, user_id: int, species_id: str):
        """Track catches and check if any quest was completed."""
        # ── Track total catches ──
        bump_daemon_quest(user_id, "total_catches", 1)
        
        # ── Check if mystery caught ──
        if species_id in ["saboteur_rare", "wildcard_rare"]:
            bump_daemon_quest(user_id, "catch_mystery", 1)
            await self._check_quest_completion(message, user_id, "catch_mystery")
        
        # ── Check chain quest completion ──
        chain_order = [
            {"id": "catch_chain_1", "goal": 1},
            {"id": "catch_chain_2", "goal": 5},
            {"id": "catch_chain_3", "goal": 10},
            {"id": "catch_chain_4", "goal": 25},
            {"id": "catch_chain_5", "goal": 50},
            {"id": "catch_chain_6", "goal": 100},
        ]
        
        quest_data = get_daemon_quest(user_id)
        total_catches = quest_data.get("total_catches", {}).get("progress", 0)
        
        for chain in chain_order:
            chain_id = chain["id"]
            chain_quest = quest_data.get(chain_id, {})
            
            if chain_quest.get("claimed", False):
                continue
            
            if total_catches >= chain["goal"]:
                await self._check_quest_completion(message, user_id, chain_id)
                break

    async def _send_quest_completion_notification(self, message: discord.Message, user_id: int, quest_id: str):
        """Send a quest completion notification."""
        quest = QUEST_DEFS.get(quest_id)
        if not quest:
            return
        
        # Get the user who completed the quest
        user = message.guild.get_member(user_id) if message.guild else None
        if not user:
            try:
                user = await self.bot.fetch_user(user_id)
            except:
                return
        
        quest_name = quest["name"]
        
        await message.channel.send(
            f"🎯 **{user.display_name}** completed **{quest_name}**! "
            f"Use `!claimquest {quest_id}` to claim your rewards! 💰"
        )

    async def _check_quest_completion(self, message: discord.Message, user_id: int, quest_id: str):
        """Check if a quest was completed and send notification."""
        quest_data = get_daemon_quest(user_id)
        quest = QUEST_DEFS.get(quest_id)
        if not quest:
            return
        
        progress = quest_data.get(quest_id, {}).get("progress", 0)
        claimed = quest_data.get(quest_id, {}).get("claimed", False)
        notified = quest_data.get(quest_id, {}).get("notified", False)
        
        # ── For global raid quests, use global progress ──
        if quest.get("type") == "raid_global":
            if quest_id in ["raid_damage_2500", "raid_damage_5000", "raid_damage_7500", "raid_damage_10000"]:
                progress = get_raid_global_progress().get("total_damage", 0)
            elif quest_id == "raid_defeat":
                progress = 1 if get_raid_defeated() else 0
            elif quest_id in ["raid_participate_25", "raid_participate_50"]:
                progress = get_raid_participant_count()
        
        # ── For personal raid quests ──
        elif quest.get("type") == "raid_individual":
            # progress is already tracked per user via bump_daemon_quest
            pass
        
        if progress >= quest["goal"] and not claimed and not notified:
            user_key = str(user_id)
            if user_key not in _data["daemon_quests"]:
                _data["daemon_quests"][user_key] = {}
            if quest_id not in _data["daemon_quests"][user_key]:
                _data["daemon_quests"][user_key][quest_id] = {}
            _data["daemon_quests"][user_key][quest_id]["notified"] = True
            from cogs.state import _schedule_save
            _schedule_save("daemon_quests")
            
            await self._send_quest_completion_notification(message, user_id, quest_id)

    # ── Equip ─────────────────────────────────────────────────────────────────

    @commands.command(name="equipdaemon")
    async def equip_daemon_cmd(self, ctx: commands.Context, daemon_id: str = None):
        """Equip a Daemon (or pass no ID to unequip)."""
        user_id = ctx.author.id

        if daemon_id is None:
            equip_daemon(user_id, None)
            set_stabilizer_used(user_id, False)
            await ctx.reply("✅ Unequipped all Daemons.")
            return

        coll = get_daemon_collection(user_id)
        owned = coll["owned"]

        if daemon_id not in owned:
            await ctx.reply(f"❌ You don't own daemon `{daemon_id}`.")
            return

        species_id = owned[daemon_id]["species"]
        daemon = DAEMON_POOL[species_id]

        if species_id == "stabilizer_legendary":
            set_stabilizer_used(user_id, False)

        key = (user_id, daemon_id)
        revealed_perk = None
        if daemon.get("mystery") and key not in _mystery_revealed:
            _mystery_revealed[key] = True
            perk = daemon.get("perk")
            if perk == "saboteur_drain":
                revealed_perk = "10% chance to randomly drain small JC"
            elif perk == "wildcard_gamble":
                revealed_perk = "30% chance of +10 JC, 70% chance of -5 JC"

        equip_daemon(user_id, daemon_id)
        
        perk_info = ""
        if revealed_perk:
            perk_info = f"\n🔍 **Identified:** {revealed_perk}"
        
        await ctx.reply(f"✅ Equipped {daemon['emoji']} **{daemon['name']}**.{perk_info}")

    # ── Combine ───────────────────────────────────────────────────────────────

    @commands.command(name="combinedaemon")
    async def combine_daemon_cmd(self, ctx: commands.Context, id1: str = None, id2: str = None, id3: str = None):
        """Combine up to 3 Daemons (unlocks at 50% Jarvis Power)."""
        power = get_jarvis_power()
        if power < 50:
            await ctx.reply(f"❌ Combine unlocks at 50% Jarvis Power (currently {round(power)}%).")
            return

        user_id = ctx.author.id
        coll = get_daemon_collection(user_id)
        owned = coll["owned"]
        current_slots = get_combine_slots(user_id)

        slot_ids = [id1, id2, id3]
        
        for daemon_id in slot_ids:
            if daemon_id is not None and daemon_id not in owned:
                await ctx.reply(f"❌ You don't own daemon `{daemon_id}`.")
                return
        
        # ── #4: Check if daemon is already in another slot ──
        for daemon_id in slot_ids:
            if daemon_id is not None:
                for slot in current_slots:
                    if slot == daemon_id:
                        await ctx.reply(f"❌ Daemon `{daemon_id}` is already in a combine slot!")
                        return

        active_slots = sum(1 for sid in slot_ids if sid is not None)
        total_cost = active_slots * COMBINE_COST_PER_SLOT

        if get_credits(user_id) < total_cost:
            await ctx.reply(f"❌ Combining costs **{total_cost} 🪙** ({active_slots} slots × {COMBINE_COST_PER_SLOT} JC).")
            return

        spend_credits(user_id, total_cost)
        set_combine_slots(user_id, id1, id2, id3)

        if id1 and id2 and id3:
            bump_daemon_quest(user_id, "combine_3", 1)
            await self._check_quest_completion(ctx.message, user_id, "combine_3")

        daemon_names = []
        for daemon_id in slot_ids:
            if daemon_id:
                species_id = owned[daemon_id]["species"]
                daemon_names.append(DAEMON_POOL[species_id]["emoji"] + " " + DAEMON_POOL[species_id]["name"])

        result = f"✅ Combined {' + '.join(daemon_names) if daemon_names else 'nothing'}.\n(-{total_cost} 🪙)"
        await ctx.reply(result)

    # ── View Combined Daemons ──────────────────────────────────────────────

    @commands.command(name="mycombine", aliases=["combined", "combineslots"])
    async def my_combine(self, ctx: commands.Context):
        """View your currently combined Daemons."""
        user_id = ctx.author.id
        slots = get_combine_slots(user_id)
        coll = get_daemon_collection(user_id)
        owned = coll.get("owned", {})
        
        embed = discord.Embed(
            title="🔗 Your Combined Daemons",
            color=discord.Color.blurple(),
        )
        
        slot_names = []
        for i, slot_id in enumerate(slots, 1):
            if slot_id is None:
                slot_names.append(f"**Slot {i}:** Empty")
            else:
                daemon_data = owned.get(slot_id)
                if daemon_data:
                    species_id = daemon_data.get("species")
                    daemon = DAEMON_POOL.get(species_id, {})
                    emoji = daemon.get("emoji", "❓")
                    name = daemon.get("name", "Unknown")
                    rarity = daemon.get("rarity", "unknown")
                    slot_names.append(f"**Slot {i}:** {emoji} {name} ({rarity}) - ID: `{slot_id}`")
                else:
                    slot_names.append(f"**Slot {i}:** ❌ Invalid (daemon missing)")
        
        embed.description = "\n".join(slot_names) if slot_names else "No combine slots set."
        embed.set_footer(text="Use !combinedaemon <id1> <id2> <id3> to set slots")
        await ctx.reply(embed=embed)

    # ── My Daemons Command ──────────────────────────────────────────────────

    @commands.command(name="mydaemons", aliases=["daemon", "daemons"])
    async def my_daemons(self, ctx: commands.Context):
        """Show all daemons you own."""
        user_id = ctx.author.id
        coll = get_daemon_collection(user_id)
        owned = coll.get("owned", {})
        equipped = coll.get("equipped")
        combine_slots = coll.get("combine_slots", [None, None, None])
        
        if not owned:
            embed = discord.Embed(
                title="📦 My Daemons",
                description="You haven't caught any Daemons yet. Chat in the server for a chance to spawn one!",
                color=discord.Color.greyple(),
            )
            await ctx.reply(embed=embed)
            return
        
        embed = discord.Embed(
            title="📦 My Daemons",
            description=f"You own **{len(owned)}** Daemon(s).",
            color=discord.Color.blurple(),
        )
        
        # Combined slots section
        slot_names = []
        for i, slot_id in enumerate(combine_slots, 1):
            if slot_id is None:
                slot_names.append(f"**Slot {i}:** Empty")
            else:
                daemon_data = owned.get(slot_id)
                if daemon_data:
                    species_id = daemon_data.get("species")
                    daemon = DAEMON_POOL.get(species_id, {})
                    emoji = daemon.get("emoji", "❓")
                    name = daemon.get("name", "Unknown")
                    rarity = daemon.get("rarity", "unknown")
                    slot_names.append(f"**Slot {i}:** {emoji} {name} ({rarity})")
                else:
                    slot_names.append(f"**Slot {i}:** ❌ Invalid (daemon missing)")
        
        embed.add_field(
            name="🔗 Combined Slots",
            value="\n".join(slot_names) if slot_names else "No combine slots set.",
            inline=False,
        )
        
        # All owned daemons section
        lines = []
        for daemon_id, data in owned.items():
            species_id = data.get("species")
            daemon = DAEMON_POOL.get(species_id, {})
            emoji = daemon.get("emoji", "❓")
            name = daemon.get("name", "Unknown")
            rarity = daemon.get("rarity", "unknown")
            
            markers = []
            if daemon_id == equipped:
                markers.append("✅ equipped")
            if daemon_id in combine_slots:
                slot_idx = combine_slots.index(daemon_id) + 1
                markers.append(f"🔗 slot {slot_idx}")
            
            marker_str = f" ({', '.join(markers)})" if markers else ""
            lines.append(f"`{daemon_id}` {emoji} **{name}** ({rarity}){marker_str}")
        
        embed.add_field(
            name="📋 Your Collection",
            value="\n".join(lines[:20]) if lines else "No daemons yet.",
            inline=False,
        )
        if len(lines) > 20:
            embed.description += f"\n*(Showing 20 of {len(lines)} daemons)*"
        
        embed.set_footer(text="Use !equipdaemon <id> to equip • !scrapdaemon <id> to scrap • !mycombine to view combined slots")
        await ctx.reply(embed=embed)

    # ── #1: Fusion Streak Command ────────────────────────────────────────────

    @commands.command(name="fusionstreak", aliases=["fstreak"])
    async def fusion_streak(self, ctx: commands.Context):
        """View your current fusion streak."""
        streak = get_fusion_streak(ctx.author.id)
        bonus = min(20, streak * 2)
        embed = discord.Embed(
            title="🔥 Fusion Streak",
            description=f"You have **{streak}** consecutive successful fusions!",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Bonus", value=f"+{bonus}% success rate")
        embed.set_footer(text="Streak resets on fusion failure")
        await ctx.reply(embed=embed)

    # ── Fusion ────────────────────────────────────────────────────────────────

    @commands.command(name="fusedaemon")
    async def fuse_daemon(self, ctx: commands.Context, id1: str, id2: str, id3: str = None):
        """Fuse duplicates into next-rarity daemon."""
        user_id = ctx.author.id
        coll = get_daemon_collection(user_id)
        owned = coll["owned"]

        equipped = get_equipped_daemon(user_id)
        possessor_active = False
        if equipped and DAEMON_POOL.get(equipped["species"], {}).get("perk") == "fusion_discount":
            possessor_active = True
            if id3 is None:
                id_list = [id1, id2]
            else:
                id_list = [id1, id2, id3]
        else:
            if id3 is None:
                await ctx.reply("❌ You need 3 duplicates to fuse (or equip Possessor for 2-dupe fusion).")
                return
            id_list = [id1, id2, id3]

        for daemon_id in id_list:
            if daemon_id not in owned:
                await ctx.reply(f"❌ You don't own daemon `{daemon_id}`.")
                return

        species = [owned[daemon_id]["species"] for daemon_id in id_list]

        if not (species[0] == species[1] == (species[2] if len(species) > 2 else species[0])):
            await ctx.reply(f"❌ All daemons must be the **same species**.")
            return

        rarity = DAEMON_POOL[species[0]]["rarity"]
        rarity_idx = RARITY_ORDER.index(rarity)
        if rarity_idx >= len(RARITY_ORDER) - 2:
            await ctx.reply(f"❌ **{DAEMON_POOL[species[0]]['name']}** is {rarity} — can't fuse any higher.")
            return

        power = get_jarvis_power()
        
        # ── #8: STABILIZER PERK: Guaranteed Fusion ──
        stabilizer_active = False
        if equipped and DAEMON_POOL.get(equipped["species"], {}).get("perk") == "stabilizer":
            if not get_stabilizer_used(user_id):
                stabilizer_active = True
                set_stabilizer_used(user_id, True)
        
        # ── #1: Fusion Streak Bonus ──
        streak = get_fusion_streak(user_id)
        streak_bonus = min(0.20, streak * 0.02)  # +2% per streak, max +20%
        
        if stabilizer_active:
            success_rate = 1.0
            power_display = "💠 **Stabilizer active!** Fusion guaranteed!"
        else:
            success_rate = min(1.0, fusion_success_rate(power) + streak_bonus)
            power_display = f"**{round(success_rate * 100)}%** at {round(power)}% Jarvis Power"
            if streak > 0:
                power_display += f"\n🔥 Fusion Streak: +{round(streak_bonus * 100)}%"

        embed = discord.Embed(
            title="⚙️ Fusion Confirmation",
            description=f"Fusing {len(id_list)} **{DAEMON_POOL[species[0]]['name']}** ({rarity.title()})\n→ 1 random **{RARITY_ORDER[rarity_idx + 1].title()}** daemon",
            color=0xFF9500,
        )
        embed.add_field(
            name="Success Rate",
            value=power_display,
            inline=False,
        )
        embed.add_field(
            name="⚠️ On Failure",
            value="All duplicates are **permanently lost** with no refund." if not stabilizer_active else "✅ Stabilizer guarantees success!",
            inline=False,
        )

        view = FusionConfirmView()
        msg = await ctx.reply(embed=embed, view=view)

        try:
            interaction = await self.bot.wait_for(
                "interaction",
                check=lambda i: i.user == ctx.author and i.message == msg,
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            await ctx.reply("❌ Fusion timed out.")
            return

        if view.custom_id == "fuse_cancel":
            await msg.edit(content="✅ Fusion cancelled.", embed=None, view=None)
            return

        # Fuse confirmed
        if stabilizer_active or random.random() < success_rate:
            for daemon_id in id_list:
                remove_daemon(user_id, daemon_id)

            next_rarity = RARITY_ORDER[rarity_idx + 1]
            candidates = [s for s, d in DAEMON_POOL.items() if d["rarity"] == next_rarity and d["rarity"] != "mythic"]
            new_species = random.choice(candidates)
            new_id = add_daemon(user_id, new_species)

            bump_daemon_quest(user_id, "fuse_once", 1)
            await self._check_quest_completion(ctx.message, user_id, "fuse_once")
            
            # ── #1: Update Fusion Streak ──
            new_streak = get_fusion_streak(user_id) + 1
            set_fusion_streak(user_id, new_streak)
            
            # Announce milestone
            if new_streak > 0 and new_streak % 5 == 0:
                await ctx.send(f"🔥 **Fusion Streak: {new_streak}!** +{min(20, new_streak * 2)}% fusion bonus!")

            result = f"✅ **Fusion successful!**\n{len(id_list)}x {DAEMON_POOL[species[0]]['name']} → {DAEMON_POOL[new_species]['emoji']} **{DAEMON_POOL[new_species]['name']}** (ID: `{new_id}`)"
            await msg.edit(content=result, embed=None, view=None)

        else:
            for daemon_id in id_list:
                remove_daemon(user_id, daemon_id)

            # ── #1: Reset Fusion Streak ──
            set_fusion_streak(user_id, 0)

            result = f"💨 **Fusion failed.** All **{DAEMON_POOL[species[0]]['name']}** were lost."
            await msg.edit(content=result, embed=None, view=None)

    @fuse_daemon.error
    async def fuse_daemon_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(
                "**Usage:** `!fusedaemon <id1> <id2> [id3]`\n"
                "**Example:** `!fusedaemon 1 2 3`\n"
                "**Note:** If you have Possessor equipped, you only need 2 IDs."
            )
        elif isinstance(error, commands.BadArgument):
            await ctx.reply(
                "**Usage:** `!fusedaemon <id1> <id2> [id3]`\n"
                "**Example:** `!fusedaemon 1 2 3`\n"
                "IDs should be numbers (e.g. `1`, `2`, `3`)."
            )

    # ── Scrap ─────────────────────────────────────────────────────────────────

    @commands.command(name="scrapdaemon")
    async def scrap_daemon(self, ctx: commands.Context, daemon_id: str):
        """Destroy a daemon for guaranteed JC."""
        user_id = ctx.author.id
        coll = get_daemon_collection(user_id)
        owned = coll["owned"]

        if daemon_id not in owned:
            await ctx.reply(f"❌ You don't own daemon `{daemon_id}`.")
            return

        equipped = get_equipped_daemon(user_id)
        if equipped and equipped.get("instance_id") == daemon_id:
            await ctx.reply("⚠️ **Warning:** This daemon is currently equipped! It will be unequipped if you scrap it.")

        species_id = owned[daemon_id]["species"]
        species_name = DAEMON_POOL[species_id]["name"]

        remove_daemon(user_id, daemon_id)
        add_credits(user_id, SCRAP_VALUE)

        await ctx.reply(f"♻️ Scrapped **{species_name}** for **{SCRAP_VALUE} 🪙**.")

    @scrap_daemon.error
    async def scrap_daemon_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(
                "**Usage:** `!scrapdaemon <id>`\n"
                "**Example:** `!scrapdaemon 1`\n"
                "Use `!daemons` to see your daemons and their IDs."
            )
        elif isinstance(error, commands.BadArgument):
            await ctx.reply(
                "**Usage:** `!scrapdaemon <id>`\n"
                "**Example:** `!scrapdaemon 1`\n"
                "IDs should be numbers (e.g. `1`, `2`, `3`)."
            )

    # ── Trading ───────────────────────────────────────────────────────────────

    @commands.command(name="tradedaemon")
    async def trade_daemon(self, ctx: commands.Context, *, args: str = None):
        """Propose a 1:1 Daemon trade."""
        if args is None:
            await ctx.reply(
                "**Usage:** `!tradedaemon @user <my_daemon_id> <their_daemon_id>`\n"
                "**Example:** `!tradedaemon @Phantom 1 2`"
            )
            return

        parts = args.split()
        if len(parts) < 3:
            await ctx.reply(
                "**Usage:** `!tradedaemon @user <my_daemon_id> <their_daemon_id>`\n"
                "**Example:** `!tradedaemon @Phantom 1 2`"
            )
            return

        user_input = parts[0]
        my_id = parts[1]
        their_id = parts[2]

        # ── #5: Self-trade same ID check ──
        if my_id == their_id:
            await ctx.reply("❌ You can't trade a daemon with itself!")
            return

        user = _resolve_user(ctx, user_input)
        if user is None:
            await ctx.reply(f"❌ Couldn't find user `{user_input}`. Please mention them (e.g. `@user`) or use their ID.")
            return

        if user.bot or user.id == ctx.author.id:
            await ctx.reply("❌ You can't trade with yourself or a bot.")
            return

        my_coll = get_daemon_collection(ctx.author.id)
        if my_id not in my_coll["owned"]:
            await ctx.reply(f"❌ You don't own daemon `{my_id}`.")
            return

        their_coll = get_daemon_collection(user.id)
        if their_id not in their_coll["owned"]:
            await ctx.reply(f"❌ **{user.display_name}** doesn't own daemon `{their_id}`.")
            return

        my_species = my_coll["owned"][my_id]["species"]
        their_species = their_coll["owned"][their_id]["species"]

        embed = discord.Embed(
            title="🔄 Daemon Trade Proposal",
            description=f"**{ctx.author.display_name}** offers you a trade:",
            color=0x9B59B6,
        )
        embed.add_field(
            name="You receive",
            value=f"{DAEMON_POOL[my_species]['emoji']} **{DAEMON_POOL[my_species]['name']}**",
            inline=True,
        )
        embed.add_field(
            name="They receive",
            value=f"{DAEMON_POOL[their_species]['emoji']} **{DAEMON_POOL[their_species]['name']}**",
            inline=True,
        )

        trade_id = f"trade_{ctx.author.id}_{user.id}_{int(time.time())}"
        view = TradeView(trade_id)
        msg = await ctx.send(
            f"<@{user.id}>",
            embed=embed,
            view=view,
        )

        _pending_trades[trade_id] = {
            "from_user": ctx.author.id,
            "to_user": user.id,
            "from_daemon": my_id,
            "to_daemon": their_id,
            "message_id": msg.id,
        }

        try:
            interaction = await self.bot.wait_for(
                "interaction",
                check=lambda i: i.user.id == user.id and i.message.id == msg.id,
                timeout=300.0,
            )
        except asyncio.TimeoutError:
            _pending_trades.pop(trade_id, None)
            timeout_embed = discord.Embed(
                title="⏰ Trade Expired",
                description=(
                    f"**{user.display_name}** didn't respond in time.\n"
                    f"The trade offer from **{ctx.author.display_name}** has expired."
                ),
                color=discord.Color.dark_gray(),
            )
            await msg.edit(content=None, embed=timeout_embed, view=None)
            return

        if view.custom_id == "trade_decline":
            _pending_trades.pop(trade_id, None)
            await msg.edit(content="❌ Trade declined.", embed=None, view=None)
            return

        my_coll_check = get_daemon_collection(ctx.author.id)
        their_coll_check = get_daemon_collection(user.id)

        if my_id not in my_coll_check["owned"] or their_id not in their_coll_check["owned"]:
            await msg.edit(content="❌ One or both daemons were traded away already.", embed=None, view=None)
            _pending_trades.pop(trade_id, None)
            return

        remove_daemon(ctx.author.id, my_id)
        remove_daemon(user.id, their_id)
        add_daemon(ctx.author.id, their_species)
        add_daemon(user.id, my_species)

        bump_daemon_quest(ctx.author.id, "trade_daemon", 1)
        await self._check_quest_completion(ctx.message, ctx.author.id, "trade_daemon")
        
        bump_daemon_quest(user.id, "trade_daemon", 1)
        await self._check_quest_completion(ctx.message, user.id, "trade_daemon")

        result = f"✅ **Trade executed!**\n**{ctx.author.display_name}** ← {DAEMON_POOL[their_species]['emoji']} {DAEMON_POOL[their_species]['name']}\n**{user.display_name}** ← {DAEMON_POOL[my_species]['emoji']} {DAEMON_POOL[my_species]['name']}"
        await msg.edit(content=result, embed=None, view=None)
        _pending_trades.pop(trade_id, None)

    @trade_daemon.error
    async def trade_daemon_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(
                "**Usage:** `!tradedaemon @user <my_daemon_id> <their_daemon_id>`\n"
                "**Example:** `!tradedaemon @Phantom 1 2`"
            )
        elif isinstance(error, commands.BadArgument):
            await ctx.reply(
                "**Usage:** `!tradedaemon @user <my_daemon_id> <their_daemon_id>`\n"
                "**Example:** `!tradedaemon @Phantom 1 2`\n"
                "IDs should be numbers (e.g. `1`, `2`, `3`)."
            )

    # ── Selling ───────────────────────────────────────────────────────────────

    @commands.command(name="selldaemon")
    async def sell_daemon(self, ctx: commands.Context, daemon_id: str = None, price: int = None):
        """List a Daemon for sale."""
        if daemon_id is None or price is None:
            await ctx.reply(
                "**Usage:** `!selldaemon <id> <price>`\n"
                "**Example:** `!selldaemon 1 100`\n"
                "Use `!daemons` to see your daemons and their IDs."
            )
            return

        if price < 1 or price > 999999:
            await ctx.reply("❌ Price must be between 1 and 999,999 JC.")
            return

        user_id = ctx.author.id
        coll = get_daemon_collection(user_id)
        owned = coll["owned"]

        if daemon_id not in owned:
            await ctx.reply(f"❌ You don't own daemon `{daemon_id}`.")
            return

        equipped = get_equipped_daemon(user_id)
        if equipped and equipped.get("instance_id") == daemon_id:
            await ctx.reply("⚠️ **Warning:** This daemon is currently equipped! It will be unequipped if you sell it.")

        species_id = owned[daemon_id]["species"]
        daemon = DAEMON_POOL[species_id]

        embed = discord.Embed(
            title="🛍️ Daemon For Sale",
            description=f"**Seller:** {ctx.author.display_name}",
            color=0x2ECC71,
        )
        embed.add_field(
            name="Item",
            value=f"{daemon['emoji']} **{daemon['name']}** ({daemon['rarity'].title()})",
            inline=False,
        )
        embed.add_field(
            name="Price",
            value=f"**{price} 🪙**",
            inline=False,
        )
        embed.set_footer(text="Click Buy to accept this offer (expires in 5 min)")

        sale_id = f"sale_{user_id}_{int(time.time())}"
        view = SaleView(sale_id, user_id)
        msg = await ctx.send(
            embed=embed,
            view=view,
        )

        _pending_sales[sale_id] = {
            "seller_id": user_id,
            "daemon_id": daemon_id,
            "price": price,
            "message_id": msg.id,
        }

        try:
            interaction = await self.bot.wait_for(
                "interaction",
                check=lambda i: i.message.id == msg.id,
                timeout=300.0,
            )
        except asyncio.TimeoutError:
            _pending_sales.pop(sale_id, None)
            timeout_embed = discord.Embed(
                title="⏰ Sale Expired",
                description=(
                    f"No one bought the **{daemon['name']}** in time.\n"
                    f"The sale has been cancelled."
                ),
                color=discord.Color.dark_gray(),
            )
            await msg.edit(embed=timeout_embed, view=None, content=None)
            return

        if view.custom_id == "cancel_sale":
            if interaction.user.id != user_id:
                await interaction.response.send_message("❌ Only the seller can cancel.", ephemeral=True)
                return
            _pending_sales.pop(sale_id, None)
            await msg.edit(embed=None, view=None, content="❌ Sale cancelled by seller.")
            return

        buyer_id = interaction.user.id
        buyer = interaction.user

        seller_coll = get_daemon_collection(user_id)
        if daemon_id not in seller_coll["owned"]:
            await msg.edit(embed=None, view=None, content="❌ Daemon was already traded away.")
            _pending_sales.pop(sale_id, None)
            return

        if get_credits(buyer_id) < price:
            await msg.edit(embed=None, view=None, content=f"❌ **{buyer.display_name}** doesn't have enough JC ({price} needed).")
            _pending_sales.pop(sale_id, None)
            return

        spend_credits(buyer_id, price)
        add_credits(user_id, price)
        remove_daemon(user_id, daemon_id)
        add_daemon(buyer_id, species_id)

        result = f"✅ **Sale complete!**\n**{buyer.display_name}** bought {daemon['emoji']} **{daemon['name']}** from **{ctx.author.display_name}** for **{price} 🪙**"
        await msg.edit(embed=None, view=None, content=result)
        _pending_sales.pop(sale_id, None)

    @sell_daemon.error
    async def sell_daemon_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(
                "**Usage:** `!selldaemon <id> <price>`\n"
                "**Example:** `!selldaemon 1 100`\n"
                "Use `!daemons` to see your daemons and their IDs."
            )
        elif isinstance(error, commands.BadArgument):
            await ctx.reply(
                "**Usage:** `!selldaemon <id> <price>`\n"
                "**Example:** `!selldaemon 1 100`\n"
                "IDs should be numbers (e.g. `1`, `2`, `3`)."
            )

    # ── Quests ─────────────────────────────────────────────────────────────────

    @commands.command(name="quests")
    async def show_quests(self, ctx: commands.Context):
        """Show available quests and progress."""
        embed = self._build_quest_menu_embed(ctx.author.id)
        view = QuestMenuView(ctx.author.id, self)
        await ctx.reply(embed=embed, view=view)

    @commands.command(name="claimquest")
    async def claim_quest(self, ctx: commands.Context, quest_id: str = None):
        """Claim a completed quest for rewards."""
        if quest_id is None:
            await ctx.reply(
                "**Usage:** `!claimquest <quest_id>`\n"
                "**Example:** `!claimquest chat_ai`\n"
                "Use `!quests` to see available quests and their IDs."
            )
            return

        if quest_id not in QUEST_DEFS:
            await ctx.reply(f"❌ Unknown quest: `{quest_id}`.\nAvailable quests: {', '.join(QUEST_DEFS.keys())}")
            return

        user_id = ctx.author.id
        power = get_jarvis_power()
        quest = QUEST_DEFS[quest_id]
        
        if quest.get("unlock_power") and power < quest["unlock_power"]:
            await ctx.reply(f"❌ This quest unlocks at {quest['unlock_power']}% Jarvis Power (currently {round(power)}%).")
            return

        user_quests = get_daemon_quest(user_id)
        quest_data = user_quests.get(quest_id, {})
        
        # ── Special handling for global raid quests ──
        if quest.get("type") == "raid_global":
            if quest_id in ["raid_damage_2500", "raid_damage_5000", "raid_damage_7500", "raid_damage_10000"]:
                actual_progress = get_raid_global_progress().get("total_damage", 0)
                if actual_progress < quest["goal"]:
                    await ctx.reply(f"❌ **{quest['name']}** global progress: {actual_progress}/{quest['goal']}. Not completed yet.")
                    return
            elif quest_id == "raid_defeat":
                if not get_raid_defeated():
                    await ctx.reply(f"❌ **{quest['name']}** - The raid has not been defeated yet!")
                    return
            elif quest_id in ["raid_participate_25", "raid_participate_50"]:
                if get_raid_participant_count() < quest["goal"]:
                    await ctx.reply(f"❌ **{quest['name']}** - Only {get_raid_participant_count()}/{quest['goal']} players have participated!")
                    return
        
        # ── For personal raid quests, check progress normally ──
        if quest_data.get("claimed"):
            await ctx.reply(f"❌ You already claimed **{quest['name']}** this cycle.")
            return

        progress = quest_data.get("progress", 0)
        
        # For global quests, use the actual progress we already calculated
        if quest.get("type") == "raid_global":
            if quest_id in ["raid_damage_2500", "raid_damage_5000", "raid_damage_7500", "raid_damage_10000"]:
                progress = get_raid_global_progress().get("total_damage", 0)
            elif quest_id == "raid_defeat":
                progress = 1 if get_raid_defeated() else 0
            elif quest_id in ["raid_participate_25", "raid_participate_50"]:
                progress = get_raid_participant_count()
        
        if progress < quest["goal"]:
            await ctx.reply(f"❌ **{quest['name']}** progress: {progress}/{quest['goal']}. Not completed yet.")
            return

        user_key = str(user_id)
        if user_key not in _data["daemon_quests"]:
            _data["daemon_quests"][user_key] = {}
        if quest_id not in _data["daemon_quests"][user_key]:
            _data["daemon_quests"][user_key][quest_id] = {}
        
        _data["daemon_quests"][user_key][quest_id]["claimed"] = True
        _data["daemon_quests"][user_key][quest_id]["notified"] = True

        add_credits(user_id, quest["reward_jc"])
        add_jarvis_power(quest["reward_power"])

        # ── #5: Quest Chain: Auto-start next quest ──
        next_quest = quest.get("next_quest")
        if next_quest:
            if user_key not in _data["daemon_quests"]:
                _data["daemon_quests"][user_key] = {}
            if next_quest not in _data["daemon_quests"][user_key]:
                _data["daemon_quests"][user_key][next_quest] = {"progress": 0, "claimed": False}
            await ctx.reply(f"📜 **Chain progress!** Next quest: **{QUEST_DEFS[next_quest]['name']}**")

        new_badges = _check_badges(user_id)
        for badge in new_badges:
            await ctx.channel.send(f"🏅 **{ctx.author.display_name}** earned the **{badge['emoji']} {badge['name']}** badge!")

        await ctx.reply(f"✅ Claimed **{quest['name']}**!\n💰 +{quest['reward_jc']} JC\n⚡ +{quest['reward_power']}% Jarvis Power")

    @claim_quest.error
    async def claim_quest_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(
                "**Usage:** `!claimquest <quest_id>`\n"
                "**Example:** `!claimquest chat_ai`\n"
                "Use `!quests` to see available quests and their IDs."
            )
        elif isinstance(error, commands.BadArgument):
            await ctx.reply(
                "**Usage:** `!claimquest <quest_id>`\n"
                "**Example:** `!claimquest chat_ai`"
            )

    # ── PART 8: Leaderboard ──────────────────────────────────────────────────

    @commands.command(name="breachboard")
    async def show_leaderboard(self, ctx: commands.Context):
        """Show System Breach leaderboard with Catchers/Raiders toggle."""
        view = LeaderboardView(ctx.author.id, self.bot)
        embed = await view._get_current_embed()
        msg = await ctx.reply(embed=embed, view=view)
        view.message = msg
    
    # ── PART 8: Badges Command ──────────────────────────────────────────────

    @commands.command(name="mybadges")
    async def show_my_badges(self, ctx: commands.Context, user: discord.User = None):
        """Show your System Breach badges with pagination."""
        target = user or ctx.author
        badges = get_system_breach_badges(target.id)
        
        if not badges:
            embed = discord.Embed(
                title=f"🏅 {target.display_name}'s System Breach Badges",
                description="No badges earned yet. Catch Daemons and complete quests to earn some!",
                color=discord.Color.greyple(),
            )
            await ctx.reply(embed=embed)
            return
        
        view = BadgesView(ctx.author.id, badges)
        await ctx.reply(embed=view.current_embed(), view=view)

    @show_my_badges.error
    async def show_my_badges_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.BadArgument):
            await ctx.reply(
                "**Usage:** `!mybadges [@user]`\n"
                "**Example:** `!mybadges` or `!mybadges @Phantom`"
            )

    # ── PART 9: Event Hub ────────────────────────────────────────────────────

    def _build_hub_embed(self, user_id: int) -> discord.Embed:
        """Build the main event hub embed."""
        event = get_event_data()
        power = get_jarvis_power()
        tier = get_power_tier(power)
        tier_emoji = get_power_tier_emoji(power)
        caught = _get_user_catch_count(user_id)
        badges = len(get_system_breach_badges(user_id))
        equipped = get_equipped_daemon(user_id)
        equipped_name = "None"
        if equipped:
            species_id = equipped.get("species")
            if species_id and species_id in DAEMON_POOL:
                equipped_name = f"{DAEMON_POOL[species_id]['emoji']} {DAEMON_POOL[species_id]['name']}"
        
        # ── Power bar (capped at 100% for display) ──
        display_power = min(100, power)
        bar_filled = int(display_power / 10)
        bar = "▰" * bar_filled + "▱" * (10 - bar_filled)
        
        # ── Power bonus info ──
        power_bonus_text = ""
        if power > 100:
            bonus_percent = round((power - 100) * 2)
            power_bonus_text = f"\n⚡ **{bonus_percent}%** damage bonus to boss/raid hits!"
        
        combat_unlocked = "✅" if power >= 25 else f"🔒 ({round(power)}/25%)"
        combine_unlocked = "✅" if power >= 50 else f"🔒 ({round(power)}/50%)"
        
        # Mid-boss status
        mid_active = get_mid_boss_active()
        mid_defeated = get_mid_boss_defeated()
        mid_hp = get_mid_boss_hp()
        
        if power >= 50 and not mid_defeated and mid_hp > 0:
            mid_status = "⚔️ Active!" if mid_active else "🔄 Preparing..."
        elif mid_defeated or mid_hp <= 0:
            mid_status = "✅ Defeated!"
        else:
            mid_status = f"🔒 ({round(power)}/50%)"
        
        raid_unlocked = "✅" if power >= 100 else f"🔒 ({round(power)}/100%)"
        
        # ── Raid phase info ──
        raid_phase_text = ""
        if power >= 100 and get_raid_active():
            raid_phase_text = "\n💥 **RAID PHASE ACTIVE!** Check `!quests` for raid quests!"
        
        embed = discord.Embed(
            title="🌀 System Breach",
            description=(
                "A mysterious corruption has spread through the Jarvis network. "
                "Rogue Daemons are everywhere — catch them, complete quests, "
                "and help stabilize Jarvis!\n\n"
                "⚠️ **This is a limited-time crisis event.**\n"
                "When the breach is sealed, all Daemons will dissolve back into "
                "the network. But your **badges, titles, and JC** are yours forever "
                "— a permanent record of your heroism."
            ),
            color=RARITY_COLOR.get(tier, 0x5865F2),
        )
        
        embed.add_field(
            name=f"{tier_emoji} Jarvis Stabilization",
            value=f"`{bar}` **{round(power)}%** — **{tier.title()}**{power_bonus_text}{raid_phase_text}\n{combat_unlocked} Combat Quests  •  {combine_unlocked} Combine\n🔄 Mid-Boss: {mid_status}  •  ⚔️ Raid: {raid_unlocked}",
            inline=False,
        )
        
        embed.add_field(
            name="📊 Your Progress",
            value=f"**Daemons caught:** {caught}\n**Badges earned:** {badges}\n**Equipped:** {equipped_name}",
            inline=False,
        )
        
        embed.add_field(
            name="👥 Participants",
            value=str(get_participant_count()),
            inline=True,
        )
        
        embed.add_field(
            name="⏳ Time Remaining",
            value=get_time_remaining(),
            inline=True,
        )
        
        embed.add_field(
            name="💰 End-of-Event Payout",
            value=(
                "When the breach ends, all remaining Daemons will be **automatically converted** to JC:\n"
                f"Common: **{PAYOUT_RATES['common']} JC** • Rare: **{PAYOUT_RATES['rare']} JC**\n"
                f"Legendary: **{PAYOUT_RATES['legendary']} JC** • Mythic: **{PAYOUT_RATES['mythic']} JC**"
            ),
            inline=False,
        )
        
        embed.set_footer(text="Use the buttons below to explore • Event updates hourly")
        return embed

    def _build_boss_embed(self, user_id: int) -> discord.Embed:
        """Build the boss status embed."""
        power = get_jarvis_power()
        
        embed = discord.Embed(
            title="⚔️ Boss Status",
            description=f"Jarvis Power: **{round(power)}%**",
            color=discord.Color.orange(),
        )
        
        # ── Power bonus info ──
        if power > 100:
            bonus_percent = round((power - 100) * 2)
            embed.add_field(
                name="⚡ Power Bonus",
                value=f"**{bonus_percent}%** extra damage on boss hits!",
                inline=False,
            )
        
        # Mid-boss status
        if power < 50:
            mid_status = f"🔒 Locked (need 50% Power)"
        elif get_mid_boss_defeated() or get_mid_boss_hp() <= 0:
            mid_status = "✅ Defeated!"
        elif get_mid_boss_active():
            mid_hp = get_mid_boss_hp()
            hp_percent = (mid_hp / MID_BOSS_HP) * 100
            hp_bar_filled = int(hp_percent / 10)
            hp_bar = "█" * hp_bar_filled + "░" * (10 - hp_bar_filled)
            mid_status = f"⚔️ Active!\n`{hp_bar}` **{int(mid_hp):,}** / {MID_BOSS_HP:,} HP ({round(hp_percent)}%)"
        else:
            mid_status = "🔄 Preparing..."
        
        # Final raid status
        if power < 100:
            raid_status = f"🔒 Locked (need 100% Power)"
        elif get_raid_hp() <= 0:
            raid_status = "✅ Defeated!"
        elif get_raid_active():
            raid_hp = get_raid_hp()
            hp_percent = (raid_hp / RAID_BOSS_HP) * 100
            hp_bar_filled = int(hp_percent / 10)
            hp_bar = "█" * hp_bar_filled + "░" * (10 - hp_bar_filled)
            raid_status = f"⚔️ Active!\n`{hp_bar}` **{int(raid_hp):,}** / {RAID_BOSS_HP:,} HP ({round(hp_percent)}%)"
        else:
            raid_status = "🔄 Preparing..."
        
        embed.add_field(
            name="🔄 Mid-Boss (Corrupted Core)",
            value=mid_status,
            inline=False,
        )
        
        embed.add_field(
            name="⚔️ Final Raid (Architect)",
            value=raid_status,
            inline=False,
        )
        
        embed.add_field(
            name="📋 Commands",
            value="`!bosshit` - Attack mid-boss (10 JC)\n`!bossstatus` - Check mid-boss\n`!raidhit` - Attack final raid (25 JC)\n`!raidstatus` - Check final raid",
            inline=False,
        )
        
        embed.set_footer(text="Use !bosshit for mid-boss • !raidhit for final raid")
        return embed

    @commands.command(name="event", aliases=["breach"])
    async def event_hub(self, ctx: commands.Context):
        """Open the System Breach event hub."""
        embed = self._build_hub_embed(ctx.author.id)
        view = EventHubView(ctx.author.id, self)
        
        msg = await ctx.reply(embed=embed, view=view)
        _event_hub_messages[ctx.channel.id] = msg.id

    # ── PART 10: Spawn Channel Management ──────────────────────────────────

    @commands.command(name="spawnchannel", aliases=["sc"])
    async def spawn_channel(self, ctx: commands.Context, action: str = None, channel: discord.TextChannel = None):
        """Manage spawn channels.
        Usage: !spawnchannel list  (anyone)
               !spawnchannel add #channel  (admin only)
               !spawnchannel remove #channel  (admin only)
               !spawnchannel reset  (admin only)
        """
        guild_id = ctx.guild.id
        
        # ── Check if user is admin for certain actions ──
        is_admin = ctx.author.guild_permissions.administrator
        
        if action is None:
            # ── Show current channels (anyone can use) ──
            channels = get_spawn_channels(guild_id)
            embed = discord.Embed(
                title="📋 Spawn Channels",
                description=(
                    "**Commands:**\n"
                    "`!spawnchannel list` - List all channels (anyone)\n"
                    "`!spawnchannel add #channel` - Add a channel (admin only)\n"
                    "`!spawnchannel remove #channel` - Remove a channel (admin only)\n"
                    "`!spawnchannel reset` - Reset all channels (admin only)"
                ),
                color=discord.Color.blue(),
            )
            
            if channels:
                channel_list = []
                for cid in channels:
                    ch = ctx.guild.get_channel(cid)
                    if ch:
                        channel_list.append(f"#{ch.name}")
                if channel_list:
                    embed.add_field(
                        name=f"✅ Active Channels ({len(channel_list)})",
                        value="\n".join(channel_list[:15]) if channel_list else "None",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="⚠️ Status",
                        value="⚠️ Some channels are missing or deleted.",
                        inline=False
                    )
            else:
                embed.add_field(
                    name="ℹ️ Status",
                    value="🌍 **Spawns work everywhere!**\nUse `!spawnchannel add #channel` to limit spawns to specific channels.",
                    inline=False
                )
            
            await ctx.reply(embed=embed)
            return
        
        # ── ADMIN ONLY ACTIONS ──
        if not is_admin:
            await ctx.reply("❌ You need **Administrator** permissions to use this command.")
            return
        
        if action.lower() == "add":
            if channel is None:
                await ctx.reply("❌ Please specify a channel: `!spawnchannel add #channel`")
                return
            
            add_spawn_channel(guild_id, channel.id)
            await ctx.reply(f"✅ Added #{channel.name} to spawn channels!")
        
        elif action.lower() == "remove":
            if channel is None:
                await ctx.reply("❌ Please specify a channel: `!spawnchannel remove #channel`")
                return
            
            remove_spawn_channel(guild_id, channel.id)
            await ctx.reply(f"✅ Removed #{channel.name} from spawn channels!")
        
        elif action.lower() == "reset":
            _data["spawn_channels"][str(guild_id)] = {"channels": []}
            from cogs.state import _schedule_save
            _schedule_save("spawn_channels")
            await ctx.reply("✅ Spawn channels reset! Spawns now work everywhere in this server.")
        
        elif action.lower() == "list":
            # ── List channels (anyone can use, but handled here too) ──
            channels = get_spawn_channels(guild_id)
            embed = discord.Embed(
                title="📋 Whitelisted Spawn Channels",
                color=discord.Color.blue(),
            )
            
            if not channels:
                embed.description = "🌍 No channels whitelisted. Spawns work **everywhere**."
            else:
                channel_list = []
                for cid in channels:
                    ch = ctx.guild.get_channel(cid)
                    if ch:
                        channel_list.append(f"#{ch.name}")
                    else:
                        channel_list.append(f"❌ Unknown ({cid})")
                
                embed.description = "\n".join(channel_list) if channel_list else "None"
                embed.add_field(
                    name="Total Channels",
                    value=str(len(channel_list)),
                    inline=True
                )
            
            await ctx.reply(embed=embed)

        else:
            await ctx.reply("❌ Unknown action. Use `add`, `remove`, `list`, or `reset`.")

    # ── PART 10: Mid-Boss (Corrupted Core) ──────────────────────────────────

    @commands.command(name="bosshit", aliases=["bh"])
    async def boss_hit(self, ctx: commands.Context):
        """Hit the mid-boss (costs 10 JC)."""
        power = get_jarvis_power()
        if power < 50:
            await ctx.reply(f"❌ Mid-boss unlocks at 50% Jarvis Power (currently {round(power)}%).")
            return
        
        if get_mid_boss_defeated():
            await ctx.reply("❌ The mid-boss has already been defeated!")
            return
        
        if not get_mid_boss_active():
            await ctx.reply("❌ The mid-boss hasn't spawned yet. Wait for it to appear!")
            return
        
        mid_hp = get_mid_boss_hp()
        if mid_hp <= 0:
            await ctx.reply("❌ The mid-boss has already been defeated!")
            return
        
        user_id = ctx.author.id
        
        last_hit = get_mid_boss_last_hit()
        if str(user_id) in last_hit:
            elapsed = time.time() - last_hit[str(user_id)]
            if elapsed < MID_BOSS_COOLDOWN_SECONDS:
                remaining = int(MID_BOSS_COOLDOWN_SECONDS - elapsed)
                await ctx.reply(f"⏳ You must wait **{remaining}s** before hitting the mid-boss again.")
                return
        
        if get_credits(user_id) < MID_BOSS_HIT_COST:
            await ctx.reply(f"❌ You need **{MID_BOSS_HIT_COST} 🪙** to hit the mid-boss.")
            return
        
        spend_credits(user_id, MID_BOSS_HIT_COST)
        
        # ── Calculate damage with power bonus ──
        base_damage = random.randint(MID_BOSS_DAMAGE_PER_HIT_MIN, MID_BOSS_DAMAGE_PER_HIT_MAX)
        damage_bonus = get_raid_damage_bonus()
        damage = int(base_damage * damage_bonus)
        
        new_hp = mid_hp - damage
        set_mid_boss_hp(new_hp)
        set_mid_boss_last_hit(user_id, time.time())
        
        # Track damage for rewards
        add_raid_damage(user_id, damage)
        
        # ── Show bonus if active ──
        bonus_text = ""
        if damage_bonus > 1.0:
            bonus_text = f" (⚡ {round((damage_bonus - 1) * 100)}% power bonus!)"
        
        if new_hp <= 0:
            set_mid_boss_hp(0)
            set_mid_boss_defeated(True)
            set_mid_boss_final_blow_user(user_id)
            
            # Bonus JC for defeating mid-boss
            bonus_jc = 200
            add_credits(user_id, bonus_jc)
            
            await ctx.channel.send(
                f"🎉 **{ctx.author.display_name}** landed the final blow on the Corrupted Core!\n"
                f"**{damage}** damage dealt! +{bonus_jc} JC bonus!{bonus_text}\n"
                f"⚔️ The path to the Architect is now open! Reach 100% Power to unlock the final raid!"
            )
            
            # Auto-activate final raid if power >= 100%
            if get_jarvis_power() >= 100.0 and not get_raid_active():
                set_raid_active(True)
            
            return
        
        await ctx.reply(f"⚔️ You hit the mid-boss for **{damage}** damage!{bonus_text} ({int(new_hp)} HP remaining)")


    @commands.command(name="bossstatus", aliases=["bs"])
    async def boss_status(self, ctx: commands.Context):
        """Check the mid-boss status."""
        power = get_jarvis_power()
        
        if power < 50:
            embed = discord.Embed(
                title="🔄 Mid-Boss — Locked",
                description=f"Jarvis Power must reach **50%** to unlock the mid-boss.\nCurrently at **{round(power)}%**.",
                color=discord.Color.red(),
            )
            await ctx.reply(embed=embed)
            return
        
        if get_mid_boss_defeated() or get_mid_boss_hp() <= 0:
            embed = discord.Embed(
                title="✅ Mid-Boss — Defeated!",
                description="The Corrupted Core has been destroyed! The path to the Architect is now open!",
                color=discord.Color.green(),
            )
            await ctx.reply(embed=embed)
            return
        
        if not get_mid_boss_active():
            embed = discord.Embed(
                title="🔄 Mid-Boss — Spawning Soon",
                description="The Corrupted Core is preparing to manifest. Stay tuned!",
                color=discord.Color.gold(),
            )
            await ctx.reply(embed=embed)
            return
        
        mid_hp = get_mid_boss_hp()
        hp_percent = (mid_hp / MID_BOSS_HP) * 100
        hp_bar_filled = int(hp_percent / 10)
        hp_bar = "█" * hp_bar_filled + "░" * (10 - hp_bar_filled)
        
        embed = discord.Embed(
            title="🔄 Mid-Boss — Corrupted Core",
            description="A corrupted core has manifested! Work together to destroy it!",
            color=discord.Color.orange(),
        )
        
        embed.add_field(
            name="💀 Boss HP",
            value=f"`{hp_bar}` **{int(mid_hp):,}** / {MID_BOSS_HP:,} HP ({round(hp_percent)}%)",
            inline=False,
        )
        
        embed.add_field(
            name="🎯 Cost per hit",
            value=f"{MID_BOSS_HIT_COST} 🪙",
            inline=True,
        )
        
        embed.add_field(
            name="⏳ Cooldown",
            value=f"{MID_BOSS_COOLDOWN_SECONDS}s",
            inline=True,
        )
        
        # ── Show power bonus if active ──
        if power > 100:
            bonus_percent = round((power - 100) * 2)
            embed.add_field(
                name="⚡ Power Bonus",
                value=f"**{bonus_percent}%** extra damage!",
                inline=True,
            )
        
        embed.set_footer(text="Use !bosshit to attack the mid-boss!")
        await ctx.reply(embed=embed)

    # ── PART 10: Final Raid Boss ────────────────────────────────────────────

    def _build_raid_embed(self, user_id: int) -> discord.Embed:
        """Build the raid status embed."""
        power = get_jarvis_power()
        
        if power < 100:
            return discord.Embed(
                title="⚔️ Final Raid — Locked",
                description=f"Jarvis Power must reach **100%** to unlock the final raid.\nCurrently at **{round(power)}%**.",
                color=discord.Color.red(),
            )
        
        if not get_raid_active():
            return discord.Embed(
                title="⚔️ Final Raid — Spawning Soon",
                description="Jarvis has reached 100% Power! The final raid boss is preparing to spawn.\nStay tuned — it will appear shortly!",
                color=discord.Color.gold(),
            )
        
        raid_hp = get_raid_hp()
        hp_percent = (raid_hp / RAID_BOSS_HP) * 100
        hp_bar_filled = int(hp_percent / 10)
        hp_bar = "█" * hp_bar_filled + "░" * (10 - hp_bar_filled)
        
        user_damage = get_raid_damage(user_id)
        cooldown_remaining = 0
        last_hit = get_raid_last_hit()
        if str(user_id) in last_hit:
            elapsed = time.time() - last_hit[str(user_id)]
            if elapsed < RAID_COOLDOWN_SECONDS:
                cooldown_remaining = RAID_COOLDOWN_SECONDS - elapsed
        
        # ── Power bonus info ──
        bonus_text = ""
        if power > 100:
            bonus_percent = round((power - 100) * 2)
            bonus_text = f"\n⚡ **{bonus_percent}%** damage bonus active!"
        
        embed = discord.Embed(
            title="⚔️ Final Raid — Architect",
            description=f"The Architect has manifested! Work together to bring it down!{bonus_text}",
            color=discord.Color.red(),
        )
        
        embed.add_field(
            name="💀 Boss HP",
            value=f"`{hp_bar}` **{int(raid_hp):,}** / {RAID_BOSS_HP:,} HP ({round(hp_percent)}%)",
            inline=False,
        )
        
        embed.add_field(
            name="🎯 Your Contribution",
            value=f"**{user_damage}** damage dealt\n"
                  f"Cost per hit: {RAID_HIT_COST} 🪙",
            inline=True,
        )
        
        if cooldown_remaining > 0:
            embed.add_field(
                name="⏳ Cooldown",
                value=f"Next hit in **{int(cooldown_remaining)}s**",
                inline=True,
            )
        else:
            embed.add_field(
                name="⚔️ Ready",
                value="Use `!raidhit` to attack!",
                inline=True,
            )
        
        embed.set_footer(text="Raid boss HP is shared across all servers")
        return embed

    @commands.command(name="raidhit")
    async def raid_hit(self, ctx: commands.Context):
        """Hit the final raid boss (costs 25 JC)."""
        power = get_jarvis_power()
        if power < 100:
            await ctx.reply(f"❌ Final raid unlocks at 100% Jarvis Power (currently {round(power)}%).")
            return
        
        if not get_raid_active():
            await ctx.reply("❌ The final raid boss hasn't spawned yet. Wait for it to appear!")
            return
        
        raid_hp = get_raid_hp()
        if raid_hp <= 0:
            await ctx.reply("❌ The final raid boss has already been defeated!")
            return
        
        user_id = ctx.author.id
        
        last_hit = get_raid_last_hit()
        if str(user_id) in last_hit:
            elapsed = time.time() - last_hit[str(user_id)]
            if elapsed < RAID_COOLDOWN_SECONDS:
                remaining = int(RAID_COOLDOWN_SECONDS - elapsed)
                await ctx.reply(f"⏳ You must wait **{remaining}s** before hitting the final raid boss again.")
                return
        
        if get_credits(user_id) < RAID_HIT_COST:
            await ctx.reply(f"❌ You need **{RAID_HIT_COST} 🪙** to hit the final raid boss.")
            return
        
        spend_credits(user_id, RAID_HIT_COST)
        
        # ── Calculate damage with power bonus ──
        base_damage = random.randint(RAID_DAMAGE_PER_HIT_MIN, RAID_DAMAGE_PER_HIT_MAX)
        damage_bonus = get_raid_damage_bonus()
        damage = int(base_damage * damage_bonus)
        
        new_hp = raid_hp - damage
        set_raid_hp(new_hp)
        set_raid_last_hit(user_id, time.time())
        
        # ── Track personal raid stats ──
        add_raid_damage(user_id, damage)
        bump_daemon_quest(user_id, "hit_raid", 1)
        
        # ── Track personal damage quests ──
        bump_daemon_quest(user_id, "personal_damage_100", damage)
        bump_daemon_quest(user_id, "personal_damage_500", damage)
        bump_daemon_quest(user_id, "personal_damage_1000", damage)
        bump_daemon_quest(user_id, "personal_damage_5000", damage)
        
        # ── Track personal hits quests ──
        bump_daemon_quest(user_id, "personal_hits_10", 1)
        bump_daemon_quest(user_id, "personal_hits_50", 1)
        
        # ── Track global raid damage ──
        global_damage = add_raid_global_damage(damage)
        
        # ── Track participant ──
        add_raid_participant(user_id)
        
        # ── Check quest completions ──
        await self._check_quest_completion(ctx.message, user_id, "hit_raid")
        
        # ── Check personal quests ──
        for quest_id in INDIVIDUAL_RAID_QUESTS:
            await self._check_quest_completion(ctx.message, user_id, quest_id)
        
        # ── Check global quests ──
        for quest_id in RAID_QUESTS:
            await self._check_quest_completion(ctx.message, user_id, quest_id)
        
        # ── Show bonus if active ──
        bonus_text = ""
        if damage_bonus > 1.0:
            bonus_text = f" (⚡ {round((damage_bonus - 1) * 100)}% power bonus!)"
        
        if new_hp <= 0:
            set_raid_hp(0)
            set_raid_final_blow_user(user_id)
            set_raid_defeated(True)
            
            # ── Check final blow quest ──
            bump_daemon_quest(user_id, "personal_final_blow", 1)
            await self._check_quest_completion(ctx.message, user_id, "personal_final_blow")
            
            new_badges = _check_badges(user_id)
            for badge in new_badges:
                await ctx.channel.send(f"🏅 **{ctx.author.display_name}** earned the **{badge['emoji']} {badge['name']}** badge!")
            
            await ctx.channel.send(
                f"🎉 **{ctx.author.display_name}** landed the final blow on the Architect!\n"
                f"**{damage}** damage dealt!{bonus_text} The final raid boss has been defeated!"
            )
            return
        
        await ctx.reply(f"⚔️ You hit the final raid boss for **{damage}** damage!{bonus_text} ({int(new_hp)} HP remaining)\n"
                       f"🌍 Global raid damage: **{global_damage:,}**")

    @raid_hit.error
    async def raid_hit_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(
                "**Usage:** `!raidhit`\n"
                "Attack the final raid boss for a chance to deal damage!"
            )

    @commands.command(name="raidstatus")
    async def raid_status(self, ctx: commands.Context):
        """Check the final raid boss status."""
        embed = self._build_raid_embed(ctx.author.id)
        await ctx.reply(embed=embed)

    async def _raid_auto_end_check(self):
        """Background task to end the raid after defeat."""
        await self.bot.wait_until_ready()
        
        while True:
            await asyncio.sleep(60)
            if get_raid_hp() <= 0 and get_raid_active():
                set_raid_active(False)

    # ── Raid Regeneration (Silent) ──────────────────────────────────────────

    async def _raid_regeneration_loop(self):
        """Background task: silently regenerates final raid boss HP every 5 hours."""
        await self.bot.wait_until_ready()
        
        while True:
            await asyncio.sleep(5 * 3600)  # 5 hours
            
            if not get_raid_active():
                continue
            
            if get_raid_hp() <= 0:
                continue
            
            # Silently regenerate 50 HP (no announcement)
            current_hp = get_raid_hp()
            set_raid_hp(min(RAID_BOSS_HP, current_hp + 50))

    # ── PART 11: Closing Ceremony ──────────────────────────────────────────

    async def _closing_ceremony(self):
        """Run the closing ceremony and wipe all event data."""
        user_stats = {}
        for uid_str in _data.get("daemon_quests", {}).keys():
            try:
                uid = int(uid_str)
                user = await self.bot.fetch_user(uid)
                if not user:
                    continue
                
                catch_count = _get_user_catch_count(uid)
                caught, total = _get_user_registry_progress(uid)
                raid_damage = get_raid_damage(uid)
                badges = get_system_breach_badges(uid)
                engagement_days = get_engagement_days_count(uid)
                
                user_stats[uid] = {
                    "user": user,
                    "catch_count": catch_count,
                    "registry": (caught, total),
                    "raid_damage": raid_damage,
                    "badges": badges,
                    "engagement_days": engagement_days,
                }
            except:
                continue
        
        for uid in user_stats:
            _check_badges(uid)
        
        # Send personalized closing messages (DMs only - NO channel broadcast)
        closing_embed = discord.Embed(
            title="🌀 System Breach Contained",
            description="The breach has been sealed. Jarvis's core stabilizes — your Daemons dissolve back into the network.",
            color=discord.Color.purple(),
        )
        
        for uid, stats in user_stats.items():
            user = stats["user"]
            caught = stats["catch_count"]
            caught_total = stats["registry"]
            badges = stats["badges"]
            raid_damage = stats["raid_damage"]
            engagement_days = stats["engagement_days"]
            
            payout = _calculate_payout(uid)
            
            badge_str = ", ".join([f"{BADGE_DEFS.get(b, {}).get('emoji', '')} {BADGE_DEFS.get(b, {}).get('name', b)}" for b in badges]) or "None"
            
            embed = discord.Embed(
                title="📊 Your System Breach Summary",
                description=f"**{user.display_name}**, here's your final report:",
                color=discord.Color.gold(),
            )
            embed.add_field(name="🔎 Daemons Caught", value=str(caught), inline=True)
            embed.add_field(name="🗂️ Registry Progress", value=f"{caught_total[0]}/{caught_total[1]}", inline=True)
            embed.add_field(name="⚔️ Raid Damage", value=str(raid_damage), inline=True)
            embed.add_field(name="📅 Engagement Days", value=str(engagement_days), inline=True)
            embed.add_field(name="🏅 Badges Earned", value=badge_str, inline=False)
            
            # ── #7: Collector Bonus ──
            if caught_total[0] >= caught_total[1] and caught_total[1] > 0:
                add_credits(uid, 200)
                embed.add_field(
                    name="🗂️ Collector Bonus",
                    value="You caught every species! +200 JC bonus!",
                    inline=False
                )
            
            if payout > 0:
                embed.add_field(
                    name="💰 Daemon Conversion",
                    value=f"Your remaining daemons were converted to **{payout} JC**!",
                    inline=False
                )
                add_credits(uid, payout)
            else:
                embed.add_field(
                    name="💰 Daemon Conversion",
                    value="You had no remaining daemons to convert.",
                    inline=False
                )
            
            embed.set_footer(text="Thank you for participating in the System Breach event!")
            
            try:
                await user.send(embed=embed)
            except:
                pass
        
        # Clear all event data
        for uid_str in list(_data.get("daemons", {}).keys()):
            try:
                clear_system_breach_data(int(uid_str))
            except:
                pass
        
        _data["daemons"] = {}
        _data["daemon_quests"] = {}
        _data["engagement_days"] = {}
        reset_jarvis_power()
        set_event_active(False)
        
        # Reset raid state
        reset_raid_state()
        
        # Reset mid-boss state
        reset_mid_boss_state()
        
        # Reset global raid progress
        _data["raid_global_progress"] = {
            "total_damage": 0,
            "participants": [],
            "defeated": False,
        }
        from cogs.state import _schedule_save
        _schedule_save("raid_global_progress")
        
        # Reset fusion streaks
        _data["fusion_streaks"] = {}
        from cogs.state import _schedule_save
        _schedule_save("fusion_streaks")

    @commands.command(name="endbreach")
    @commands.is_owner()
    async def end_breach(self, ctx: commands.Context):
        """End the System Breach event and run the closing ceremony."""
        event = get_event_data()
        if not event.get("active"):
            await ctx.reply("❌ The System Breach event is not active.")
            return
        
        await ctx.reply("🌀 Closing ceremony starting... This may take a moment.")
        await self._closing_ceremony()
        await ctx.reply("✅ System Breach event has ended. All data has been wiped.")

    @commands.command(name="startbreach")
    @commands.is_owner()
    async def start_breach(self, ctx: commands.Context, hours: int = 336):
        """Start the System Breach event."""
        reset_jarvis_power()
        set_event_active(True, time.time() + hours * 3600)
        
        # Reset raid state
        reset_raid_state()
        
        # Reset mid-boss state
        reset_mid_boss_state()
        
        # Reset global raid progress
        _data["raid_global_progress"] = {
            "total_damage": 0,
            "participants": [],
            "defeated": False,
        }
        from cogs.state import _schedule_save
        _schedule_save("raid_global_progress")
        
        # Reset fusion streaks
        _data["fusion_streaks"] = {}
        from cogs.state import _schedule_save
        _schedule_save("fusion_streaks")
        
        # Calculate end time
        end_time = datetime.fromtimestamp(time.time() + hours * 3600)
        formatted_end = end_time.strftime("%B %d, %Y at %I:%M %p %Z")
        
        embed = discord.Embed(
            title="🌀 System Breach — Event Started!",
            description=(
                "The mysterious corruption has been detected. "
                "Daemons are spawning across the network!\n\n"
                f"⏳ **Duration:** {hours} hours (2 weeks)\n"
                f"📅 **Ends:** {formatted_end}\n"
                f"👥 **Type:** `!event` or `/breach` to join the fight!"
            ),
            color=discord.Color.green(),
        )
        embed.add_field(
            name="📋 Getting Started",
            value=(
                "1. Use `!event` to open the hub\n"
                "2. Complete **Power Quests** to stabilize Jarvis\n"
                "3. Catch Daemons and earn rewards!\n"
                "4. At **50% Power**, fight the **Corrupted Core** (mid-boss)!\n"
                "5. At **100% Power**, fight the **Architect** (final raid)!"
            ),
            inline=False,
        )
        embed.set_footer(text="Good luck, Agents! The network needs you.")
        
        await ctx.send(embed=embed)

    @commands.command(name="spawndaemon")
    @commands.is_owner()
    async def spawn_daemon(self, ctx: commands.Context, species_id: str = None):
        """Force-spawn a Daemon (testing)."""
        if species_id is None:
            species_id = _pick_species()
        if species_id not in DAEMON_POOL:
            await ctx.reply(f"❌ Unknown species.")
            return
        now = time.time()
        _pending_spawns[ctx.channel.id] = {"species": species_id, "spawned_at": now}
        await ctx.send(embed=_species_embed(species_id, spawned=True))
        asyncio.create_task(self._despawn_after(ctx.channel.id, species_id))

    @commands.command(name="powertest")
    @commands.is_owner()
    async def power_test(self, ctx: commands.Context, power: float = 50):
        """Manually set Jarvis Power (testing). Also triggers boss spawns."""
        power = max(0.0, min(200.0, power))
        
        # ── Calculate how much power to add ──
        current_power = get_jarvis_power()
        amount_to_add = power - current_power
        
        if amount_to_add > 0:
            add_jarvis_power(amount_to_add)  # This triggers boss spawns
        else:
            # If lowering power, just set it directly
            _data["jarvis_power"]["value"] = power
            from cogs.state import _schedule_save
            _schedule_save("jarvis_power")
        
        rates = {r: round(catch_success_rate(r, min(power, 100)) * 100) for r in ["common", "rare", "legendary"]}
        bonus_text = f"\n⚡ Damage bonus: **{round((power - 100) * 2)}%**" if power > 100 else ""
        await ctx.reply(f"✅ Jarvis Power set to **{power}%**!{bonus_text}\nCatch rates: {rates}")

    @commands.command(name="setpower")
    @commands.is_owner()
    async def set_power(self, ctx: commands.Context, power: float):
        """Manually set Jarvis Power (owner only). Can go up to 200%."""
        if power < 0 or power > 200:
            await ctx.reply("❌ Power must be between 0 and 200.")
            return
        
        # ── Calculate how much power to add ──
        current_power = get_jarvis_power()
        amount_to_add = power - current_power
        
        if amount_to_add > 0:
            add_jarvis_power(amount_to_add)  # This triggers boss spawns
        else:
            # If lowering power, just set it directly
            _data["jarvis_power"]["value"] = power
            from cogs.state import _schedule_save
            _schedule_save("jarvis_power")
        
        bonus_percent = round((power - 100) * 2) if power > 100 else 0
        bonus_text = f"\n⚡ Damage bonus: **{bonus_percent}%**" if power > 100 else ""
        
        await ctx.reply(f"✅ Jarvis Power set to **{power}%**!{bonus_text}")
        
    @commands.command(name="raidreset")
    @commands.is_owner()
    async def raid_reset(self, ctx: commands.Context):
        """Reset the final raid boss (owner only)."""
        reset_raid_state()
        set_raid_active(True)
        await ctx.reply(f"✅ Final raid boss reset to {RAID_BOSS_HP} HP and activated!")

    @commands.command(name="raidactivate")
    @commands.is_owner()
    async def raid_activate(self, ctx: commands.Context):
        """Manually activate the final raid boss (owner only)."""
        set_raid_active(True)
        await ctx.reply("✅ Final raid boss activated!")

    @commands.command(name="midreset")
    @commands.is_owner()
    async def mid_reset(self, ctx: commands.Context):
        """Reset the mid-boss (owner only)."""
        reset_mid_boss_state()
        set_mid_boss_active(True)
        await ctx.reply(f"✅ Mid-boss reset to {MID_BOSS_HP} HP and activated!")

    # ── PART 12: Help & Stats ──────────────────────────────────────────────

    @commands.command(name="breachhelp")
    async def breach_help(self, ctx: commands.Context):
        """Show all System Breach event commands."""
        embed = _build_help_embed()
        await ctx.reply(embed=embed)

    @commands.command(name="breachstats")
    async def breach_stats(self, ctx: commands.Context):
        """Show overall event statistics."""
        power = get_jarvis_power()
        total_catches = 0
        total_users = len(_data.get("daemon_quests", {}))
        
        for quest_data in _data.get("daemon_quests", {}).values():
            total_catches += quest_data.get("catch_chain_1", {}).get("progress", 0)
        
        ends_at = get_event_data().get("ends_at", 0)
        time_left = max(0, int(ends_at - time.time()))
        hours, remainder = divmod(time_left, 3600)
        minutes, _ = divmod(remainder, 60)
        
        embed = discord.Embed(
            title="📊 System Breach — Event Stats",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="⚡ Jarvis Power", value=f"{round(power)}%", inline=True)
        embed.add_field(name="🔎 Total Catches", value=str(total_catches), inline=True)
        embed.add_field(name="👥 Participants", value=str(total_users), inline=True)
        embed.add_field(name="⏳ Time Remaining", value=f"{hours}h {minutes}m", inline=True)
        
        # Mid-boss status
        mid_hp = get_mid_boss_hp()
        mid_status = "Not active"
        if get_mid_boss_defeated():
            mid_status = "✅ Defeated!"
        elif get_mid_boss_active() and mid_hp > 0:
            mid_status = f"⚔️ {int(mid_hp)} HP remaining"
        elif power >= 50:
            mid_status = "🔄 Preparing..."
        
        # Final raid status
        raid_hp = get_raid_hp()
        raid_status = "Not active"
        if get_raid_active() and raid_hp > 0:
            raid_status = f"⚔️ {int(raid_hp)} HP remaining"
        elif get_raid_active() and raid_hp <= 0:
            raid_status = "✅ Defeated!"
        elif power >= 100:
            raid_status = "🔄 Preparing..."
        
        # Global raid damage
        global_damage = get_raid_global_progress().get("total_damage", 0)
        
        embed.add_field(name="🔄 Mid-Boss Status", value=mid_status, inline=True)
        embed.add_field(name="⚔️ Final Raid Status", value=raid_status, inline=True)
        embed.add_field(name="🌍 Global Raid Damage", value=f"{global_damage:,}", inline=True)
        
        await ctx.reply(embed=embed)


# ══════════════════════════════════════════════════════════════════════════════
# SLASH COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

    @app_commands.command(name="breach", description="Open the System Breach event hub")
    async def slash_breach(self, interaction: discord.Interaction):
        event = get_event_data()
        if not event.get("active"):
            await interaction.response.send_message("❌ The System Breach event is not active.", ephemeral=True)
            return
        
        embed = self._build_hub_embed(interaction.user.id)
        view = EventHubView(interaction.user.id, self)
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="breachboard", description="View System Breach leaderboard")
    async def slash_breachboard(self, interaction: discord.Interaction):
        event = get_event_data()
        if not event.get("active"):
            await interaction.response.send_message("❌ The System Breach event is not active.", ephemeral=True)
            return
        
        all_users = {}
        if "daemon_quests" in _data:
            for uid_str, quest_data in _data.get("daemon_quests", {}).items():
                try:
                    uid = int(uid_str)
                    total = quest_data.get("total_catches", {}).get("progress", 0)
                    if total > 0:
                        all_users[uid] = total
                except (ValueError, TypeError):
                    pass

        top_catchers = sorted(all_users.items(), key=lambda x: x[1], reverse=True)[:10]
        top_raiders = get_top_raid_contributors(10)

        catchers_embed = discord.Embed(
            title="🔎 Top Daemon Catchers",
            color=0x3498DB,
            description="Most Daemons caught this event"
        )

        if top_catchers:
            for rank, (user_id, count) in enumerate(top_catchers, 1):
                try:
                    user = await interaction.client.fetch_user(user_id)
                    name = user.display_name
                except:
                    name = f"User {user_id}"
                
                medal = "🥇" if rank == 1 else ("🥈" if rank == 2 else ("🥉" if rank == 3 else f"{rank}."))
                catchers_embed.add_field(
                    name=f"{medal} {name}",
                    value=f"**{count}** Daemons caught",
                    inline=False
                )
        else:
            catchers_embed.description = "No catches yet."

        raiders_embed = discord.Embed(
            title="⚔️ Top Raid Damage",
            color=0xE74C3C,
            description="Most damage dealt to the raid boss"
        )

        if top_raiders:
            for rank, (user_id, damage) in enumerate(top_raiders, 1):
                try:
                    user = await interaction.client.fetch_user(user_id)
                    name = user.display_name
                except:
                    name = f"User {user_id}"
                
                medal = "🥇" if rank == 1 else ("🥈" if rank == 2 else ("🥉" if rank == 3 else f"{rank}."))
                raiders_embed.add_field(
                    name=f"{medal} {name}",
                    value=f"**{damage}** damage",
                    inline=False
                )
        else:
            raiders_embed.description = "No raid damage yet."

        await interaction.response.send_message(embeds=[catchers_embed, raiders_embed])

    @app_commands.command(name="daemon", description="View your daemon collection")
    async def slash_daemon(self, interaction: discord.Interaction):
        """Slash command for viewing daemons."""
        event = get_event_data()
        if not event.get("active"):
            await interaction.response.send_message("❌ The System Breach event is not active.", ephemeral=True)
            return
        
        user_id = interaction.user.id
        coll = get_daemon_collection(user_id)
        owned = coll.get("owned", {})
        equipped = coll.get("equipped")
        combine_slots = coll.get("combine_slots", [None, None, None])
        
        if not owned:
            embed = discord.Embed(
                title="📦 My Daemons",
                description="You haven't caught any Daemons yet. Chat in the server for a chance to spawn one!",
                color=discord.Color.greyple(),
            )
            await interaction.response.send_message(embed=embed)
            return
        
        embed = discord.Embed(
            title="📦 My Daemons",
            description=f"You own **{len(owned)}** Daemon(s).",
            color=discord.Color.blurple(),
        )
        
        # Combined slots
        slot_names = []
        for i, slot_id in enumerate(combine_slots, 1):
            if slot_id is None:
                slot_names.append(f"**Slot {i}:** Empty")
            else:
                daemon_data = owned.get(slot_id)
                if daemon_data:
                    species_id = daemon_data.get("species")
                    daemon = DAEMON_POOL.get(species_id, {})
                    emoji = daemon.get("emoji", "❓")
                    name = daemon.get("name", "Unknown")
                    rarity = daemon.get("rarity", "unknown")
                    slot_names.append(f"**Slot {i}:** {emoji} {name} ({rarity})")
                else:
                    slot_names.append(f"**Slot {i}:** ❌ Invalid")
        
        embed.add_field(
            name="🔗 Combined Slots",
            value="\n".join(slot_names) if slot_names else "No combine slots set.",
            inline=False,
        )
        
        # All owned daemons
        lines = []
        for daemon_id, data in owned.items():
            species_id = data.get("species")
            daemon = DAEMON_POOL.get(species_id, {})
            emoji = daemon.get("emoji", "❓")
            name = daemon.get("name", "Unknown")
            rarity = daemon.get("rarity", "unknown")
            
            markers = []
            if daemon_id == equipped:
                markers.append("✅ equipped")
            if daemon_id in combine_slots:
                slot_idx = combine_slots.index(daemon_id) + 1
                markers.append(f"🔗 slot {slot_idx}")
            
            marker_str = f" ({', '.join(markers)})" if markers else ""
            lines.append(f"`{daemon_id}` {emoji} **{name}** ({rarity}){marker_str}")
        
        embed.add_field(
            name="📋 Your Collection",
            value="\n".join(lines[:20]) if lines else "No daemons yet.",
            inline=False,
        )
        if len(lines) > 20:
            embed.description += f"\n*(Showing 20 of {len(lines)} daemons)*"
        
        embed.set_footer(text="Use !equipdaemon <id> to equip • !scrapdaemon <id> to scrap")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="mybadges", description="Show your System Breach badges")
    @app_commands.describe(user="User to look up (optional)")
    async def slash_mybadges(self, interaction: discord.Interaction, user: discord.User = None):
        event = get_event_data()
        if not event.get("active"):
            await interaction.response.send_message("❌ The System Breach event is not active.", ephemeral=True)
            return
        
        target = user or interaction.user
        badges = get_system_breach_badges(target.id)
        
        if not badges:
            embed = discord.Embed(
                title=f"🏅 {target.display_name}'s System Breach Badges",
                description="No badges earned yet. Catch Daemons and complete quests to earn some!",
                color=discord.Color.greyple(),
            )
            await interaction.response.send_message(embed=embed)
            return
        
        view = BadgesView(interaction.user.id, badges)
        await interaction.response.send_message(embed=view.current_embed(), view=view)

    @app_commands.command(name="bosshit", description="Attack the mid-boss")
    async def slash_boss_hit(self, interaction: discord.Interaction):
        """Slash command for hitting the mid-boss."""
        event = get_event_data()
        if not event.get("active"):
            await interaction.response.send_message("❌ The System Breach event is not active.", ephemeral=True)
            return
        
        power = get_jarvis_power()
        if power < 50:
            await interaction.response.send_message(f"❌ Mid-boss unlocks at 50% Jarvis Power (currently {round(power)}%).", ephemeral=True)
            return
        
        if get_mid_boss_defeated():
            await interaction.response.send_message("❌ The mid-boss has already been defeated!", ephemeral=True)
            return
        
        if not get_mid_boss_active():
            await interaction.response.send_message("❌ The mid-boss hasn't spawned yet. Wait for it to appear!", ephemeral=True)
            return
        
        mid_hp = get_mid_boss_hp()
        if mid_hp <= 0:
            await interaction.response.send_message("❌ The mid-boss has already been defeated!", ephemeral=True)
            return
        
        user_id = interaction.user.id
        
        last_hit = get_mid_boss_last_hit()
        if str(user_id) in last_hit:
            elapsed = time.time() - last_hit[str(user_id)]
            if elapsed < MID_BOSS_COOLDOWN_SECONDS:
                remaining = int(MID_BOSS_COOLDOWN_SECONDS - elapsed)
                await interaction.response.send_message(f"⏳ You must wait **{remaining}s** before hitting the mid-boss again.", ephemeral=True)
                return
        
        if get_credits(user_id) < MID_BOSS_HIT_COST:
            await interaction.response.send_message(f"❌ You need **{MID_BOSS_HIT_COST} 🪙** to hit the mid-boss.", ephemeral=True)
            return
        
        await interaction.response.defer(thinking=True)
        
        spend_credits(user_id, MID_BOSS_HIT_COST)
        
        # ── Calculate damage with power bonus ──
        base_damage = random.randint(MID_BOSS_DAMAGE_PER_HIT_MIN, MID_BOSS_DAMAGE_PER_HIT_MAX)
        damage_bonus = get_raid_damage_bonus()
        damage = int(base_damage * damage_bonus)
        
        new_hp = mid_hp - damage
        set_mid_boss_hp(new_hp)
        set_mid_boss_last_hit(user_id, time.time())
        
        add_raid_damage(user_id, damage)
        
        # ── Show bonus if active ──
        bonus_text = ""
        if damage_bonus > 1.0:
            bonus_text = f" (⚡ {round((damage_bonus - 1) * 100)}% power bonus!)"
        
        if new_hp <= 0:
            set_mid_boss_hp(0)
            set_mid_boss_defeated(True)
            set_mid_boss_final_blow_user(user_id)
            
            bonus_jc = 200
            add_credits(user_id, bonus_jc)
            
            await interaction.followup.send(
                f"🎉 **{interaction.user.display_name}** landed the final blow on the Corrupted Core!\n"
                f"**{damage}** damage dealt! +{bonus_jc} JC bonus!{bonus_text}\n"
                f"⚔️ The path to the Architect is now open! Reach 100% Power to unlock the final raid!"
            )
            
            if get_jarvis_power() >= 100.0 and not get_raid_active():
                set_raid_active(True)
            
            return
        
        await interaction.followup.send(f"⚔️ You hit the mid-boss for **{damage}** damage!{bonus_text} ({int(new_hp)} HP remaining)")

    @app_commands.command(name="bossstatus", description="Check mid-boss status")
    async def slash_boss_status(self, interaction: discord.Interaction):
        """Slash command for checking mid-boss status."""
        event = get_event_data()
        if not event.get("active"):
            await interaction.response.send_message("❌ The System Breach event is not active.", ephemeral=True)
            return
        
        power = get_jarvis_power()
        
        if power < 50:
            embed = discord.Embed(
                title="🔄 Mid-Boss — Locked",
                description=f"Jarvis Power must reach **50%** to unlock the mid-boss.\nCurrently at **{round(power)}%**.",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed)
            return
        
        if get_mid_boss_defeated() or get_mid_boss_hp() <= 0:
            embed = discord.Embed(
                title="✅ Mid-Boss — Defeated!",
                description="The Corrupted Core has been destroyed! The path to the Architect is now open!",
                color=discord.Color.green(),
            )
            await interaction.response.send_message(embed=embed)
            return
        
        if not get_mid_boss_active():
            embed = discord.Embed(
                title="🔄 Mid-Boss — Spawning Soon",
                description="The Corrupted Core is preparing to manifest. Stay tuned!",
                color=discord.Color.gold(),
            )
            await interaction.response.send_message(embed=embed)
            return
        
        mid_hp = get_mid_boss_hp()
        hp_percent = (mid_hp / MID_BOSS_HP) * 100
        hp_bar_filled = int(hp_percent / 10)
        hp_bar = "█" * hp_bar_filled + "░" * (10 - hp_bar_filled)
        
        embed = discord.Embed(
            title="🔄 Mid-Boss — Corrupted Core",
            description="A corrupted core has manifested! Work together to destroy it!",
            color=discord.Color.orange(),
        )
        
        embed.add_field(
            name="💀 Boss HP",
            value=f"`{hp_bar}` **{int(mid_hp):,}** / {MID_BOSS_HP:,} HP ({round(hp_percent)}%)",
            inline=False,
        )
        
        embed.add_field(
            name="🎯 Cost per hit",
            value=f"{MID_BOSS_HIT_COST} 🪙",
            inline=True,
        )
        
        embed.add_field(
            name="⏳ Cooldown",
            value=f"{MID_BOSS_COOLDOWN_SECONDS}s",
            inline=True,
        )
        
        if power > 100:
            bonus_percent = round((power - 100) * 2)
            embed.add_field(
                name="⚡ Power Bonus",
                value=f"**{bonus_percent}%** extra damage!",
                inline=True,
            )
        
        embed.set_footer(text="Use /bosshit to attack the mid-boss!")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="raidhit", description="Attack the final raid boss")
    async def slash_raidhit(self, interaction: discord.Interaction):
        event = get_event_data()
        if not event.get("active"):
            await interaction.response.send_message("❌ The System Breach event is not active.", ephemeral=True)
            return
        
        power = get_jarvis_power()
        if power < 100:
            await interaction.response.send_message(f"❌ Final raid unlocks at 100% Jarvis Power (currently {round(power)}%).", ephemeral=True)
            return
        
        if not get_raid_active():
            await interaction.response.send_message("❌ The final raid boss hasn't spawned yet. Wait for it to appear!", ephemeral=True)
            return
        
        raid_hp = get_raid_hp()
        if raid_hp <= 0:
            await interaction.response.send_message("❌ The final raid boss has already been defeated!", ephemeral=True)
            return
        
        user_id = interaction.user.id
        
        last_hit = get_raid_last_hit()
        if str(user_id) in last_hit:
            elapsed = time.time() - last_hit[str(user_id)]
            if elapsed < RAID_COOLDOWN_SECONDS:
                remaining = int(RAID_COOLDOWN_SECONDS - elapsed)
                await interaction.response.send_message(f"⏳ You must wait **{remaining}s** before hitting the final raid boss again.", ephemeral=True)
                return
        
        if get_credits(user_id) < RAID_HIT_COST:
            await interaction.response.send_message(f"❌ You need **{RAID_HIT_COST} 🪙** to hit the final raid boss.", ephemeral=True)
            return
        
        await interaction.response.defer(thinking=True)
        
        spend_credits(user_id, RAID_HIT_COST)
        
        # ── Calculate damage with power bonus ──
        base_damage = random.randint(RAID_DAMAGE_PER_HIT_MIN, RAID_DAMAGE_PER_HIT_MAX)
        damage_bonus = get_raid_damage_bonus()
        damage = int(base_damage * damage_bonus)
        
        new_hp = raid_hp - damage
        set_raid_hp(new_hp)
        set_raid_last_hit(user_id, time.time())
        
        # ── Track personal raid stats ──
        add_raid_damage(user_id, damage)
        bump_daemon_quest(user_id, "hit_raid", 1)
        
        # ── Track personal damage quests ──
        bump_daemon_quest(user_id, "personal_damage_100", damage)
        bump_daemon_quest(user_id, "personal_damage_500", damage)
        bump_daemon_quest(user_id, "personal_damage_1000", damage)
        bump_daemon_quest(user_id, "personal_damage_5000", damage)
        
        # ── Track personal hits quests ──
        bump_daemon_quest(user_id, "personal_hits_10", 1)
        bump_daemon_quest(user_id, "personal_hits_50", 1)
        
        # ── Track global raid damage ──
        add_raid_global_damage(damage)
        
        # ── Track participant ──
        add_raid_participant(user_id)
        
        # ── Check quest completions ──
        await self._check_quest_completion(interaction.message, user_id, "hit_raid")
        
        # ── Check personal quests ──
        for quest_id in INDIVIDUAL_RAID_QUESTS:
            await self._check_quest_completion(interaction.message, user_id, quest_id)
        
        # ── Check global quests ──
        for quest_id in RAID_QUESTS:
            await self._check_quest_completion(interaction.message, user_id, quest_id)
        
        # ── Show bonus if active ──
        bonus_text = ""
        if damage_bonus > 1.0:
            bonus_text = f" (⚡ {round((damage_bonus - 1) * 100)}% power bonus!)"
        
        if new_hp <= 0:
            set_raid_hp(0)
            set_raid_final_blow_user(user_id)
            set_raid_defeated(True)
            
            # ── Check final blow quest ──
            bump_daemon_quest(user_id, "personal_final_blow", 1)
            await self._check_quest_completion(interaction.message, user_id, "personal_final_blow")
            
            new_badges = _check_badges(user_id)
            for badge in new_badges:
                await interaction.channel.send(f"🏅 **{interaction.user.display_name}** earned the **{badge['emoji']} {badge['name']}** badge!")
            
            await interaction.followup.send(
                f"🎉 **{interaction.user.display_name}** landed the final blow on the Architect!\n"
                f"**{damage}** damage dealt!{bonus_text} The final raid boss has been defeated!"
            )
            return
        
        await interaction.followup.send(f"⚔️ You hit the final raid boss for **{damage}** damage!{bonus_text} ({int(new_hp)} HP remaining)")

    @app_commands.command(name="raidstatus", description="Check final raid boss status")
    async def slash_raidstatus(self, interaction: discord.Interaction):
        event = get_event_data()
        if not event.get("active"):
            await interaction.response.send_message("❌ The System Breach event is not active.", ephemeral=True)
            return
        
        embed = self._build_raid_embed(interaction.user.id)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="breachstats", description="Show event statistics")
    async def slash_breachstats(self, interaction: discord.Interaction):
        event = get_event_data()
        if not event.get("active"):
            await interaction.response.send_message("❌ The System Breach event is not active.", ephemeral=True)
            return
        
        power = get_jarvis_power()
        total_catches = 0
        total_users = len(_data.get("daemon_quests", {}))
        
        for quest_data in _data.get("daemon_quests", {}).values():
            total_catches += quest_data.get("catch_chain_1", {}).get("progress", 0) 
        
        ends_at = get_event_data().get("ends_at", 0)
        time_left = max(0, int(ends_at - time.time()))
        hours, remainder = divmod(time_left, 3600)
        minutes, _ = divmod(remainder, 60)
        
        embed = discord.Embed(
            title="📊 System Breach — Event Stats",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="⚡ Jarvis Power", value=f"{round(power)}%", inline=True)
        embed.add_field(name="🔎 Total Catches", value=str(total_catches), inline=True)
        embed.add_field(name="👥 Participants", value=str(total_users), inline=True)
        embed.add_field(name="⏳ Time Remaining", value=f"{hours}h {minutes}m", inline=True)
        
        # Mid-boss status
        mid_hp = get_mid_boss_hp()
        mid_status = "Not active"
        if get_mid_boss_defeated():
            mid_status = "✅ Defeated!"
        elif get_mid_boss_active() and mid_hp > 0:
            mid_status = f"⚔️ {int(mid_hp)} HP remaining"
        elif power >= 50:
            mid_status = "🔄 Preparing..."
        
        # Final raid status
        raid_hp = get_raid_hp()
        raid_status = "Not active"
        if get_raid_active() and raid_hp > 0:
            raid_status = f"⚔️ {int(raid_hp)} HP remaining"
        elif get_raid_active() and raid_hp <= 0:
            raid_status = "✅ Defeated!"
        elif power >= 100:
            raid_status = "🔄 Preparing..."
        
        # Global raid damage
        global_damage = get_raid_global_progress().get("total_damage", 0)
        
        embed.add_field(name="🔄 Mid-Boss Status", value=mid_status, inline=True)
        embed.add_field(name="⚔️ Final Raid Status", value=raid_status, inline=True)
        embed.add_field(name="🌍 Global Raid Damage", value=f"{global_damage:,}", inline=True)
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="breachhelp", description="Show event commands")
    async def slash_breachhelp(self, interaction: discord.Interaction):
        embed = _build_help_embed()
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(SystemBreach(bot))