"""One-off: import locally-logged predictions into the production Postgres log.

INSERT-ONLY, deliberately.

`store.migrate_json_into_db()` must NOT be used for this. It calls
`upsert_many()`, which overwrites existing rows — and for ~200 keys the
production row is *better* than the local one: local grading stopped on
2026-09-09 while production graded through 09-12, so 55 fixtures are settled
in Postgres and still ungraded in the local file. An upsert would un-settle
all of them and wipe the visible track record.

Every write here goes through ON CONFLICT (key) DO NOTHING, so an existing
row is never touched. Re-running is therefore safe and idempotent.

Usage (dry run prints what would happen and changes nothing):

    DATABASE_URL=... python scripts/import_local_log.py
    DATABASE_URL=... python scripts/import_local_log.py --apply
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psycopg

TABLE = "prediction_log"
LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "prediction_log.json"


def settled(entry: dict) -> bool:
    return bool(entry.get("graded")) and entry.get("actual_home_score") is not None


def main() -> int:
    apply = "--apply" in sys.argv
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set.")
        return 2

    local = json.loads(LOG_PATH.read_text(encoding="utf-8"))
    # Guard against a malformed/duplicated file: last occurrence of a key wins,
    # matching how the JSON backend itself de-duplicates on write.
    by_key = {e["key"]: e for e in local}
    print(f"local file: {len(local)} entries, {len(by_key)} unique keys, "
          f"{sum(1 for e in by_key.values() if settled(e))} settled")

    with psycopg.connect(url, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT key, entry FROM {TABLE}")
            existing = {k: e for k, e in cur.fetchall()}

        before_total = len(existing)
        before_settled = sum(1 for e in existing.values() if settled(e))
        print(f"postgres before: {before_total} rows, {before_settled} settled")

        to_insert = [e for k, e in by_key.items() if k not in existing]
        print(f"\nwould insert {len(to_insert)} new rows "
              f"({sum(1 for e in to_insert if settled(e))} already settled)")
        print(f"would skip {len(by_key) - len(to_insert)} keys already in postgres "
              f"(their production rows are left exactly as they are)")
        if to_insert:
            dates = sorted(e["date"] for e in to_insert if e.get("date"))
            print(f"new rows span {dates[0]} -> {dates[-1]}")

        if not apply:
            print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
            return 0

        if not to_insert:
            print("\nNothing to insert.")
            return 0

        with conn.cursor() as cur:
            cur.executemany(
                f"""INSERT INTO {TABLE} (key, sport, league, match_date, graded, entry)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (key) DO NOTHING""",
                [(e["key"], e.get("sport"), e.get("league"), e.get("date"),
                  bool(e.get("graded")), json.dumps(e)) for e in to_insert])
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(f"SELECT key, entry FROM {TABLE}")
            after = {k: e for k, e in cur.fetchall()}

    after_settled = sum(1 for e in after.values() if settled(e))
    print(f"\npostgres after: {len(after)} rows, {after_settled} settled")
    print(f"delta: +{len(after) - before_total} rows, +{after_settled - before_settled} settled")

    # The whole point of the insert-only design: nothing that was settled
    # before may have stopped being settled.
    if after_settled < before_settled:
        print("\n!! REGRESSION: settled count dropped. Investigate before trusting the log.")
        return 1
    print("OK — no previously-settled fixture was modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
