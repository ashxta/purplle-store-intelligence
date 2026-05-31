# anomalies.py
"""
anomalies.py — Real-time operational anomaly detection.

Detects:
  BILLING_QUEUE_SPIKE  — queue depth > threshold or abandonment rate spike
  CONVERSION_DROP      — today's conversion rate < 7-day rolling average * threshold
  DEAD_ZONE            — a zone has had no visits in the past 30 minutes
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List

from database import db_cursor
from metrics import get_metrics
from models import Anomaly, AnomaliesResponse, AnomalySeverity, AnomalyType

logger = logging.getLogger(__name__)

QUEUE_SPIKE_WARN = 5
QUEUE_SPIKE_CRITICAL = 10
CONVERSION_DROP_THRESHOLD = 0.25   # 25% drop vs 7-day avg → WARN
CONVERSION_DROP_CRITICAL = 0.40    # 40% drop → CRITICAL
DEAD_ZONE_MINUTES = 30
STALE_FEED_WARN_SECONDS = 600      # 10 minutes


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _detect_queue_spike(store_id: str, target_date: str, detected_at: str) -> List[Anomaly]:
    anomalies = []
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
              (SELECT COUNT(DISTINCT visitor_id) FROM events
               WHERE store_id=? AND is_staff=0
                 AND event_type IN ('BILLING_QUEUE_JOIN','ZONE_ENTER')
                 AND zone_id='BILLING' AND DATE(timestamp)=?) -
              (SELECT COUNT(DISTINCT visitor_id) FROM events
               WHERE store_id=? AND is_staff=0
                 AND event_type='ZONE_EXIT'
                 AND zone_id='BILLING' AND DATE(timestamp)=?) AS depth
            """,
            (store_id, target_date, store_id, target_date),
        )
        row = cur.fetchone()
        depth = max(0, row["depth"] or 0)

    if depth >= QUEUE_SPIKE_CRITICAL:
        anomalies.append(Anomaly(
            anomaly_type=AnomalyType.BILLING_QUEUE_SPIKE,
            severity=AnomalySeverity.CRITICAL,
            description=f"Billing queue depth is {depth} (critical threshold: {QUEUE_SPIKE_CRITICAL})",
            suggested_action="Open additional billing counter immediately. Consider floor staff redirection.",
            detected_at=detected_at,
            metadata={"queue_depth": depth},
        ))
    elif depth >= QUEUE_SPIKE_WARN:
        anomalies.append(Anomaly(
            anomaly_type=AnomalyType.BILLING_QUEUE_SPIKE,
            severity=AnomalySeverity.WARN,
            description=f"Billing queue depth is {depth} (warn threshold: {QUEUE_SPIKE_WARN})",
            suggested_action="Monitor billing counter. Prepare to open second counter.",
            detected_at=detected_at,
            metadata={"queue_depth": depth},
        ))
    return anomalies


def _detect_conversion_drop(store_id: str, target_date: str, detected_at: str) -> List[Anomaly]:
    anomalies = []
    today_metrics = get_metrics(store_id, date=target_date)
    today_rate = today_metrics.conversion_rate

    # Compute 7-day rolling average (excluding today)
    rates = []
    today_dt = datetime.strptime(target_date, "%Y-%m-%d")
    for i in range(1, 8):
        past_date = (today_dt - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            past_metrics = get_metrics(store_id, date=past_date)
            if past_metrics.unique_visitors > 0:
                rates.append(past_metrics.conversion_rate)
        except Exception:
            pass

    if not rates:
        return anomalies  # No historical data yet

    avg_rate = sum(rates) / len(rates)
    if avg_rate == 0:
        return anomalies

    drop = (avg_rate - today_rate) / avg_rate

    if drop >= CONVERSION_DROP_CRITICAL:
        anomalies.append(Anomaly(
            anomaly_type=AnomalyType.CONVERSION_DROP,
            severity=AnomalySeverity.CRITICAL,
            description=f"Conversion rate {today_rate:.1%} is {drop:.0%} below 7-day avg {avg_rate:.1%}",
            suggested_action="Investigate floor staff coverage and product availability. Check billing wait times.",
            detected_at=detected_at,
            metadata={"today_rate": today_rate, "seven_day_avg": round(avg_rate, 4), "drop_pct": round(drop, 4)},
        ))
    elif drop >= CONVERSION_DROP_THRESHOLD:
        anomalies.append(Anomaly(
            anomaly_type=AnomalyType.CONVERSION_DROP,
            severity=AnomalySeverity.WARN,
            description=f"Conversion rate {today_rate:.1%} is {drop:.0%} below 7-day avg {avg_rate:.1%}",
            suggested_action="Review customer journey. Consider targeted promotions or staff assistance at key zones.",
            detected_at=detected_at,
            metadata={"today_rate": today_rate, "seven_day_avg": round(avg_rate, 4), "drop_pct": round(drop, 4)},
        ))
    return anomalies


def _detect_dead_zones(store_id: str, target_date: str, detected_at: str) -> List[Anomaly]:
    anomalies = []
    cutoff = (_now_utc() - timedelta(minutes=DEAD_ZONE_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with db_cursor() as cur:
        # Zones that had visits today but none in last 30 minutes
        cur.execute(
            """
            SELECT DISTINCT zone_id FROM events
            WHERE store_id=? AND is_staff=0
              AND DATE(timestamp)=?
              AND zone_id IS NOT NULL
              AND zone_id NOT IN ('ENTRY_EXIT','BILLING')
            """,
            (store_id, target_date),
        )
        all_zones = {r["zone_id"] for r in cur.fetchall()}

        cur.execute(
            """
            SELECT DISTINCT zone_id FROM events
            WHERE store_id=? AND is_staff=0
              AND timestamp >= ?
              AND zone_id IS NOT NULL
              AND zone_id NOT IN ('ENTRY_EXIT','BILLING')
            """,
            (store_id, cutoff),
        )
        recent_zones = {r["zone_id"] for r in cur.fetchall()}

    dead_zones = all_zones - recent_zones
    for zone_id in sorted(dead_zones):
        anomalies.append(Anomaly(
            anomaly_type=AnomalyType.DEAD_ZONE,
            severity=AnomalySeverity.INFO,
            description=f"Zone '{zone_id}' has had no customer visits in the past {DEAD_ZONE_MINUTES} minutes",
            suggested_action=f"Check if zone '{zone_id}' display is engaging. Consider repositioning high-visibility products.",
            detected_at=detected_at,
            metadata={"zone_id": zone_id, "dead_minutes": DEAD_ZONE_MINUTES},
        ))
    return anomalies


def get_anomalies(store_id: str, date: str | None = None) -> AnomaliesResponse:
    now = _now_utc()
    detected_at = _fmt(now)
    
    target_date = date or now.strftime("%Y-%m-%d")

    all_anomalies: List[Anomaly] = []
    all_anomalies += _detect_queue_spike(store_id, target_date, detected_at)
    all_anomalies += _detect_conversion_drop(store_id, target_date, detected_at)
    all_anomalies += _detect_dead_zones(store_id, target_date, detected_at)

    # Sort: CRITICAL first, then WARN, then INFO
    severity_order = {AnomalySeverity.CRITICAL: 0, AnomalySeverity.WARN: 1, AnomalySeverity.INFO: 2}
    all_anomalies.sort(key=lambda a: severity_order.get(a.severity, 9))

    return AnomaliesResponse(
        store_id=store_id,
        as_of=detected_at,
        anomalies=all_anomalies,
    )