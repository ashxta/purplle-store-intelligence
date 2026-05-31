# models.py
"""
models.py — Pydantic schemas for event ingestion and API responses.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# ── Event types ───────────────────────────────────────────────────────────────
class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0


class StoreEvent(BaseModel):
    event_id: str
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: EventType
    timestamp: str   # ISO-8601 UTC
    zone_id: Optional[str] = None
    dwell_ms: int = 0
    is_staff: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("confidence")
    @classmethod
    def round_confidence(cls, v: float) -> float:
        return round(v, 4)


class IngestRequest(BaseModel):
    events: List[StoreEvent]


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    duplicate: int
    errors: List[Dict[str, Any]] = []


# ── Metrics response ──────────────────────────────────────────────────────────
class ZoneDwellMetric(BaseModel):
    zone_id: str
    avg_dwell_ms: float
    visit_count: int


class MetricsResponse(BaseModel):
    store_id: str
    as_of: str
    unique_visitors: int
    conversion_rate: float
    avg_dwell_per_zone: List[ZoneDwellMetric]
    current_queue_depth: int
    abandonment_rate: float
    total_transactions: int
    total_revenue_inr: float


# ── Funnel response ───────────────────────────────────────────────────────────
class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float


class FunnelResponse(BaseModel):
    store_id: str
    as_of: str
    stages: List[FunnelStage]
    total_sessions: int


# ── Heatmap response ──────────────────────────────────────────────────────────
class HeatmapZone(BaseModel):
    zone_id: str
    visit_frequency_normalised: float   # 0–100
    avg_dwell_ms: float
    visit_count: int


class HeatmapResponse(BaseModel):
    store_id: str
    as_of: str
    zones: List[HeatmapZone]
    data_confidence: str  # "high" / "low"


# ── Anomaly response ──────────────────────────────────────────────────────────
class AnomalySeverity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class AnomalyType(str, Enum):
    BILLING_QUEUE_SPIKE = "BILLING_QUEUE_SPIKE"
    CONVERSION_DROP = "CONVERSION_DROP"
    DEAD_ZONE = "DEAD_ZONE"
    STALE_FEED = "STALE_FEED"


class Anomaly(BaseModel):
    anomaly_type: AnomalyType
    severity: AnomalySeverity
    description: str
    suggested_action: str
    detected_at: str
    metadata: Dict[str, Any] = {}


class AnomaliesResponse(BaseModel):
    store_id: str
    as_of: str
    anomalies: List[Anomaly]


# ── Health response ───────────────────────────────────────────────────────────
class StoreFeedStatus(BaseModel):
    store_id: str
    last_event_timestamp: Optional[str]
    lag_seconds: Optional[float]
    status: str  # "OK" / "STALE_FEED" / "NO_DATA"


class HealthResponse(BaseModel):
    status: str
    version: str
    uptime_seconds: float
    database: str
    stores: List[StoreFeedStatus]