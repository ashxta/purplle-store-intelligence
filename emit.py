"""
emit.py — Event schema definition and emission helpers.
All events emitted by the detection pipeline must use these functions.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ─────────────────────────────────────────────────────────────
# Event Types
# ─────────────────────────────────────────────────────────────

class EventType:
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


# ─────────────────────────────────────────────────────────────
# Metadata
# ─────────────────────────────────────────────────────────────

@dataclass
class EventMetadata:
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0


# ─────────────────────────────────────────────────────────────
# Event Object
# ─────────────────────────────────────────────────────────────

@dataclass
class StoreEvent:
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: str
    timestamp: str
    zone_id: Optional[str]
    dwell_ms: int
    is_staff: bool
    confidence: float
    metadata: EventMetadata
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


# ─────────────────────────────────────────────────────────────
# Event Factory
# ─────────────────────────────────────────────────────────────

def make_event(
    *,
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    frame_timestamp: datetime,
    zone_id: Optional[str] = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 1.0,
    queue_depth: Optional[int] = None,
    sku_zone: Optional[str] = None,
    session_seq: int = 0,
) -> StoreEvent:

    return StoreEvent(
        store_id=store_id,
        camera_id=camera_id,
        visitor_id=visitor_id,
        event_type=event_type,
        timestamp=frame_timestamp.astimezone(
            timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        zone_id=zone_id,
        dwell_ms=int(dwell_ms),
        is_staff=bool(is_staff),
        confidence=float(confidence),
        metadata=EventMetadata(
            queue_depth=queue_depth,
            sku_zone=sku_zone,
            session_seq=session_seq,
        ),
    )


# ─────────────────────────────────────────────────────────────
# JSON Helper
# ─────────────────────────────────────────────────────────────

def _json_converter(obj):
    """
    Converts numpy types and other non-serializable
    objects into standard Python types.
    """

    try:
        import numpy as np

        if isinstance(obj, np.bool_):
            return bool(obj)

        if isinstance(obj, np.integer):
            return int(obj)

        if isinstance(obj, np.floating):
            return float(obj)

        if isinstance(obj, np.ndarray):
            return obj.tolist()

    except Exception:
        pass

    return str(obj)


# ─────────────────────────────────────────────────────────────
# Event Emitter
# ─────────────────────────────────────────────────────────────

class EventEmitter:

    def __init__(
        self,
        output_path: str,
        api_url: Optional[str] = None,
    ):
        self.output_path = output_path
        self.api_url = api_url
        self._batch = []

        self._fh = open(
            output_path,
            "a",
            encoding="utf-8",
        )

    def emit(self, event: StoreEvent):

        d = event.to_dict()

        try:
            self._fh.write(
                json.dumps(
                    d,
                    default=_json_converter
                )
                + "\n"
            )

            self._fh.flush()

        except Exception as e:
            print("\n=== EVENT SERIALIZATION ERROR ===")
            print(type(e).__name__)
            print(e)
            print(d)
            raise

        self._batch.append(d)

        if len(self._batch) >= 100:
            self._flush_to_api()

    def _flush_to_api(self):

        if not self.api_url:
            return

        if not self._batch:
            return

        try:
            import requests

            response = requests.post(
                f"{self.api_url}/events/ingest",
                json={"events": self._batch},
                timeout=10,
            )

            if response.status_code not in (200, 207):
                print(
                    f"[WARN] API returned "
                    f"{response.status_code}"
                )

        except Exception as e:
            print(
                f"[WARN] Could not POST to API: {e}"
            )

        finally:
            self._batch.clear()

    def close(self):

        self._flush_to_api()

        if self._fh:
            self._fh.close()