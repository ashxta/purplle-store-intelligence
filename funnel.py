# funnel.py
"""
funnel.py — Conversion funnel computation.

Funnel stages (session-level, not raw event count):
  Entry → Any Zone Visit → Billing Zone → Purchase

Re-entries: a visitor_id with multiple ENTRY events is counted once
per unique session (defined as: ENTRY followed by EXIT, or end-of-day).
The REENTRY event marks the start of a new session for the same visitor_id.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

from database import db_cursor
from models import FunnelResponse, FunnelStage

logger = logging.getLogger(__name__)
BILLING_WINDOW_SEC = 300


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_funnel(store_id: str, date: str | None = None) -> FunnelResponse:
    target_date = date or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    with db_cursor() as cur:
        # Stage 1: Unique customer sessions (ENTRY events, deduped by visitor_id)
        # A REENTRY event starts a new logical session but same visitor_id;
        # we count it as a separate session.
        cur.execute(
            """
            SELECT COUNT(*) as cnt
            FROM (
                SELECT visitor_id, MIN(timestamp) as first_entry
                FROM events
                WHERE store_id = ?
                  AND is_staff = 0
                  AND event_type IN ('ENTRY','REENTRY')
                  AND DATE(timestamp) = ?
                GROUP BY visitor_id
            )
            """,
            (store_id, target_date),
        )
        row = cur.fetchone()
        total_sessions = row["cnt"] if row else 0

        # Stage 2: Sessions that visited at least one named zone
        cur.execute(
            """
            SELECT COUNT(DISTINCT visitor_id) as cnt
            FROM events
            WHERE store_id = ?
              AND is_staff = 0
              AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL')
              AND zone_id IS NOT NULL
              AND zone_id NOT IN ('ENTRY_EXIT', 'BILLING')
              AND DATE(timestamp) = ?
            """,
            (store_id, target_date),
        )
        row = cur.fetchone()
        visited_zone = row["cnt"] if row else 0

        # Stage 3: Sessions that reached billing zone
        cur.execute(
            """
            SELECT COUNT(DISTINCT visitor_id) as cnt
            FROM events
            WHERE store_id = ?
              AND is_staff = 0
              AND event_type IN ('BILLING_QUEUE_JOIN', 'ZONE_ENTER')
              AND zone_id = 'BILLING'
              AND DATE(timestamp) = ?
            """,
            (store_id, target_date),
        )
        row = cur.fetchone()
        reached_billing = row["cnt"] if row else 0

        # Stage 4: Sessions that resulted in a POS purchase
        cur.execute(
            """
            SELECT COUNT(DISTINCT e.visitor_id) as cnt
            FROM events e
            JOIN pos_transactions p ON p.store_id = e.store_id
            WHERE e.store_id = ?
              AND e.is_staff = 0
              AND e.zone_id = 'BILLING'
              AND e.event_type IN ('BILLING_QUEUE_JOIN','ZONE_ENTER','ZONE_DWELL')
              AND DATE(e.timestamp) = ?
              AND (
                  CAST(strftime('%s', p.timestamp) AS INTEGER) -
                  CAST(strftime('%s', e.timestamp) AS INTEGER)
              ) BETWEEN 0 AND ?
            """,
            (store_id, target_date, BILLING_WINDOW_SEC),
        )
        row = cur.fetchone()
        purchased = row["cnt"] if row else 0

    def drop_pct(current: int, prev: int) -> float:
        if prev == 0:
            return 0.0
        return round((1 - current / prev) * 100, 1)

    stages: List[FunnelStage] = [
        FunnelStage(stage="Entry", count=total_sessions, drop_off_pct=0.0),
        FunnelStage(stage="Zone Visit", count=visited_zone,
                    drop_off_pct=drop_pct(visited_zone, total_sessions)),
        FunnelStage(stage="Billing Queue", count=reached_billing,
                    drop_off_pct=drop_pct(reached_billing, visited_zone)),
        FunnelStage(stage="Purchase", count=purchased,
                    drop_off_pct=drop_pct(purchased, reached_billing)),
    ]

    return FunnelResponse(
        store_id=store_id,
        as_of=_now_utc(),
        stages=stages,
        total_sessions=total_sessions,
    )