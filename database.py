# database.py
"""
database.py — SQLite database setup and connection management.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Generator

DB_PATH = os.getenv("DB_PATH", "/data/store_intelligence.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_cursor() -> Generator[sqlite3.Cursor, None, None]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create all tables if they don't exist."""
    with db_cursor() as cur:
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                event_id     TEXT PRIMARY KEY,
                store_id     TEXT NOT NULL,
                camera_id    TEXT NOT NULL,
                visitor_id   TEXT NOT NULL,
                event_type   TEXT NOT NULL,
                timestamp    TEXT NOT NULL,
                zone_id      TEXT,
                dwell_ms     INTEGER DEFAULT 0,
                is_staff     INTEGER DEFAULT 0,
                confidence   REAL,
                queue_depth  INTEGER,
                sku_zone     TEXT,
                session_seq  INTEGER DEFAULT 0,
                ingested_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_events_store_ts
                ON events(store_id, timestamp);

            CREATE INDEX IF NOT EXISTS idx_events_visitor
                ON events(visitor_id, store_id);

            CREATE INDEX IF NOT EXISTS idx_events_type
                ON events(event_type, store_id);

            CREATE TABLE IF NOT EXISTS pos_transactions (
                transaction_id   TEXT PRIMARY KEY,
                store_id         TEXT NOT NULL,
                timestamp        TEXT NOT NULL,
                basket_value_inr REAL
            );

            CREATE INDEX IF NOT EXISTS idx_pos_store_ts
                ON pos_transactions(store_id, timestamp);

            CREATE TABLE IF NOT EXISTS store_feed_status (
                store_id           TEXT PRIMARY KEY,
                last_event_ts      TEXT,
                last_updated       TEXT DEFAULT (datetime('now'))
            );
        """)
    print(f"[DB] Initialised at {DB_PATH}")