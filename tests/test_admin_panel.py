from types import SimpleNamespace

from web.app import build_admin_panel_payload


class DummyGuild:
    def __init__(self, guild_id, name, member_count, joined_at):
        self.id = guild_id
        self.name = name
        self.member_count = member_count
        self.joined_at = joined_at
        self.created_at = joined_at
        self.owner = None


def test_build_admin_panel_payload_includes_guild_members_and_ban_state():
    guild = DummyGuild(123, "Test Guild", 10, "2024-01-01T00:00:00Z")
    bot = SimpleNamespace(guilds=[guild], user=SimpleNamespace(name="Jarvis"))

    payload = build_admin_panel_payload(
        bot,
        guild_bans={123: {"reason": "spam", "banned_at": 1704067200}},
        bot_bans={"99": {"reason": "abuse", "expires": None}},
    )

    assert payload["guilds"][0]["id"] == 123
    assert payload["guilds"][0]["member_count"] == 10
    assert payload["guilds"][0]["joined_at"] == "2024-01-01"
    assert payload["guilds"][0]["is_banned"] is True
    assert payload["bot_bans"][0]["user_id"] == "99"
