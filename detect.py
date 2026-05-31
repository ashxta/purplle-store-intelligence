# detect.py
"""
detect.py — Main CCTV detection pipeline.

Processes video clips using YOLOv8 + ByteTrack and emits structured events.
Handles all edge cases: group entry, staff exclusion, re-entry, occlusion.

Usage:
    python detect.py --video path/to/cam.mp4 \\
                     --camera-id CAM_ENTRY_01 \\
                     --store-id STORE_BLR_002 \\
                     --layout ../store_layout.json \\
                     --output events.jsonl \\
                     [--api-url http://localhost:8000]
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

# ── Try to import ultralytics; give clear error if missing ────────────────────
try:
    from ultralytics import YOLO
except ImportError:
    print("ERROR: Install ultralytics first: pip install ultralytics")
    sys.exit(1)

from emit import EventEmitter, EventType, make_event
from tracker import TrackerManager

# ── Zone geometry helpers ──────────────────────────────────────────────────────
def point_in_polygon(point: Tuple[float, float], polygon: List[List[int]]) -> bool:
    """Ray-casting polygon test."""
    x, y = point
    pts = np.array(polygon, dtype=np.float32)
    return cv2.pointPolygonTest(pts, (float(x), float(y)), False) >= 0


def load_layout(layout_path: str) -> dict:
    with open(layout_path) as f:
        return json.load(f)


def get_camera_zones(layout: dict, camera_id: str) -> List[dict]:
    """Return zones covered by this camera."""
    return [z for z in layout["zones"] if camera_id in z.get("camera_ids", [])]


def detect_zone(center: Tuple[float, float], zones: List[dict]) -> Optional[str]:
    """Return zone_id for the given centroid, or None."""
    for zone in zones:
        if point_in_polygon(center, zone["polygon"]):
            return zone["zone_id"]
    return None


def get_camera_type(layout: dict, camera_id: str) -> str:
    for cam in layout.get("cameras", []):
        if cam["camera_id"] == camera_id:
            return cam.get("type", "main_floor")
    return "main_floor"


# ── Entry/exit direction detection ────────────────────────────────────────────
class EntryLineDetector:
    """
    Tracks centroid crossing of the entry threshold line.
    Direction determined by comparing centroid Y across frames.
    ENTRY = centroid moves from above line (outside) to below (inside).
    EXIT  = centroid moves from below to above.
    """
    def __init__(self, entry_line_y: int, debounce_frames: int = 5):
        self.line_y = entry_line_y
        self.debounce = debounce_frames
        self._prev_y: Dict[int, float] = {}
        self._cross_frames: Dict[int, int] = {}

    def check_crossing(self, track_id: int, cy: float) -> Optional[str]:
        prev = self._prev_y.get(track_id)
        self._prev_y[track_id] = cy
        if prev is None:
            return None

        crossed = None
        if prev < self.line_y <= cy:
            crossed = EventType.ENTRY   # moving downward (into store)
        elif prev >= self.line_y > cy:
            crossed = EventType.EXIT    # moving upward (out of store)

        if crossed:
            # Debounce: require N consecutive crossing frames
            count = self._cross_frames.get(track_id, 0) + 1
            self._cross_frames[track_id] = count
            if count >= self.debounce:
                self._cross_frames[track_id] = 0
                return crossed
        else:
            self._cross_frames[track_id] = 0
        return None

    def cleanup(self, track_id: int) -> None:
        self._prev_y.pop(track_id, None)
        self._cross_frames.pop(track_id, None)


# ── Billing queue tracker ──────────────────────────────────────────────────────
class BillingQueueTracker:
    """Tracks concurrent occupancy in the billing zone."""
    def __init__(self, pos_transactions: List[dict], billing_window_sec: int = 300):
        self._in_billing: Dict[str, float] = {}  # visitor_id → enter_time
        self._billing_window = billing_window_sec
        # Index POS by timestamp for fast lookup
        
        self._pos_times = []

        for t in pos_transactions:
            try:
                dt = datetime.strptime(
                    f"{t['order_date']} {t['order_time']}",
                    "%d-%m-%Y %H:%M:%S"
        )
                self._pos_times.append(dt.timestamp())
            except Exception:
                continue

        self._pos_times.sort()

    @property
    def queue_depth(self) -> int:
        return len(self._in_billing)

    def visitor_entered(self, visitor_id: str, now: float) -> int:
        self._in_billing[visitor_id] = now
        return self.queue_depth

    def visitor_exited(self, visitor_id: str, now: float) -> Tuple[bool, float]:
        """Returns (was_abandoned, dwell_ms)."""
        enter_time = self._in_billing.pop(visitor_id, None)
        if enter_time is None:
            return False, 0
        dwell_ms = int((now - enter_time) * 1000)
        abandoned = not self._has_nearby_transaction(now)
        return abandoned, dwell_ms

    def _has_nearby_transaction(self, now: float) -> bool:
        """Was there a POS transaction in the billing window?"""
        import bisect
        lo = bisect.bisect_left(self._pos_times, now - self._billing_window)
        hi = bisect.bisect_right(self._pos_times, now + 60)
        return hi > lo


# ── Main processing loop ───────────────────────────────────────────────────────
def process_video(
    video_path: str,
    camera_id: str,
    store_id: str,
    layout: dict,
    emitter: EventEmitter,
    pos_transactions: List[dict],
    clip_start_dt: Optional[datetime] = None,
    max_frames: Optional[int] = None,
):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"ERROR: Cannot open {video_path}")
        return

    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    camera_type = get_camera_type(layout, camera_id)
    zones = get_camera_zones(layout, camera_id)

    print(f"[{camera_id}] {total_frames} frames @ {fps:.1f}fps — type: {camera_type}")
    print(f"[{camera_id}] Covering zones: {[z['zone_id'] for z in zones]}")

    # Model — YOLOv8n is fastest; use 's' or 'm' for better accuracy
    model = YOLO("yolov8n.pt")

    tracker_mgr = TrackerManager()
    entry_detector = None
    billing_tracker = BillingQueueTracker(pos_transactions)

    # Find entry line for entry cameras
    if camera_type == "entry_exit":
        for z in zones:
            if z.get("zone_id") == "ENTRY_EXIT" and "entry_line_y" in z:
                entry_detector = EntryLineDetector(z["entry_line_y"])
                break
        if not entry_detector:
            entry_detector = EntryLineDetector(entry_line_y=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) * 0.45))

    frame_num = 0
    # Process every 3rd frame to save time (effectively 5fps from 15fps)
    FRAME_SKIP = 3

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        frame_num += 1
        if max_frames and frame_num > max_frames:
            break
        if frame_num % FRAME_SKIP != 0:
            continue

        # Frame timestamp = clip start + offset
        offset_sec = frame_num / fps
        if clip_start_dt:
            frame_dt = clip_start_dt.replace(tzinfo=timezone.utc)
            frame_ts = datetime.fromtimestamp(
                frame_dt.timestamp() + offset_sec, tz=timezone.utc
            )
        else:
            frame_ts = datetime.now(tz=timezone.utc)

        now = frame_ts.timestamp()

        # Run YOLOv8 detection + ByteTrack
        results = model.track(
            frame,
            persist=True,
            classes=[0],       # class 0 = person
            conf=0.35,         # lower threshold; we flag low-conf, not drop
            iou=0.5,
            tracker="bytetrack.yaml",
            verbose=False,
        )

        active_track_ids = set()

        if results and results[0].boxes is not None:
            boxes = results[0].boxes
            for box in boxes:
                if box.id is None:
                    continue
                track_id = int(box.id.item())
                active_track_ids.add(track_id)
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                bbox = (x1, y1, x2, y2)
                conf = float(box.conf.item())
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2

                is_new = track_id not in tracker_mgr._active

                if is_new:
                    state, is_reentry = tracker_mgr.register_track(
                        track_id, bbox, frame, conf, now
                    )

                    if is_reentry:
                        ev = make_event(
                            store_id=store_id,
                            camera_id=camera_id,
                            visitor_id=state.visitor_id,
                            event_type=EventType.REENTRY,
                            frame_timestamp=frame_ts,
                            is_staff=state.is_staff,
                            confidence=conf,
                            session_seq=state.session_seq,
                        )
                        emitter.emit(ev)

                    if camera_type == "entry_exit" and entry_detector:
                        direction = entry_detector.check_crossing(track_id, cy)
                        if direction:
                            ev = make_event(
                                store_id=store_id,
                                camera_id=camera_id,
                                visitor_id=state.visitor_id,
                                event_type=direction,
                                frame_timestamp=frame_ts,
                                is_staff=state.is_staff,
                                confidence=conf,
                                session_seq=state.session_seq,
                            )
                            emitter.emit(ev)
                else:
                    state = tracker_mgr.update_track(track_id, bbox, conf, now)

                if state is None:
                    continue

                # Entry crossing (ongoing)
                if camera_type == "entry_exit" and entry_detector:
                    direction = entry_detector.check_crossing(track_id, cy)
                    if direction:
                        ev = make_event(
                            store_id=store_id,
                            camera_id=camera_id,
                            visitor_id=state.visitor_id,
                            event_type=direction,
                            frame_timestamp=frame_ts,
                            is_staff=state.is_staff,
                            confidence=conf,
                            session_seq=state.session_seq,
                        )
                        emitter.emit(ev)

                # Zone detection (non-entry cameras)
                if camera_type != "entry_exit" and zones:
                    new_zone = detect_zone((cx, cy), zones)
                    if new_zone != state.current_zone:
                        if state.current_zone:
                            result = tracker_mgr.exit_zone(track_id, now)
                            if result:
                                old_zone, dwell_ms = result
                                # Billing abandon check
                                if old_zone == "BILLING":
                                    abandoned, _ = billing_tracker.visitor_exited(
                                        state.visitor_id, now
                                    )
                                    if abandoned and not state.is_staff:
                                        ev = make_event(
                                            store_id=store_id,
                                            camera_id=camera_id,
                                            visitor_id=state.visitor_id,
                                            event_type=EventType.BILLING_QUEUE_ABANDON,
                                            frame_timestamp=frame_ts,
                                            zone_id=old_zone,
                                            dwell_ms=dwell_ms,
                                            is_staff=state.is_staff,
                                            confidence=conf,
                                            session_seq=state.session_seq,
                                        )
                                        emitter.emit(ev)
                                ev = make_event(
                                    store_id=store_id,
                                    camera_id=camera_id,
                                    visitor_id=state.visitor_id,
                                    event_type=EventType.ZONE_EXIT,
                                    frame_timestamp=frame_ts,
                                    zone_id=old_zone,
                                    dwell_ms=dwell_ms,
                                    is_staff=state.is_staff,
                                    confidence=conf,
                                    session_seq=state.session_seq,
                                )
                                emitter.emit(ev)

                        if new_zone:
                            tracker_mgr.enter_zone(track_id, new_zone, now)
                            qd = None
                            if new_zone == "BILLING":
                                qd = billing_tracker.visitor_entered(state.visitor_id, now)
                                ev_type = EventType.BILLING_QUEUE_JOIN if qd > 1 else EventType.ZONE_ENTER
                            else:
                                ev_type = EventType.ZONE_ENTER

                            sku_zone = next(
                                (z.get("sku_zone") for z in zones if z["zone_id"] == new_zone), None
                            )
                            ev = make_event(
                                store_id=store_id,
                                camera_id=camera_id,
                                visitor_id=state.visitor_id,
                                event_type=ev_type,
                                frame_timestamp=frame_ts,
                                zone_id=new_zone,
                                is_staff=state.is_staff,
                                confidence=conf,
                                queue_depth=qd,
                                sku_zone=sku_zone,
                                session_seq=state.session_seq,
                            )
                            emitter.emit(ev)

                    # Periodic ZONE_DWELL emit
                    elif state.current_zone and tracker_mgr.should_emit_dwell(track_id, now):
                        dwell_ms = int((now - (state.zone_enter_time or now)) * 1000)
                        ev = make_event(
                            store_id=store_id,
                            camera_id=camera_id,
                            visitor_id=state.visitor_id,
                            event_type=EventType.ZONE_DWELL,
                            frame_timestamp=frame_ts,
                            zone_id=state.current_zone,
                            dwell_ms=dwell_ms,
                            is_staff=state.is_staff,
                            confidence=conf,
                            session_seq=state.session_seq,
                        )
                        emitter.emit(ev)

        # Handle disappeared tracks
        prev_ids = set(tracker_mgr._active.keys())
        disappeared = prev_ids - active_track_ids
        for track_id in disappeared:
            state = tracker_mgr.remove_track(track_id, now)
            if state and camera_type == "entry_exit" and entry_detector:
                entry_detector.cleanup(track_id)

        if frame_num % (fps * 60 * FRAME_SKIP) == 0:
            mins = int(offset_sec / 60)
            print(f"[{camera_id}] Processed {mins}m, active tracks: {len(active_track_ids)}")

    cap.release()
    print(f"[{camera_id}] Done. Total frames: {frame_num}")


def load_pos_transactions(pos_path: str) -> List[dict]:
    import csv
    txns = []
    try:
        with open(pos_path) as f:
            for row in csv.DictReader(f):
                txns.append(row)
    except FileNotFoundError:
        print(f"[WARN] POS file not found: {pos_path} — billing correlation disabled")
    return txns


def infer_clip_start(video_path: str) -> Optional[datetime]:
    """Try to parse clip start time from filename. Falls back to None."""
    import re
    name = Path(video_path).stem
    m = re.search(r"(\d{4}-\d{2}-\d{2})[T_](\d{2}[:\-]\d{2})", name)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)}T{m.group(2).replace('-', ':')}", "%Y-%m-%dT%H:%M")
        except ValueError:
            pass
    # Default: April 10 2026 10:00 (store open)
    return datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)


def main():
    parser = argparse.ArgumentParser(description="Store CCTV detection pipeline")
    parser.add_argument("--video", required=True)
    parser.add_argument("--camera-id", required=True)
    parser.add_argument("--store-id", default="STORE_BLR_002")
    parser.add_argument("--layout", default="../store_layout.json")
    parser.add_argument("--pos", default="../pos_transactions.csv")
    parser.add_argument("--output", default="events.jsonl")
    parser.add_argument("--api-url", default=None)
    parser.add_argument("--max-frames", type=int, default=None)
    args = parser.parse_args()

    layout = load_layout(args.layout)
    pos_transactions = load_pos_transactions(args.pos)
    clip_start = infer_clip_start(args.video)

    print(f"Processing: {args.video}")
    print(f"Camera: {args.camera_id}, Store: {args.store_id}")
    print(f"Clip start time: {clip_start}")
    print(f"POS transactions loaded: {len(pos_transactions)}")

    emitter = EventEmitter(args.output, api_url=args.api_url)
    try:
        process_video(
            video_path=args.video,
            camera_id=args.camera_id,
            store_id=args.store_id,
            layout=layout,
            emitter=emitter,
            pos_transactions=pos_transactions,
            clip_start_dt=clip_start,
            max_frames=args.max_frames,
        )
    finally:
        emitter.close()
    print(f"Events written to: {args.output}")


if __name__ == "__main__":
    main()