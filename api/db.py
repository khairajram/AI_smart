"""
api/db.py
─────────
SQLite persistence layer for the Store Intelligence API.

Design decisions
----------------
* SQLite with WAL journal mode — allows concurrent reads while a single
  writer is active.  Sufficient for the challenge workload (one ingest
  thread, many readers).
* INSERT OR IGNORE on event_id — idempotency at the DB level.  Safe to
  call POST /events/ingest twice with the same payload.
* Single module-level connection factory — thread-safe via RLock.
* All query results returned as sqlite3.Row so columns are accessible
  by name (e.g. row["visitor_id"]).

Schema
------
events table stores the full event schema. Indexes on store_id,
timestamp, visitor_id, and event_type cover all analytics queries.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

# Path resolves relative to project root when running via uvicorn / docker
_DB_PATH = Path("data/store_intelligence.db")
_lock    = threading.RLock()


# ─────────────────────────────────────────────────────────────────────
#  Connection factory
# ─────────────────────────────────────────────────────────────────────

def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")   # safe + fast
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA cache_size=-32000")    # 32 MB cache
    return conn


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Context manager: acquire lock, yield connection, commit or rollback."""
    with _lock:
        conn = _make_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ─────────────────────────────────────────────────────────────────────
#  Schema initialisation
# ─────────────────────────────────────────────────────────────────────

_CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT    PRIMARY KEY,
    store_id    TEXT    NOT NULL,
    camera_id   TEXT    NOT NULL,
    visitor_id  TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,
    timestamp   TEXT    NOT NULL,
    zone_id     TEXT,
    dwell_ms    INTEGER DEFAULT 0,
    is_staff    INTEGER DEFAULT 0,    -- 0/1 boolean
    confidence  REAL    DEFAULT 1.0,
    queue_depth INTEGER,
    sku_zone    TEXT,
    session_seq INTEGER,
    ingested_at TEXT    NOT NULL
);

-- Query-pattern indexes
CREATE INDEX IF NOT EXISTS idx_ev_store    ON events (store_id);
CREATE INDEX IF NOT EXISTS idx_ev_ts       ON events (timestamp);
CREATE INDEX IF NOT EXISTS idx_ev_vid      ON events (visitor_id);
CREATE INDEX IF NOT EXISTS idx_ev_type     ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_ev_zone     ON events (zone_id);
CREATE INDEX IF NOT EXISTS idx_ev_store_ts ON events (store_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_ev_store_type ON events (store_id, event_type);
"""


def init_db(db_path: Optional[Path] = None) -> None:
    """
    Create tables and indexes if they don't exist.
    Call once at API startup.
    """
    global _DB_PATH
    if db_path:
        _DB_PATH = db_path

    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with get_db() as conn:
        conn.executescript(_CREATE_SCHEMA)

    logger.info("SQLite DB initialised at %s", _DB_PATH)


# ─────────────────────────────────────────────────────────────────────
#  Event insertion
# ─────────────────────────────────────────────────────────────────────

def insert_events(events: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Bulk-insert events.  INSERT OR IGNORE ensures idempotency by event_id.

    Parameters
    ----------
    events : list of dicts matching the StoreEvent schema

    Returns
    -------
    dict with keys: accepted, duplicate
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    rows: List[tuple] = []
    for e in events:
        meta = e.get("metadata") or {}
        if isinstance(meta, dict):
            qd  = meta.get("queue_depth")
            skz = meta.get("sku_zone")
            seq = meta.get("session_seq")
        else:
            # Pydantic model
            qd  = getattr(meta, "queue_depth", None)
            skz = getattr(meta, "sku_zone", None)
            seq = getattr(meta, "session_seq", None)

        rows.append((
            str(e["event_id"]),
            str(e["store_id"]),
            str(e["camera_id"]),
            str(e["visitor_id"]),
            str(e["event_type"]) if not hasattr(e["event_type"], "value") else e["event_type"].value,
            str(e["timestamp"]),
            e.get("zone_id"),
            int(e.get("dwell_ms") or 0),
            1 if e.get("is_staff") else 0,
            float(e.get("confidence") or 1.0),
            int(qd) if qd is not None else None,
            str(skz) if skz else None,
            int(seq) if seq is not None else None,
            now_iso,
        ))

    if not rows:
        return {"accepted": 0, "duplicate": 0}

    with get_db() as conn:
        before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.executemany(
            """
            INSERT OR IGNORE INTO events
            (event_id, store_id, camera_id, visitor_id, event_type, timestamp,
             zone_id, dwell_ms, is_staff, confidence, queue_depth, sku_zone,
             session_seq, ingested_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        after = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    accepted  = after - before
    duplicate = len(rows) - accepted
    return {"accepted": accepted, "duplicate": duplicate}


# ─────────────────────────────────────────────────────────────────────
#  Event queries
# ─────────────────────────────────────────────────────────────────────

def query_events(
    store_id:      str,
    since:         Optional[str]       = None,
    until:         Optional[str]       = None,
    event_types:   Optional[List[str]] = None,
    exclude_staff: bool                = True,
    visitor_id:    Optional[str]       = None,
) -> List[sqlite3.Row]:
    """
    Flexible event query for a single store.

    Parameters
    ----------
    store_id      : filter to this store
    since         : ISO-8601 lower bound (inclusive)
    until         : ISO-8601 upper bound (inclusive)
    event_types   : list of event_type strings to include
    exclude_staff : if True, exclude is_staff=1 rows
    visitor_id    : restrict to a single visitor
    """
    where:  List[str] = ["store_id = ?"]
    params: List[Any] = [store_id]

    if exclude_staff:
        where.append("is_staff = 0")
    if since:
        where.append("timestamp >= ?")
        params.append(since)
    if until:
        where.append("timestamp <= ?")
        params.append(until)
    if event_types:
        ph = ",".join("?" * len(event_types))
        where.append(f"event_type IN ({ph})")
        params.extend(event_types)
    if visitor_id:
        where.append("visitor_id = ?")
        params.append(visitor_id)

    sql = (
        "SELECT * FROM events WHERE "
        + " AND ".join(where)
        + " ORDER BY timestamp ASC"
    )
    with get_db() as conn:
        return conn.execute(sql, params).fetchall()


def get_last_event_per_store() -> Dict[str, str]:
    """
    Return {store_id: last_timestamp_iso} for all stores.
    Used by /health to detect STALE_FEED conditions.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT store_id, MAX(timestamp) AS last_ts FROM events GROUP BY store_id"
        ).fetchall()
    return {row["store_id"]: row["last_ts"] for row in rows}


def get_known_store_ids() -> List[str]:
    """Return all store IDs that have at least one ingested event."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT store_id FROM events ORDER BY store_id"
        ).fetchall()
    return [row["store_id"] for row in rows]


def clear_store_events(store_id: str) -> int:
    """Delete all events for a store. Returns count deleted."""
    with get_db() as conn:
        conn.execute("DELETE FROM events WHERE store_id = ?", (store_id,))
        return conn.execute("SELECT changes()").fetchone()[0]


def event_count() -> int:
    """Total events in DB — used by /health."""
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
