# tracker.py
"""
tracker.py — Per-track state management, Re-ID, and session token assignment.

Design decisions:
- Re-ID uses appearance embedding cosine similarity when available (torchreid).
  Falls back to bounding-box IoU trajectory matching when no GPU/model available.
- Re-entry grace window: 90 seconds. A disappeared track whose embedding
  similarity to a re-appearing track exceeds RE_ID_THRESHOLD is considered
  the same visitor session → emits REENTRY instead of ENTRY.
- Staff detection: dominant HSV hue in bounding box crop checked against
  a configurable staff uniform hue range. Override via environment variable
  STAFF_UNIFORM_HUE_RANGE="160,200" (green aprons → adjust for your store).
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

# ── Configuration ──────────────────────────────────────────────────────────────
RE_ID_THRESHOLD = 0.72       # cosine similarity to consider same person
REENTRY_GRACE_SEC = 90       # seconds before a re-appearing person is "new"
DWELL_EMIT_INTERVAL_MS = 30_000  # emit ZONE_DWELL every 30 s of continuous dwell
STAFF_UNIFORM_HUE_RANGE = tuple(
    int(x) for x in os.getenv("STAFF_UNIFORM_HUE_RANGE", "85,105").split(",")
)  # HSV hue 85–105 ≈ green/teal uniform

# ── Track state ────────────────────────────────────────────────────────────────
@dataclass
class TrackState:
    track_id: int
    visitor_id: str
    first_seen: float           # epoch time
    last_seen: float
    last_bbox: Tuple[int, int, int, int]  # x1,y1,x2,y2
    current_zone: Optional[str] = None
    zone_enter_time: Optional[float] = None
    last_dwell_emit_time: Optional[float] = None
    is_staff: bool = False
    confidence: float = 1.0
    session_seq: int = 0
    embedding: Optional[np.ndarray] = None
    in_billing: bool = False
    billing_enter_time: Optional[float] = None

    def bbox_center(self) -> Tuple[float, float]:
        x1, y1, x2, y2 = self.last_bbox
        return ((x1 + x2) / 2, (y1 + y2) / 2)


@dataclass
class LostTrack:
    """Tracks that have exited or been lost — kept for Re-ID matching."""
    visitor_id: str
    lost_at: float
    embedding: Optional[np.ndarray]
    last_bbox: Tuple[int, int, int, int]


class TrackerManager:
    def __init__(self):
        self._active: Dict[int, TrackState] = {}      # track_id → state
        self._lost: List[LostTrack] = []              # recently lost tracks
        self._visitor_counter = 0
        self._reentry_map: Dict[str, int] = {}        # visitor_id → reentry count

    # ── Visitor ID allocation ──────────────────────────────────────────────────
    def _new_visitor_id(self) -> str:
        self._visitor_counter += 1
        return f"VIS_{self._visitor_counter:06d}"

    # ── Staff detection ────────────────────────────────────────────────────────
    @staticmethod
    def detect_staff(frame: np.ndarray, bbox: Tuple[int, int, int, int]) -> Tuple[bool, float]:
        """
        Dominant hue-based staff detection.
        Returns (is_staff, confidence).
        """
        x1, y1, x2, y2 = bbox
        h, w = frame.shape[:2]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return False, 0.5

        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        hue = hsv[:, :, 0].flatten()
        lo, hi = STAFF_UNIFORM_HUE_RANGE
        mask = (hue >= lo) & (hue <= hi)
        fraction = mask.sum() / max(len(hue), 1)

        # > 35% of pixels in uniform hue range → likely staff
        is_staff = fraction > 0.35
        confidence = min(1.0, fraction / 0.35) if is_staff else 1.0 - fraction
        return is_staff, round(confidence, 3)

    # ── Re-ID ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        a, b = a.flatten(), b.flatten()
        denom = (np.linalg.norm(a) * np.linalg.norm(b))
        if denom == 0:
            return 0.0
        return float(np.dot(a, b) / denom)

    def _bbox_iou(self, b1: Tuple, b2: Tuple) -> float:
        ax1, ay1, ax2, ay2 = b1
        bx1, by1, bx2, by2 = b2
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
        area_b = max(1, (bx2 - bx1) * (by2 - by1))
        return inter / (area_a + area_b - inter)

    def match_lost_track(
        self,
        bbox: Tuple[int, int, int, int],
        embedding: Optional[np.ndarray],
        now: float,
    ) -> Optional[LostTrack]:
        """Try to match a new detection against recently lost tracks."""
        candidates = [lt for lt in self._lost if (now - lt.lost_at) < REENTRY_GRACE_SEC]
        if not candidates:
            return None

        best, best_score = None, 0.0
        for lt in candidates:
            if embedding is not None and lt.embedding is not None:
                score = self._cosine_similarity(embedding, lt.embedding)
            else:
                # Fallback: IoU on last known bbox
                score = self._bbox_iou(bbox, lt.last_bbox)
            if score > best_score:
                best_score, best = score, lt

        if best_score >= RE_ID_THRESHOLD:
            return best
        return None

    # ── Public API ─────────────────────────────────────────────────────────────
    def register_track(
        self,
        track_id: int,
        bbox: Tuple[int, int, int, int],
        frame: np.ndarray,
        confidence: float,
        now: float,
        embedding: Optional[np.ndarray] = None,
    ) -> Tuple[TrackState, bool]:
        """
        Register a newly-appeared track. Returns (state, is_reentry).
        """
        is_staff, staff_conf = self.detect_staff(frame, bbox)

        matched = self.match_lost_track(bbox, embedding, now)
        if matched:
            # Re-entry
            visitor_id = matched.visitor_id
            self._lost = [lt for lt in self._lost if lt.visitor_id != visitor_id]
            self._reentry_map[visitor_id] = self._reentry_map.get(visitor_id, 0) + 1
            is_reentry = True
        else:
            visitor_id = self._new_visitor_id()
            is_reentry = False

        state = TrackState(
            track_id=track_id,
            visitor_id=visitor_id,
            first_seen=now,
            last_seen=now,
            last_bbox=bbox,
            is_staff=is_staff,
            confidence=round(confidence * (staff_conf if is_staff else 1.0), 3),
            embedding=embedding,
        )
        self._active[track_id] = state
        return state, is_reentry

    def update_track(
        self,
        track_id: int,
        bbox: Tuple[int, int, int, int],
        confidence: float,
        now: float,
        embedding: Optional[np.ndarray] = None,
    ) -> Optional[TrackState]:
        state = self._active.get(track_id)
        if state is None:
            return None
        state.last_bbox = bbox
        state.last_seen = now
        state.confidence = round(confidence, 3)
        if embedding is not None:
            # Rolling average embedding
            if state.embedding is not None:
                state.embedding = 0.7 * state.embedding + 0.3 * embedding
            else:
                state.embedding = embedding
        return state

    def remove_track(self, track_id: int, now: float) -> Optional[TrackState]:
        state = self._active.pop(track_id, None)
        if state:
            self._lost.append(LostTrack(
                visitor_id=state.visitor_id,
                lost_at=now,
                embedding=state.embedding,
                last_bbox=state.last_bbox,
            ))
            # Prune stale lost tracks
            self._lost = [lt for lt in self._lost if (now - lt.lost_at) < REENTRY_GRACE_SEC * 2]
        return state

    def get_active(self) -> List[TrackState]:
        return list(self._active.values())

    def get_track(self, track_id: int) -> Optional[TrackState]:
        return self._active.get(track_id)

    # ── Zone helpers ───────────────────────────────────────────────────────────
    def enter_zone(self, track_id: int, zone_id: str, now: float) -> None:
        state = self._active.get(track_id)
        if state:
            state.current_zone = zone_id
            state.zone_enter_time = now
            state.last_dwell_emit_time = now
            state.session_seq += 1

    def exit_zone(self, track_id: int, now: float) -> Optional[Tuple[str, int]]:
        """Returns (zone_id, dwell_ms) or None."""
        state = self._active.get(track_id)
        if state and state.current_zone and state.zone_enter_time:
            zone_id = state.current_zone
            dwell_ms = int((now - state.zone_enter_time) * 1000)
            state.current_zone = None
            state.zone_enter_time = None
            state.session_seq += 1
            return zone_id, dwell_ms
        return None

    def should_emit_dwell(self, track_id: int, now: float) -> bool:
        state = self._active.get(track_id)
        if state and state.last_dwell_emit_time:
            elapsed_ms = (now - state.last_dwell_emit_time) * 1000
            if elapsed_ms >= DWELL_EMIT_INTERVAL_MS:
                state.last_dwell_emit_time = now
                return True
        return False