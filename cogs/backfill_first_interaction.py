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
1396510418834423951 - First ever interaction with Jarvis•27/05/2026 21:20
1491388367978496191 - First ever interaction with Jarvis•27/05/2026 21:33
910495965113368647 - First ever interaction with Jarvis•27/05/2026 22:22
1223672283730677780 - First ever interaction with Jarvis•27/05/2026 23:45
1331725906808410205 - First ever interaction with Jarvis•28/05/2026 02:44
630122700391710721 - First ever interaction with Jarvis•28/05/2026 03:23
1158795381971374141 - First ever interaction with Jarvis•28/05/2026 14:53
1490796211802869971 - First ever interaction with Jarvis•28/05/2026 16:31
374000725971304449 - First ever interaction with Jarvis•28/05/2026 17:14
1015890547673673778 - First ever interaction with Jarvis•28/05/2026 22:56
992753501316841604 - First ever interaction with Jarvis•29/05/2026 00:48
656541631746539546 - First ever interaction with Jarvis•29/05/2026 03:15
1498713491681447996 - First ever interaction with Jarvis•29/05/2026 03:24
1305128004409757769 - First ever interaction with Jarvis•29/05/2026 17:46
1146804428251332629 - First ever interaction with Jarvis•30/05/2026 01:00
1391101897120682074 - First ever interaction with Jarvis•30/05/2026 03:10
892584714710429716 - First ever interaction with Jarvis•02/06/2026 04:22
1265963911816155177 - First ever interaction with Jarvis•02/06/2026 18:51
1303344436809564181 - First ever interaction with Jarvis•02/06/2026 18:55
674705713133780994 - First ever interaction with Jarvis•02/06/2026 19:39
1411797620711751843 - First ever interaction with Jarvis•02/06/2026 19:43
760720549092917248 - First ever interaction with Jarvis•02/06/2026 20:28
1206221118411644970 - First ever interaction with Jarvis•04/06/2026 00:25
1058253101737463870 -First ever interaction with Jarvis•05/06/2026 19:47
1449622516321616032 - First ever interaction with Jarvis•06/06/2026 22:15
1039612680014671931 - First ever interaction with Jarvis•07/06/2026 15:13
621314645772337152 - First ever interaction with Jarvis•07/06/2026 15:23
423359674465648660 - First ever interaction with Jarvis•07/06/2026 16:41
955785232504717392 - First ever interaction with Jarvis•08/06/2026 00:15
1478873443335147702 - First ever interaction with Jarvis•08/06/2026 01:40
621314645772337152 - First ever interaction with Jarvis•09/06/2026 16:17
304008330437853194 - First ever interaction with Jarvis•09/06/2026 22:21
1421788632317952073 - First ever interaction with Jarvis•09/06/2026 23:54
651721922777710622 - First ever interaction with Jarvis•10/06/2026 22:52
1234410790069604465 - First ever interaction with Jarvis•11/06/2026 10:16
308318060555534347 - First ever interaction with Jarvis•14/06/2026 03:25
1140077841405448323 - First ever interaction with Jarvis•14/06/2026 03:26
960015423481450506 - First ever interaction with Jarvis•14/06/2026 12:23
1167653240352026624 - First ever interaction with Jarvis•14/06/2026 14:19
1330932728362696805 - First ever interaction with Jarvis•14/06/2026 15:53
1496035592411152445 - First ever interaction with Jarvis•14/06/2026 16:15
915840911412428841 - First ever interaction with Jarvis•14/06/2026 18:49
1386868860719861831 - First ever interaction with Jarvis•15/06/2026 06:57
843791135424249857 - First ever interaction with Jarvis•15/06/2026 18:53
1457646865666539643 - First ever interaction with Jarvis•15/06/2026 19:29
930545363876712508 - First ever interaction with Jarvis•15/06/2026 21:40
1487968733312848028 - First ever interaction with Jarvis•15/06/2026 22:47
941570450184609822 - First ever interaction with Jarvis•16/06/2026 01:08
1039612680014671931 - First ever interaction with Jarvis•16/06/2026 21:14
1470567067638825165 - First ever interaction with Jarvis•16/06/2026 22:21
1158004694703157308 - First ever interaction with Jarvis•16/06/2026 22:27
1510854545528324156 - First ever interaction with Jarvis•16/06/2026 22:33
1311250828362911765 - First ever interaction with Jarvis•18/06/2026 12:19
785357401473286177 - First ever interaction with Jarvis•18/06/2026 21:47
833953613638139955 - First ever interaction with Jarvis•18/06/2026 22:09
1224488480122601532 - First ever interaction with Jarvis•18/06/2026 22:56
345050089070395402 - First ever interaction with Jarvis•19/06/2026 01:59
1059809449470083173 - First ever interaction with Jarvis•19/06/2026 11:43
776462567073251379 - First ever interaction with Jarvis•19/06/2026 17:48
1078713179728773205 - First ever interaction with Jarvis•19/06/2026 17:50
1460221479995310090 - First ever interaction with Jarvis•19/06/2026 18:10
1397635477728661566 - First ever interaction with Jarvis•20/06/2026 00:42
1390731391171432468 - First ever interaction with Jarvis•20/06/2026 01:00
840454767620915200 - First ever interaction with Jarvis•20/06/2026 02:04
955785232504717392 - First ever interaction with Jarvis•20/06/2026 16:49
1392251946374533232 - First ever interaction with Jarvis•20/06/2026 19:39
1392895519784964166 - First ever interaction with Jarvis•20/06/2026 20:37
1383570858261348437 - First ever interaction with Jarvis•20/06/2026 20:40
545216491281448981 - First ever interaction with Jarvis•21/06/2026 08:47
832641137293787210 - First ever interaction with Jarvis•21/06/2026 16:47
897896149481050132 - First ever interaction with Jarvis•22/06/2026 17:52
1062734724499513424 - First ever interaction with Jarvis•23/06/2026 15:38
927680881626316851 - First ever interaction with Jarvis•24/06/2026 20:16
1339340207454949426 - First ever interaction with Jarvis•26/06/2026 02:05
768548512392020009 - First ever interaction with Jarvis•27/06/2026 09:51
1362901984373510244- First ever interaction with Jarvis•27/06/2026 17:50
809066800322576395 - First ever interaction with Jarvis•28/06/2026 01:51
1446565627593621595 - First ever interaction with Jarvis•28/06/2026 18:31
1056760648337457184 - First ever interaction with Jarvis•28/06/2026 18:31
1376883695243493458 - First ever interaction with Jarvis•28/06/2026 19:53
1454704363304910971 - First ever interaction with Jarvis•28/06/2026 22:30
1449628612683890692 - First ever interaction with Jarvis•28/06/2026 22:36
1380989333720268831 - First ever interaction with Jarvis•30/06/2026 22:20
1438145490813325422 - First ever interaction with Jarvis•01/07/2026 11:45
1496258962515820770 - First ever interaction with Jarvis•01/07/2026 20:40
1116807828720590969 - First ever interaction with Jarvis•01/07/2026 21:11
882282803813830697 - First ever interaction with Jarvis•03/07/2026 00:42
844034964462108672 - First ever interaction with Jarvis•03/07/2026 13:08
1127171386809536553 - First ever interaction with Jarvis•04/07/2026 01:45
1396414611225313371 - First ever interaction with Jarvis•04/07/2026 22:02
1431572123117420554 - First ever interaction with Jarvis•05/07/2026 23:34
1282326149434834996 - First ever interaction with Jarvis•05/07/2026 23:53
917607159871725639 - First ever interaction with Jarvis•07/07/2026 21:39
1433832607786733729 - First ever interaction with Jarvis•08/07/2026 10:09
871236678235353159 - First ever interaction with Jarvis•09/07/2026 11:53
372456601266683914 - First ever interaction with Jarvis•10/07/2026 01:41
1199405084321255565 - First ever interaction with Jarvis•10/07/2026 13:17
851675873856716810 - First ever interaction with Jarvis•10/07/2026 18:48
1424772930054656153 - First ever interaction with Jarvis•10/07/2026 18:50
1504787040200294540 - First ever interaction with Jarvis•10/07/2026 20:49
1467328283283816514 - First ever interaction with Jarvis•10/07/2026 20:52
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
    """Apply ENTRIES via cogs.state's already-connected in-memory data +
    Turso connection. Call this from main.py AFTER `await init_db()` so the
    Turso connection exists. Returns how many entries were applied. Safe to
    call on every startup — set_first_interaction() just overwrites."""
    entries = _parse_entries(ENTRIES)
    if not entries:
        return 0

    from cogs.state import set_first_interaction  # lazy import: avoids circulars

    for uid, ts in entries:
        set_first_interaction(int(uid), ts)
        print(f"  ✅ backfillfirst: {uid} → {datetime.fromtimestamp(ts, tz=timezone.utc):%Y-%m-%d} UTC")
    return len(entries)


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