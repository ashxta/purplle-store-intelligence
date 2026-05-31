# health.py
"""
health.py — Service health and feed staleness checks.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from database import db_cursor
from models import HealthResponse, StoreFeedStatus

_start_time = time.time()
VERSION = "1.0.0"
STALE_FEED_THRESHOLD_SEC = 600   # 10 minutes


def get_health() -> HealthResponse:
    now_ts = time.time()
    now_dt = datetime.now(tz=timezone.utc)

    # DB check
    db_status = "ok"
    stores: list[StoreFeedStatus] = []
    try:
        with db_cursor() as cur:
            cur.execute("SELECT store_id, last_event_ts FROM store_feed_status")
            rows = cur.fetchall()

        for row in rows:
            last_ts_str = row["last_event_ts"]
            lag_seconds = None
            status = "NO_DATA"
            if last_ts_str:
                last_dt = datetime.strptime(last_ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
                lag_seconds = round((now_dt - last_dt).total_seconds(), 1)
                status = "STALE_FEED" if lag_seconds > STALE_FEED_THRESHOLD_SEC else "OK"

            stores.append(StoreFeedStatus(
                store_id=row["store_id"],
                last_event_timestamp=last_ts_str,
                lag_seconds=lag_seconds,
                status=status,
            ))
    except Exception as e:
        db_status = f"error: {e}"

    return HealthResponse(
        status="ok" if db_status == "ok" else "degraded",
        version=VERSION,
        uptime_seconds=round(now_ts - _start_time, 1),
        database=db_status,
        stores=stores,
    )