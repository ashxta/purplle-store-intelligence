# DESIGN.md

## System Architecture

The project follows an event-driven architecture.

Instead of directly computing analytics from video footage, the pipeline first converts customer activity into structured events. This decouples computer vision from analytics and allows the analytics layer to evolve independently.

The architecture consists of five major components:

1. Detection Layer
2. Tracking Layer
3. Event Streaming Layer
4. Analytics API
5. Monitoring Layer

---

## Detection Layer

The detection layer processes CCTV footage frame-by-frame.

A YOLO-based detector identifies customers in each frame and extracts bounding boxes with confidence scores.

The detector operates independently for each camera.

---

## Tracking Layer

Detected customers are assigned temporary identities.

The tracker maintains movement history and session state to determine:

* ENTRY
* EXIT
* REENTRY
* ZONE_ENTER
* ZONE_EXIT
* ZONE_DWELL

This allows customer journeys to be reconstructed from event streams.

---

## Event Stream Design

JSONL was selected as the intermediate format because:

* Human-readable
* Easily replayable
* Debuggable
* Supports batch ingestion

Each event contains:

* visitor_id
* store_id
* camera_id
* event_type
* timestamp
* confidence
* metadata

---

## Analytics Layer

FastAPI provides REST endpoints for:

* Metrics
* Funnel
* Heatmap
* Anomalies
* Health

Analytics are computed dynamically from stored events.

---

## Database Choice

SQLite was selected because:

* Zero configuration
* Lightweight
* Fast for challenge scale
* Simple deployment

For production deployments PostgreSQL would be preferred.

---

## AI-Assisted Decisions

### Decision 1: Event-driven Architecture

AI suggested directly computing metrics from video outputs.

This approach was rejected because event streams are more scalable and easier to replay and debug.

### Decision 2: SQLite vs PostgreSQL

AI suggested PostgreSQL for production readiness.

SQLite was selected because challenge datasets are small and deployment simplicity was prioritized.

### Decision 3: Visitor Re-entry Handling

AI suggested generating a new visitor for every entry.

This was overridden because re-entry handling is a specific requirement in the challenge and significantly impacts conversion metrics.

---

## Trade-offs

The solution prioritizes:

* Simplicity
* Reliability
* Explainability

over maximum computer vision accuracy.

The resulting system is easier to debug, deploy, and extend.
