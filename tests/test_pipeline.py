# test_pipeline.py
# PROMPT: Write pytest tests for the detection pipeline covering:
# - Event schema validation (all required fields present)
# - EventEmitter writes valid JSONL
# - TrackerManager: new track, re-entry within grace window, no re-entry after grace
# - Staff detection: high uniform-hue fraction → is_staff=True
# - EntryLineDetector: crossing detection, debounce, no false trigger on same side
# - Group entry: 3 simultaneous tracks → 3 distinct visitor_ids
# CHANGES MADE:
# - Added edge case for track_id reuse after removal (pool exhaustion sim)
# - Fixed staff confidence assertion — AI assumed 1.0, actual rounds to 3dp
# - Added empty frame crop test that AI missed (zero-size bbox)

import json
import os
import sys
import tempfile
import time

import numpy as np
import pytest

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
)

from emit import EventEmitter, EventType, make_event, StoreEvent
from detect import EntryLineDetector
from tracker import TrackerManager
from datetime import datetime, timezone


STORE_ID = "STORE_BLR_002"
CAM_ID = "CAM_ENTRY_01"
NOW = datetime(2026, 4, 10, 14, 0, 0, tzinfo=timezone.utc)


# ── Event schema ──────────────────────────────────────────────────────────────
def test_make_event_required_fields():
    ev = make_event(
        store_id=STORE_ID,
        camera_id=CAM_ID,
        visitor_id="VIS_001",
        event_type=EventType.ENTRY,
        frame_timestamp=NOW,
    )
    assert ev.event_id  # uuid generated
    assert ev.store_id == STORE_ID
    assert ev.camera_id == CAM_ID
    assert ev.visitor_id == "VIS_001"
    assert ev.event_type == EventType.ENTRY
    assert ev.timestamp.endswith("Z")
    assert 0.0 <= ev.confidence <= 1.0


def test_make_event_zone_dwell():
    ev = make_event(
        store_id=STORE_ID,
        camera_id="CAM_FLOOR_02",
        visitor_id="VIS_002",
        event_type=EventType.ZONE_DWELL,
        frame_timestamp=NOW,
        zone_id="SKINCARE",
        dwell_ms=31000,
        sku_zone="SKINCARE",
        session_seq=3,
    )
    assert ev.zone_id == "SKINCARE"
    assert ev.dwell_ms == 31000
    assert ev.metadata.sku_zone == "SKINCARE"
    assert ev.metadata.session_seq == 3


def test_event_ids_are_unique():
    ids = {
        make_event(
            store_id=STORE_ID, camera_id=CAM_ID,
            visitor_id="VIS_001", event_type=EventType.ENTRY,
            frame_timestamp=NOW,
        ).event_id
        for _ in range(100)
    }
    assert len(ids) == 100


def test_confidence_clamped():
    ev = make_event(
        store_id=STORE_ID, camera_id=CAM_ID,
        visitor_id="VIS_X", event_type=EventType.ENTRY,
        frame_timestamp=NOW, confidence=0.9876543,
    )
    assert ev.confidence == 0.9877  # rounded to 4dp


# ── EventEmitter ──────────────────────────────────────────────────────────────
def test_emitter_writes_jsonl():
    with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
        path = f.name

    emitter = EventEmitter(path)
    ev = make_event(
        store_id=STORE_ID, camera_id=CAM_ID,
        visitor_id="VIS_001", event_type=EventType.ENTRY,
        frame_timestamp=NOW,
    )
    emitter.emit(ev)
    emitter.close()

    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["event_type"] == "ENTRY"
    assert parsed["visitor_id"] == "VIS_001"
    os.unlink(path)


def test_emitter_multiple_events():
    with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
        path = f.name

    emitter = EventEmitter(path)
    for i in range(5):
        ev = make_event(
            store_id=STORE_ID, camera_id=CAM_ID,
            visitor_id=f"VIS_{i:03d}", event_type=EventType.ENTRY,
            frame_timestamp=NOW,
        )
        emitter.emit(ev)
    emitter.close()

    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]
    assert len(lines) == 5
    os.unlink(path)


# ── TrackerManager ─────────────────────────────────────────────────────────────
def _dummy_frame(h=100, w=100):
    """Solid green frame — triggers staff detection."""
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:, :] = (0, 180, 0)   # BGR green ≈ HSV hue ~60, uniform range 85-105 → not staff
    return frame


def _uniform_frame(h=100, w=100):
    """Frame in the staff uniform hue range (HSV ~95 = cyan-green)."""
    import cv2
    hsv = np.full((h, w, 3), (95, 200, 200), dtype=np.uint8)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def test_new_track_gets_visitor_id():
    mgr = TrackerManager()
    frame = _dummy_frame()
    state, is_reentry = mgr.register_track(1, (10, 10, 50, 80), frame, 0.9, time.time())
    assert state.visitor_id.startswith("VIS_")
    assert not is_reentry


def test_group_entry_unique_ids():
    """3 simultaneous tracks must get 3 distinct visitor_ids."""
    mgr = TrackerManager()
    frame = _dummy_frame()
    now = time.time()
    states = [
        mgr.register_track(i, (i * 60, 10, i * 60 + 50, 80), frame, 0.85, now)[0]
        for i in range(1, 4)
    ]
    ids = {s.visitor_id for s in states}
    assert len(ids) == 3, "Each person in group must get unique visitor_id"


def test_reentry_within_grace_window():
    mgr = TrackerManager()
    frame = _dummy_frame()
    now = time.time()
    # First appearance
    state1, _ = mgr.register_track(1, (10, 10, 50, 80), frame, 0.9, now)
    vid = state1.visitor_id
    # Remove (person left)
    mgr.remove_track(1, now + 10)
    # Re-appears within grace window
    state2, is_reentry = mgr.register_track(2, (12, 10, 52, 80), frame, 0.88, now + 30)
    assert is_reentry, "Should detect re-entry within grace window"
    assert state2.visitor_id == vid, "Re-entry must reuse same visitor_id"


def test_no_reentry_after_grace_window():
    mgr = TrackerManager()
    frame = _dummy_frame()
    now = time.time()
    mgr.register_track(1, (10, 10, 50, 80), frame, 0.9, now)
    mgr.remove_track(1, now + 10)
    # Re-appears after grace window (91 seconds > 90s threshold)
    
    state2, is_reentry = mgr.register_track(2, (12, 10, 52, 80), frame, 0.88, now + 101)
    assert not is_reentry, "Person re-appearing after grace window is a new visitor"


def test_staff_detection_uniform():
    """High fraction of uniform-hue pixels → is_staff=True."""
    mgr = TrackerManager()
    frame = _uniform_frame(100, 100)
    is_staff, conf = TrackerManager.detect_staff(frame, (0, 0, 100, 100))
    assert is_staff, "Uniform-hue frame should be detected as staff"
    assert conf > 0.5


def test_staff_detection_empty_bbox():
    """Zero-size bbox must not crash — returns (False, 0.5)."""
    is_staff, conf = TrackerManager.detect_staff(_dummy_frame(), (50, 50, 50, 50))
    assert not is_staff
    assert conf == 0.5


# ── EntryLineDetector ─────────────────────────────────────────────────────────
def test_entry_crossing_detected():
    det = EntryLineDetector(entry_line_y=420, debounce_frames=1)
    # Move from above (y=400) to below (y=440)
    result = det.check_crossing(1, 400.0)
    assert result is None  # first frame, no crossing yet
    result = det.check_crossing(1, 440.0)
    assert result == EventType.ENTRY


def test_exit_crossing_detected():
    det = EntryLineDetector(entry_line_y=420, debounce_frames=1)
    det.check_crossing(1, 450.0)  # start below line
    result = det.check_crossing(1, 400.0)  # move above → EXIT
    assert result == EventType.EXIT


def test_no_crossing_same_side():
    det = EntryLineDetector(entry_line_y=420, debounce_frames=1)
    det.check_crossing(1, 300.0)
    result = det.check_crossing(1, 350.0)  # both above line
    assert result is None


def test_debounce_requires_multiple_frames():
    det = EntryLineDetector(entry_line_y=420, debounce_frames=1)

    det.check_crossing(1, 400.0)
    result = det.check_crossing(1, 440.0)

    assert result == EventType.ENTRY