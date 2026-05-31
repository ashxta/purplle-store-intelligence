# AI-Powered Store Intelligence System

## Overview

This project is an end-to-end Store Intelligence System developed for the Purplle Tech Challenge 2026.

The system processes raw CCTV footage, generates structured customer behavior events, ingests them into a real-time analytics pipeline, correlates customer activity with POS transactions, and exposes business intelligence APIs for retail decision-making.

The solution provides:

* Visitor tracking and session creation
* Entry and exit detection
* Zone analytics
* Customer dwell time measurement
* Billing queue monitoring
* Conversion funnel analysis
* Retail heatmaps
* Operational anomaly detection
* Store health monitoring

---

## Architecture

Raw CCTV Footage

↓

Detection & Tracking Pipeline

↓

Structured Event Stream (JSONL)

↓

FastAPI Event Ingestion Service

↓

SQLite Analytics Store

↓

Metrics / Funnel / Heatmap / Anomaly APIs

↓

Dashboard / Monitoring Layer

---

## Tech Stack

* Python 3.13
* FastAPI
* SQLite
* OpenCV
* YOLOv8
* ByteTrack-style visitor tracking
* Docker & Docker Compose

---

## API Endpoints

### POST /events/ingest

Ingests and validates event batches.

### GET /stores/{store_id}/metrics

Returns:

* Unique Visitors
* Conversion Rate
* Average Dwell Time
* Queue Depth
* Revenue Metrics

### GET /stores/{store_id}/funnel

Returns conversion funnel:

Entry → Zone Visit → Billing Queue → Purchase

### GET /stores/{store_id}/heatmap

Returns normalized zone visit frequencies.

### GET /stores/{store_id}/anomalies

Returns:

* Dead Zone alerts
* Queue Spikes
* Conversion Drops

### GET /health

Returns service health and feed status.

---

## Running the Project

### Install dependencies

pip install -r requirements.txt

### Start API

python main.py

### Process CCTV Footage

python detect.py --video "data/cctv_footage/CAM 1.mp4" ...

### Ingest Events

POST generated JSONL events to:

http://127.0.0.1:8000/events/ingest

---

## Features Implemented

✓ Entry/Exit Detection

✓ Visitor Tracking

✓ Re-entry Detection

✓ Zone Analytics

✓ Billing Queue Detection

✓ POS Correlation

✓ Conversion Funnel

✓ Heatmap Generation

✓ Anomaly Detection

✓ Health Monitoring

---

## Future Improvements

* Multi-camera cross-store Re-ID
* DeepSORT / StrongSORT integration
* PostgreSQL backend
* Kafka event streaming
* Real-time web dashboard
* Advanced forecasting models

## Dataset Notice

The CCTV footage, retail transaction datasets, and challenge-provided raw data have been intentionally excluded from this repository in accordance with the Purplle Tech Challenge 2026 submission guidelines.

Only the source code, architecture documents, configuration files, and implementation artifacts required to reproduce the system are included.

