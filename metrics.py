# metrics.py
"""
metrics.py — Real-time store metric computation.

Conversion logic: a visitor session is "converted" if the visitor_id
had a ZONE_ENTER/ZONE_DWELL/BILLING_QUEUE_JOIN event in the BILLING zone
within the 5-minute window before any POS transaction timestamp.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

from database import db_cursor
from models import MetricsResponse, ZoneDwellMetric

logger = logging.getLogger(__name__)

BILLING_WINDOW_SEC = 300  # 5 minutes


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_metrics(store_id: str, date: str | None = None) -> MetricsResponse:
    """
    Compute today's (or a given date's) store metrics.
    All staff events (is_staff=1) are excluded.
    """
    target_date = date or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    with db_cursor() as cur:
        # ── Unique customer visitors (entry events, excluding staff) ──────────
        cur.execute(
            """
            SELECT COUNT(DISTINCT visitor_id) as cnt
            FROM events
            WHERE store_id = ?
              AND is_staff = 0
              AND event_type IN ('ENTRY', 'REENTRY')
              AND DATE(timestamp) = ?
            """,
            (store_id, target_date),
        )
        row = cur.fetchone()
        unique_visitors = row["cnt"] if row else 0

        # ── Average dwell per zone ─────────────────────────────────────────────
        cur.execute(
            """
            SELECT zone_id,
                   AVG(dwell_ms) as avg_dwell,
                   COUNT(*)      as visit_count
            FROM events
            WHERE store_id = ?
              AND is_staff = 0
              AND event_type IN ('ZONE_EXIT', 'ZONE_DWELL')
              AND zone_id IS NOT NULL
              AND DATE(timestamp) = ?
            GROUP BY zone_id
            """,
            (store_id, target_date),
        )
        zone_rows = cur.fetchall()
        avg_dwell_per_zone: List[ZoneDwellMetric] = [
            ZoneDwellMetric(
                zone_id=r["zone_id"],
                avg_dwell_ms=round(r["avg_dwell"] or 0, 1),
                visit_count=r["visit_count"],
            )
            for r in zone_rows
        ]

        # ── Current queue depth (BILLING zone live occupancy) ─────────────────
        cur.execute(
            """
            SELECT
              (SELECT COUNT(DISTINCT visitor_id) FROM events
               WHERE store_id = ? AND is_staff = 0
                 AND event_type IN ('BILLING_QUEUE_JOIN', 'ZONE_ENTER')
                 AND zone_id = 'BILLING'
                 AND DATE(timestamp) = ?) -
              (SELECT COUNT(DISTINCT visitor_id) FROM events
               WHERE store_id = ? AND is_staff = 0
                 AND event_type = 'ZONE_EXIT'
                 AND zone_id = 'BILLING'
                 AND DATE(timestamp) = ?) as depth
            """,
            (store_id, target_date, store_id, target_date),
        )
        row = cur.fetchone()
        queue_depth = max(0, row["depth"] or 0)

        # ── Abandonment rate ───────────────────────────────────────────────────
        cur.execute(
            """
            SELECT COUNT(DISTINCT visitor_id) as abandoned
            FROM events
            WHERE store_id = ?
              AND is_staff = 0
              AND event_type = 'BILLING_QUEUE_ABANDON'
              AND DATE(timestamp) = ?
            """,
            (store_id, target_date),
        )
        row = cur.fetchone()
        abandoned = row["abandoned"] if row else 0

        cur.execute(
            """
            SELECT COUNT(DISTINCT visitor_id) as joined
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
        joined_billing = row["joined"] if row else 0
        abandonment_rate = round(abandoned / max(joined_billing, 1), 4)

        # ── POS transactions ───────────────────────────────────────────────────
        cur.execute(
            """
            SELECT COUNT(*) as cnt, COALESCE(SUM(basket_value_inr), 0) as revenue
            FROM pos_transactions
            WHERE store_id = ?
              AND DATE(timestamp) = ?
            """,
            (store_id, target_date),
        )
        row = cur.fetchone()
        total_transactions = row["cnt"] if row else 0
        total_revenue = round(row["revenue"] if row else 0, 2)

        # ── Conversion rate ────────────────────────────────────────────────────
        # Visitors who had a billing zone event in the 5-min window before any txn
        cur.execute(
            """
            SELECT DISTINCT e.visitor_id
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
        converted_visitors = len(cur.fetchall())
        conversion_rate = round(converted_visitors / max(unique_visitors, 1), 4)

    return MetricsResponse(
        store_id=store_id,
        as_of=_now_utc(),
        unique_visitors=unique_visitors,
        conversion_rate=conversion_rate,
        avg_dwell_per_zone=avg_dwell_per_zone,
        current_queue_depth=queue_depth,
        abandonment_rate=abandonment_rate,
        total_transactions=total_transactions,
        total_revenue_inr=total_revenue,
    )