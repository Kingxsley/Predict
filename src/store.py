"""
Persistence for the prediction log.

Two backends, chosen automatically:

  * **Postgres**, when DATABASE_URL is set. This is what a deployed instance
    should use. Without it the log lives on the container's local disk and is
    destroyed on every redeploy, so a track record can never accumulate past
    the most recent deploy — which is exactly the state production was in.
  * **A JSON file**, otherwise. Keeps local development and the CLI working
    with no database to run.

The interface is deliberately small and row-oriented rather than
"read-modify-write the whole file", because grading updates a few hundred
rows at a time and appending one fixture should not rewrite the entire log.

Failure policy: a database that cannot be reached must never take the site
down. `available()` reports the backend actually in use, and every write is
best-effort — the fixture board and predictions do not depend on the log.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config

DATABASE_URL = os.environ.get("DATABASE_URL", "")
JSON_PATH = Path(os.environ.get("PREDICTION_LOG_PATH",
                                str(config.DATA_DIR / "prediction_log.json")))

_lock = threading.Lock()
_pool: Any = None
_backend = "json"
_init_error: str | None = None

TABLE = "prediction_log"
_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    key         TEXT PRIMARY KEY,
    sport       TEXT,
    league      TEXT,
    match_date  TEXT,
    graded      BOOLEAN NOT NULL DEFAULT FALSE,
    entry       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS {TABLE}_pending_idx ON {TABLE} (graded, match_date);
"""


def _connect():
    import psycopg
    return psycopg.connect(DATABASE_URL, connect_timeout=10)


def init() -> str:
    """Picks a backend and prepares it. Safe to call repeatedly."""
    global _backend, _init_error
    with _lock:
        if _backend != "json" or not DATABASE_URL:
            return _backend
        try:
            with _connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(_DDL)
                conn.commit()
            _backend = "postgres"
            _init_error = None
        except Exception as e:
            # Fall back rather than fail: a missing database should cost the
            # track record's durability, not the whole service.
            _backend = "json"
            _init_error = f"{type(e).__name__}: {e}"
        return _backend


def backend() -> dict:
    return {"backend": _backend,
            "durable": _backend == "postgres",
            "path": None if _backend == "postgres" else str(JSON_PATH),
            "error": _init_error}


# ------------------------------------------------------------------ json

def _json_load() -> list[dict]:
    if not JSON_PATH.exists():
        return []
    try:
        return json.loads(JSON_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _json_write(entries: list[dict]) -> None:
    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = JSON_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    tmp.replace(JSON_PATH)


# --------------------------------------------------------------- public

def load_all() -> list[dict]:
    if _backend == "postgres":
        try:
            with _connect() as conn, conn.cursor() as cur:
                cur.execute(f"SELECT entry FROM {TABLE}")
                return [row[0] for row in cur.fetchall()]
        except Exception:
            return []
    return _json_load()


def insert_if_absent(entry: dict) -> bool:
    """Adds one entry unless its key already exists. Returns True if written."""
    if _backend == "postgres":
        try:
            with _connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f"""INSERT INTO {TABLE} (key, sport, league, match_date, graded, entry)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (key) DO NOTHING""",
                    (entry["key"], entry.get("sport"), entry.get("league"),
                     entry.get("date"), bool(entry.get("graded")), json.dumps(entry)))
                written = cur.rowcount > 0
                conn.commit()
                return written
        except Exception:
            return False
    with _lock:
        entries = _json_load()
        if any(e.get("key") == entry["key"] for e in entries):
            return False
        entries.append(entry)
        _json_write(entries)
        return True


def upsert_many(entries: list[dict]) -> int:
    """Writes back a batch of modified entries. Returns the number written."""
    if not entries:
        return 0
    if _backend == "postgres":
        try:
            with _connect() as conn, conn.cursor() as cur:
                cur.executemany(
                    f"""INSERT INTO {TABLE} (key, sport, league, match_date, graded, entry, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, now())
                        ON CONFLICT (key) DO UPDATE SET
                            graded = EXCLUDED.graded,
                            entry = EXCLUDED.entry,
                            updated_at = now()""",
                    [(e["key"], e.get("sport"), e.get("league"), e.get("date"),
                      bool(e.get("graded")), json.dumps(e)) for e in entries])
                conn.commit()
                return len(entries)
        except Exception:
            return 0
    with _lock:
        current = {e.get("key"): e for e in _json_load()}
        for e in entries:
            current[e["key"]] = e
        _json_write(list(current.values()))
        return len(entries)


def migrate_json_into_db() -> int:
    """One-shot import of an existing on-disk log into Postgres, so switching
    a running deployment to a database keeps whatever it had already logged."""
    if _backend != "postgres":
        return 0
    existing = _json_load()
    return upsert_many(existing) if existing else 0
