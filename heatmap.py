# heatmap.py
"""
heatmap.py — Zone visit frequency heatmap normalised to 0–100.
"""
from __future__ import annotations

from datetime import datetime, timezone

from database import db_cursor
from models import HeatmapResponse, HeatmapZone


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

LOW_CONFIDENCE_SESSION_THRESHOLD = 20


def get_heatmap(store_id: str, date: str | None = None) -> HeatmapResponse:
    target_date = date or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT zone_id,
                   COUNT(DISTINCT visitor_id) as visit_count,
                   AVG(dwell_ms)              as avg_dwell
            FROM events
            WHERE store_id = ?
              AND is_staff = 0
              AND event_type IN ('ZONE_EXIT', 'ZONE_DWELL', 'ZONE_ENTER')
              AND zone_id IS NOT NULL
              AND zone_id != 'ENTRY_EXIT'
              AND DATE(timestamp) = ?
            GROUP BY zone_id
            ORDER BY visit_count DESC
            """,
            (store_id, target_date),
        )
        rows = cur.fetchall()

        # Unique sessions for confidence check
        cur.execute(
            """
            SELECT COUNT(DISTINCT visitor_id) as sessions
            FROM events
            WHERE store_id = ?
              AND is_staff = 0
              AND event_type = 'ENTRY'
              AND DATE(timestamp) = ?
            """,
            (store_id, target_date),
        )
        sess_row = cur.fetchone()
        total_sessions = sess_row["sessions"] if sess_row else 0

    if not rows:
        return HeatmapResponse(
            store_id=store_id,
            as_of=_now_utc(),
            zones=[],
            data_confidence="low",
        )

    max_visits = max(r["visit_count"] for r in rows) or 1
    zones = [
        HeatmapZone(
            zone_id=r["zone_id"],
            visit_frequency_normalised=round(r["visit_count"] / max_visits * 100, 1),
            avg_dwell_ms=round(r["avg_dwell"] or 0, 1),
            visit_count=r["visit_count"],
        )
        for r in rows
    ]

    confidence = "low" if total_sessions < LOW_CONFIDENCE_SESSION_THRESHOLD else "high"
    return HeatmapResponse(
        store_id=store_id,
        as_of=_now_utc(),
        zones=zones,
        data_confidence=confidence,
    )