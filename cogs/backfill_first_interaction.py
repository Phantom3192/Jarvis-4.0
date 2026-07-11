"""
Backfill first_interaction (and seen) for users whose real first-interaction
date got lost before that table existed.

HOW TO USE
1. Fill in ENTRIES below — one line per user, exactly in this format:
       user_id - DD/MM/YYYY
   e.g.
       1467328283283816514 - 10/07/2026
       1504787040200294540 - 10/07/2026

2. Two ways to actually apply it:

   a) STANDALONE (no bot running): from the project root, with TURSO_URL /
      TURSO_TOKEN set in your .env:
          python scripts/backfill_first_interaction.py
      Opens its own direct Turso connection, writes the change, exits.
      Restart the bot afterwards so it reloads state from Turso.

   b) AUTO, ON BOT STARTUP (what main.py calls): run_backfill() below reuses
      cogs.state's already-connected in-memory data + Turso connection, so
      it takes effect immediately without a second restart. It's safe to
      leave wired into main.py permanently — every call just OVERWRITES the
      same entries with the same values, so re-running on every deploy is a
      harmless no-op once ENTRIES stops changing. Just clear ENTRIES back to
      empty once you're done backfilling, so future deploys don't re-log it.

This does NOT touch any other table (credits, stats, etc). Going forward,
mark_seen() in cogs/state.py stamps new users automatically — this file is
only for fixing pre-existing data.
"""
import json
import re
from datetime import datetime, timezone

# ── Fill this in — one line per user. Accepts either:
#      user_id - DD/MM/YYYY
#    or a straight paste of the log embed line:
#      user_id - First ever interaction with Jarvis•DD/MM/YYYY HH:MM
#    (any extra text/time around the date is ignored — only the date matters)
ENTRIES = """
1049677357927125012 - 02/04/2026
707258471329955851 - 02/04/2026
1146398794724941964 - 03/04/2026
1196508345658523691 - 03/04/2026
1398882668401135657 - 05/04/2026
1316367195777011786 - 07/04/2026
1413168299050532965 - 07/04/2026
1113148246370562109 - 08/04/2026
1433930959174242444 - 08/04/2026
1060796603595767808 - 08/04/2026
979993503058763776 - 08/04/2026
561510329993920523 - 09/04/2026
1446847412235927626 - 09/04/2026
1120655318175715378 - 26/04/2026
873549393985409034 - 27/04/2026
1476242342901055591 - 27/04/2026
909620128252067840 - 24/05/2026
1239441913631871049 - 24/05/2026
1165694877267402766 - 26/05/2026
780789015837671445 - 26/05/2026
1380843851077517332 - 26/05/2026
1371537669774905354 - 26/05/2026
1322175619597074582 -  27/05/2026
1344457134750044180 - 27/05/2026
1285397741798690849 - 27/05/2026
1442805546196668459 - 27/05/2026
1505519622764630136 - 27/05/2026
1241639520751976470 - 27/05/2026
821633464175558657 - 27/05/2026
1042722447683768381 - 27/05/2026
1478720092500918414 - 27/05/2026
1336247895946690601 - 27/05/2026
807306907261206569 - 27/05/2026
1279086222173540464 - 27/05/2026
"""
# ──────────────────────────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
_UID_RE = re.compile(r"^\s*(\d+)")


def _parse_entries(raw: str) -> list[tuple[str, float]]:
    parsed = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        uid_match = _UID_RE.match(line)
        date_match = _DATE_RE.search(line)
        if not uid_match or not date_match:
            print(f"  ⚠️ skipped unparseable line: {line}")
            continue
        uid_part = uid_match.group(1)
        day, month, year = date_match.groups()
        dt = datetime(int(year), int(month), int(day), tzinfo=timezone.utc)
        parsed.append((uid_part, dt.timestamp()))
    return parsed


def run_backfill() -> int:
    """Apply ENTRIES directly against cogs.state's in-memory data + Turso
    connection. Call this from main.py AFTER `await init_db()` so the Turso
    connection exists. Returns how many entries were applied — 0 on any
    failure. Deliberately never raises: a one-off backfill helper must never
    be able to crash bot startup, so every failure mode here is caught and
    logged instead of propagated.

    Writes directly to cogs.state's internal _data / _schedule_save rather
    than calling a named helper like set_first_interaction(), since that
    helper may not exist on whatever version of state.py is actually
    deployed — this only needs _data (a plain dict) and _schedule_save
    (present in every version of state.py), so it can't drift out of sync
    with state.py's public API.
    """
    try:
        entries = _parse_entries(ENTRIES)
        if not entries:
            return 0

        import cogs.state as state

        state._data.setdefault("first_interaction", {})
        state._data.setdefault("seen", set())

        for uid, ts in entries:
            state._data["first_interaction"][str(uid)] = ts
            state._data["seen"].add(int(uid))
            print(f"  ✅ backfillfirst: {uid} → {datetime.fromtimestamp(ts, tz=timezone.utc):%Y-%m-%d} UTC")

        state._schedule_save("first_interaction")
        state._schedule_save("seen")
        return len(entries)

    except Exception as e:
        print(f"❌ backfillfirst: skipped due to error — {e}")
        return 0


def _standalone_main():
    """Direct-to-Turso path for running this file with no bot process at
    all (`python scripts/backfill_first_interaction.py`)."""
    import os
    from dotenv import load_dotenv
    load_dotenv()

    entries = _parse_entries(ENTRIES)
    if not entries:
        print("ENTRIES is empty — fill in the block at the top of this script first.")
        return

    turso_url = os.getenv("TURSO_URL", "").strip().lstrip("=").strip()
    turso_token = os.getenv("TURSO_TOKEN", "").strip().lstrip("=").strip()
    if not turso_url or not turso_token:
        print("❌ TURSO_URL / TURSO_TOKEN not found in environment (.env).")
        return

    import libsql_experimental as libsql
    conn = libsql.connect(database=turso_url, auth_token=turso_token)

    def _load(key: str, default):
        row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _save(key: str, value) -> None:
        conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        conn.commit()

    first_interaction: dict[str, float] = _load("first_interaction", {})
    seen: list[str] = _load("seen", [])
    seen_set = set(seen)

    for uid, ts in entries:
        first_interaction[uid] = ts
        seen_set.add(uid)
        print(f"  {uid} → {datetime.fromtimestamp(ts, tz=timezone.utc):%Y-%m-%d} UTC")

    _save("first_interaction", first_interaction)
    _save("seen", sorted(seen_set, key=int))

    print(f"\n✅ Backfilled {len(entries)} user(s). Restart the bot to pick this up.")


if __name__ == "__main__":
    _standalone_main()