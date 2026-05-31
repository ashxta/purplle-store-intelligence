# ingestion.py
"""
ingestion.py — Ingest, validate, and deduplicate events.
Idempotent by event_id: calling twice with same payload is safe.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from database import db_cursor
from models import IngestResponse, StoreEvent

logger = logging.getLogger(__name__)


def ingest_events(events: List[StoreEvent]) -> IngestResponse:
    accepted = 0
    rejected = 0
    duplicate = 0
    errors: List[Dict[str, Any]] = []

    with db_cursor() as cur:
        for ev in events:
            try:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO events
                        (event_id, store_id, camera_id, visitor_id, event_type,
                         timestamp, zone_id, dwell_ms, is_staff, confidence,
                         queue_depth, sku_zone, session_seq)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        ev.event_id,
                        ev.store_id,
                        ev.camera_id,
                        ev.visitor_id,
                        ev.event_type,
                        ev.timestamp,
                        ev.zone_id,
                        ev.dwell_ms,
                        int(ev.is_staff),
                        ev.confidence,
                        ev.metadata.queue_depth,
                        ev.metadata.sku_zone,
                        ev.metadata.session_seq,
                    ),
                )
                if cur.rowcount == 0:
                    duplicate += 1
                else:
                    accepted += 1
                    # Update feed status
                    cur.execute(
                        """
                        INSERT INTO store_feed_status (store_id, last_event_ts, last_updated)
                        VALUES (?, ?, datetime('now'))
                        ON CONFLICT(store_id) DO UPDATE SET
                            last_event_ts = excluded.last_event_ts,
                            last_updated  = excluded.last_updated
                        WHERE excluded.last_event_ts > store_feed_status.last_event_ts
                              OR store_feed_status.last_event_ts IS NULL
                        """,
                        (ev.store_id, ev.timestamp),
                    )
            except Exception as e:
                rejected += 1
                errors.append({"event_id": ev.event_id, "error": str(e)})
                logger.warning("Rejected event %s: %s", ev.event_id, e)

    logger.info(
        "Ingest complete: accepted=%d duplicate=%d rejected=%d",
        accepted, duplicate, rejected,
    )
    return IngestResponse(
        accepted=accepted,
        rejected=rejected,
        duplicate=duplicate,
        errors=errors,
    )


def load_pos_transactions(csv_path: str) -> int:
    """Load POS transactions from CSV into the database. Returns row count."""
    import csv

    loaded = 0
    with open(csv_path) as f, db_cursor() as cur:
        for row in csv.DictReader(f):
            cur.execute(
                """
                INSERT OR IGNORE INTO pos_transactions
                    (transaction_id, store_id, timestamp, basket_value_inr)
                VALUES (?,?,?,?)
                """,
                (
                    row["transaction_id"],
                    row["store_id"],
                    row["timestamp"],
                    float(row["basket_value_inr"]),
                ),
            )
            if cur.rowcount:
                loaded += 1
    logger.info("Loaded %d POS transactions", loaded)
    return loaded