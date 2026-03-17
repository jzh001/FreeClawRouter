"""
FreeClawRouter – storage.py
SQLite-backed persistent usage log.

Every proxied request is written as a row in `requests`. The dashboard and
/stats endpoint query this table for time-series and aggregate data.

The database file location is controlled by the FREECLAWROUTER_DATA_DIR environment
variable (default: ./data/freeclawrouter.db).
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


_DB_PATH: Path | None = None
_lock = threading.Lock()


def _db_path() -> Path:
    global _DB_PATH
    if _DB_PATH is None:
        data_dir = Path(os.environ.get("FREECLAWROUTER_DATA_DIR", "data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        _DB_PATH = data_dir / "freeclawrouter.db"
    return _DB_PATH


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    with _lock:
        db = sqlite3.connect(str(_db_path()), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,        -- unix timestamp (seconds UTC)
    provider      TEXT    NOT NULL,        -- e.g. "cerebras" or "local"
    model         TEXT    NOT NULL,        -- model id string
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    total_tokens  INTEGER DEFAULT 0,
    duration_ms   INTEGER DEFAULT 0,
    is_local      INTEGER DEFAULT 0,       -- 1 if local Ollama fallback
    is_error      INTEGER DEFAULT 0        -- 1 if upstream returned an error
);
CREATE INDEX IF NOT EXISTS idx_requests_ts       ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_provider ON requests(provider, ts);
"""


def init_db() -> None:
    """Create tables if they don't exist. Call once at startup."""
    with _conn() as db:
        db.executescript(_DDL)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def record_request(
    provider: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    duration_ms: int = 0,
    is_local: bool = False,
    is_error: bool = False,
) -> None:
    """Insert one usage record. Fire-and-forget; errors are silently ignored."""
    try:
        with _conn() as db:
            db.execute(
                """
                INSERT INTO requests
                    (ts, provider, model, input_tokens, output_tokens,
                     total_tokens, duration_ms, is_local, is_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(time.time()),
                    provider,
                    model,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    duration_ms,
                    1 if is_local else 0,
                    1 if is_error else 0,
                ),
            )
    except Exception:
        pass  # never crash the proxy because of a logging failure


# ---------------------------------------------------------------------------
# Read — today's aggregates
# ---------------------------------------------------------------------------

def _today_start_ts() -> int:
    """Unix timestamp for 00:00:00 UTC today."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def get_today_stats() -> dict:
    """
    Return per-provider totals since UTC midnight.

    Returns:
        {
          "providers": {
            "cerebras": {
              "requests": 42,
              "tokens": 150000,
              "errors": 1,
              "local_fallbacks": 0
            }, ...
          },
          "totals": {
            "requests": 200,
            "tokens": 800000,
            "errors": 3,
            "local_fallbacks": 5
          }
        }
    """
    since = _today_start_ts()
    with _conn() as db:
        rows = db.execute(
            """
            SELECT
                provider,
                COUNT(*)           AS requests,
                SUM(total_tokens)  AS tokens,
                SUM(is_error)      AS errors,
                SUM(is_local)      AS local_fallbacks
            FROM requests
            WHERE ts >= ?
            GROUP BY provider
            """,
            (since,),
        ).fetchall()

    providers: dict[str, dict] = {}
    totals = {"requests": 0, "tokens": 0, "errors": 0, "local_fallbacks": 0}
    for row in rows:
        p = {
            "requests":        row["requests"],
            "tokens":          row["tokens"] or 0,
            "errors":          row["errors"] or 0,
            "local_fallbacks": row["local_fallbacks"] or 0,
        }
        providers[row["provider"]] = p
        totals["requests"]        += p["requests"]
        totals["tokens"]          += p["tokens"]
        totals["errors"]          += p["errors"]
        totals["local_fallbacks"] += p["local_fallbacks"]

    return {"providers": providers, "totals": totals}


# ---------------------------------------------------------------------------
# Read — hourly time-series (last N hours)
# ---------------------------------------------------------------------------

def get_hourly_series(hours: int = 24) -> list[dict]:
    """
    Return request counts and token usage grouped by 1-hour buckets for the
    last `hours` hours, broken down by provider.

    Returns a list of:
        {"hour_ts": 1234567890, "provider": "cerebras", "requests": 5, "tokens": 20000}
    """
    since = int(time.time()) - hours * 3600
    with _conn() as db:
        rows = db.execute(
            """
            SELECT
                (ts / 3600) * 3600   AS hour_ts,
                provider,
                COUNT(*)             AS requests,
                SUM(total_tokens)    AS tokens
            FROM requests
            WHERE ts >= ?
            GROUP BY hour_ts, provider
            ORDER BY hour_ts ASC
            """,
            (since,),
        ).fetchall()

    return [
        {
            "hour_ts":  row["hour_ts"],
            "provider": row["provider"],
            "requests": row["requests"],
            "tokens":   row["tokens"] or 0,
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Read — 7-day history
# ---------------------------------------------------------------------------

def get_daily_series(days: int = 7) -> list[dict]:
    """
    Return daily totals per provider for the last `days` days.
    """
    since = int(time.time()) - days * 86400
    with _conn() as db:
        rows = db.execute(
            """
            SELECT
                (ts / 86400) * 86400  AS day_ts,
                provider,
                COUNT(*)              AS requests,
                SUM(total_tokens)     AS tokens
            FROM requests
            WHERE ts >= ?
            GROUP BY day_ts, provider
            ORDER BY day_ts ASC
            """,
            (since,),
        ).fetchall()

    return [
        {
            "day_ts":   row["day_ts"],
            "provider": row["provider"],
            "requests": row["requests"],
            "tokens":   row["tokens"] or 0,
        }
        for row in rows
    ]
