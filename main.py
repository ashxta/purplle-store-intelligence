# main.py
"""
main.py — FastAPI entrypoint for the Store Intelligence API.

Endpoints:
  POST /events/ingest
  GET  /stores/{store_id}/metrics
  GET  /stores/{store_id}/funnel
  GET  /stores/{store_id}/heatmap
  GET  /stores/{store_id}/anomalies
  GET  /health
"""
from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ── Local imports ─────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

from anomalies import get_anomalies
from database import init_db
from funnel import get_funnel
from health import get_health
from heatmap import get_heatmap
from ingestion import ingest_events, load_pos_transactions
from metrics import get_metrics
from models import (
    AnomaliesResponse,
    FunnelResponse,
    HealthResponse,
    HeatmapResponse,
    IngestRequest,
    IngestResponse,
    MetricsResponse,
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("store_intelligence")


# ── App lifecycle ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Load POS transactions if CSV present
    pos_csv = os.getenv("POS_CSV_PATH", "/data/pos_transactions.csv")
    if os.path.exists(pos_csv):
        n = load_pos_transactions(pos_csv)
        logger.info(f'"Loaded {n} POS transactions from {pos_csv}"')
    yield


app = FastAPI(
    title="Store Intelligence API",
    version="1.0.0",
    description="Purplle offline store analytics — from CCTV to conversion metrics",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Structured request logging middleware ──────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    trace_id = str(uuid.uuid4())[:8]
    start = time.perf_counter()
    request.state.trace_id = trace_id

    response = await call_next(request)

    latency_ms = round((time.perf_counter() - start) * 1000, 1)
    store_id = request.path_params.get("store_id", "-")
    logger.info(
        f'"trace_id":"{trace_id}","store_id":"{store_id}",'
        f'"method":"{request.method}","path":"{request.url.path}",'
        f'"status":{response.status_code},"latency_ms":{latency_ms}'
    )
    response.headers["X-Trace-Id"] = trace_id
    return response


# ── Error handlers ─────────────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    logger.error(f'"error":"{type(exc).__name__}: {exc}"')
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "detail": str(exc)},
    )


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.post("/events/ingest", response_model=IngestResponse, status_code=207)
async def ingest(payload: IngestRequest, request: Request):
    """
    Ingest a batch of up to 500 structured detection events.
    Idempotent by event_id. Returns partial success on malformed events.
    """
    if len(payload.events) > 500:
        raise HTTPException(422, "Batch size exceeds 500 events")
    result = ingest_events(payload.events)
    # 207 Multi-Status: allows partial success
    status = 207 if result.rejected > 0 else 200
    return Response(
        content=result.model_dump_json(),
        status_code=status,
        media_type="application/json",
    )


@app.get("/stores/{store_id}/metrics", response_model=MetricsResponse)
async def metrics(
    store_id: str,
    date: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to today"),
):
    """
    Real-time store metrics: unique visitors, conversion rate, zone dwell, queue depth.
    Staff events are excluded. Zero-purchase stores return conversion_rate=0.
    """
    try:
        return get_metrics(store_id, date=date)
    except Exception as e:
        raise HTTPException(503, f"Database error: {e}")


@app.get("/stores/{store_id}/funnel", response_model=FunnelResponse)
async def funnel(
    store_id: str,
    date: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to today"),
):
    """
    Session-level conversion funnel: Entry → Zone Visit → Billing Queue → Purchase.
    Re-entries do not double-count a visitor session.
    """
    try:
        return get_funnel(store_id, date=date)
    except Exception as e:
        raise HTTPException(503, f"Database error: {e}")


@app.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse)
async def heatmap(
    store_id: str,
    date: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to today"),
):
    """
    Zone visit frequency + average dwell time, normalised 0–100.
    Includes data_confidence flag when session count is low (<20).
    """
    try:
        return get_heatmap(store_id, date=date)
    except Exception as e:
        raise HTTPException(503, f"Database error: {e}")


@app.get("/stores/{store_id}/anomalies")
async def anomalies(
    store_id: str,
    date: Optional[str] = Query(None)
):
    """
    Active operational anomalies: queue spikes, conversion drops, dead zones.
    Severity: INFO / WARN / CRITICAL, with suggested_action for each.
    """
    try:
        
        return get_anomalies(store_id, date=date)
    except Exception as e:
        raise HTTPException(503, f"Database error: {e}")


@app.get("/health", response_model=HealthResponse)
async def health():
    """Service health, last event timestamp per store, STALE_FEED warning if >10 min lag."""
    return get_health()


# ── Dev entrypoint ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)