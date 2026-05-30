# DESIGN.md — Store Intelligence System Architecture

## Overview

The Store Intelligence system converts raw CCTV footage from retail stores into
a live analytics API. It answers the North Star question: **"What is our offline
store conversion rate, and why?"**

The system is built in two layers:

1. **Detection Layer** (Python) — processes video frames and emits structured events
2. **Intelligence API** (FastAPI + SQLite) — ingests events, computes analytics,
   detects anomalies, exposes queryable endpoints

```
Raw CCTV Clips
    │
    ▼
[YOLOv8 Detector]          Detect all persons in frame
    │
    ▼
[ByteTrack Tracker]         Assign stable per-camera track IDs
    │                       Handle occlusion without needing ReID at track level
    ▼
[Crop Extractor]            Extract 256×128 px person crops (ImageNet normalised)
    │
    ▼
[OSNet Embedder]            Generate 512-d L2-normalised appearance embeddings
    │                       Pretrained on Market-1501 + MSMT17 (zero-shot)
    ▼
[VisitorRegistry]           Thread-safe identity resolution across all cameras:
    │                         • Active visitor → CROSS_CAMERA_MATCH
    │                         • Exited visitor within window → REENTRY
    │                         • Unknown → NEW_VISITOR
    ▼
[Event Publisher]           Emit structured JSON events (stdout or Redis)
    │
    ▼
POST /events/ingest         Store Intelligence API ingests event batch
    │
    ▼
[SQLite — WAL mode]         Persisted, indexed event store
    │
    ├── GET /stores/{id}/metrics    → KPIs (visitors, conversion, dwell, queues)
    ├── GET /stores/{id}/funnel     → Entry → Zone → Billing → Purchase
    ├── GET /stores/{id}/heatmap    → Zone visit frequency (normalised 0–100)
    ├── GET /stores/{id}/anomalies  → Queue spike, conversion drop, dead zones
    └── GET /health                 → Per-store STALE_FEED detection
```

---

## Technology Choices

### Detection: YOLOv8 (nano variant)
YOLOv8n was selected for person detection because it offers the best
latency/accuracy trade-off for retail CCTV (640×480 input, 15 FPS streams).
At ~80 FPS on CPU and ~500 FPS on GPU, it leaves headroom for the
embedding step. See `CHOICES.md` for the full model comparison.

### Tracking: ByteTrack (via `supervision`)
ByteTrack maintains stable per-camera track IDs even through occlusion by
using a two-stage association that includes low-confidence detections.
Unlike DeepSORT, it does NOT require a separate appearance model for
tracking — that role is fulfilled by OSNet at the VisitorRegistry level.
This clean separation of concerns avoids running two ReID models.

### Re-Identification: OSNet-x1.0 (torchreid)
512-dimensional appearance embeddings, pretrained on MSMT17 (15 cameras,
4,101 identities). Cosine similarity matching with a threshold of 0.82
for cross-camera matching and 0.80 for re-entry detection.
The EMA update (α=0.3) smooths embedding drift over time.

### Event Storage: SQLite with WAL mode
SQLite with Write-Ahead Logging allows concurrent reads from multiple
FastAPI worker threads while a single writer ingests events. This is
sufficient for the challenge workload. WAL mode ensures:
- Concurrent reads don't block writes
- Crashes don't corrupt the database
- Idempotent INSERT OR IGNORE on event_id is atomic

### API Framework: FastAPI + Pydantic v2
FastAPI's automatic OpenAPI documentation at `/docs` makes the API
self-describing for on-call engineers. Pydantic v2 validation rejects
malformed events at ingestion time with structured error messages.

---

## AI-Assisted Decisions

### 1. Database Engine Selection

**Decision**: SQLite with WAL mode vs PostgreSQL vs Redis

When designing the event storage layer, I consulted an LLM to evaluate
the trade-off between SQLite, PostgreSQL, and Redis for a single-store
analytics API. The LLM suggested SQLite-WAL for the challenge workload,
reasoning that:
- The challenge has a single API instance with one writer and multiple readers
- SQLite-WAL handles this without infrastructure overhead
- Adding PostgreSQL requires a separate container, migration tooling, and
  connection pooling configuration that adds complexity without benefit at
  this scale

**I agreed** with this recommendation. The challenge says "SQLite is fine"
in the FAQ, confirming this was the intended approach. I added WAL mode
and PRAGMA synchronous=NORMAL (not the default) based on the LLM's
suggestion that this gives a 3–5× write throughput improvement over the
default DELETE journal mode with acceptable crash-safety guarantees.

**What I overrode**: The LLM initially suggested aiosqlite for async
SQLite access. I chose synchronous SQLite with threading.RLock instead
because async SQLite adds complexity (async context managers, event loop
coupling) without a benefit at single-writer throughput. FastAPI runs sync
DB calls in a thread pool automatically.

---

### 2. Conversion Rate Computation Without POS Customer IDs

**Decision**: Billing-zone proxy vs POS file parsing vs event correlation

The challenge states:
> "A visitor who was in the billing zone in the 5-minute window before a
> transaction timestamp counts as a converted visitor for that session."

This requires correlating pos_transactions.csv (no customer_id) with
events. I asked an LLM how to handle the case where POS data is not
available at API startup.

The LLM suggested a **billing-zone proxy**:
- A visitor who joined the billing queue and did NOT emit a
  BILLING_QUEUE_ABANDON event is treated as "converted"

**I agreed** because:
- This is computable from events alone (no external file dependency)
- It correctly handles the "converted" definition: stayed through the
  transaction without abandoning
- The challenge's own event schema includes BILLING_QUEUE_ABANDON,
  which exists precisely for this computation

I also implemented the time-window POS correlation path in `api/store_metrics.py`
as a comment, ready to be activated when pos_transactions.csv is loaded.
The LLM helped design the 5-minute window join logic using an interval tree.

---

### 3. Anomaly Threshold Calibration

**Decision**: Queue spike threshold = 5, conversion drop warn = 80 % of 7-day avg

I asked an LLM for guidance on choosing anomaly thresholds for a retail
CCTV system with no historical baseline.

The LLM suggested:
- Queue spike: 5 as WARN, 10 as CRITICAL — based on typical single-counter
  service capacity in retail (5 people = ~5-minute wait)
- Conversion drop: alert at 20 % below 7-day average — common SLA threshold
  in retail analytics dashboards

**I agreed with the direction but adjusted the implementation**:
- Added a **7-day history requirement** before firing CONVERSION_DROP —
  the LLM's initial version would fire on day 1 when there's no baseline.
  Alerting on 0 % vs 0 % makes no sense operationally.
- Added a **minimum session count (20)** for the data_confidence flag on
  heatmaps — the LLM didn't include this check initially. A heatmap based
  on 2 visitors is noise, not signal.

---

## Production Readiness Notes

### Idempotency
`POST /events/ingest` is idempotent by `event_id`. The SQLite layer uses
`INSERT OR IGNORE` — calling the endpoint twice with the same payload
produces the same DB state. This is tested in `tests/test_api.py`.

### Graceful Degradation
All endpoints catch `Exception` and return HTTP 503 with a structured body:
```json
{"error": "Metrics unavailable", "detail": "..."}
```
No raw stack traces are exposed in responses.

### Structured Logging
Every request logs `trace_id`, `store_id`, `endpoint`, `latency_ms`,
`event_count` (for ingest), and `status_code` via `StructuredLoggingMiddleware`.
The `X-Trace-ID` header is echoed back to callers for end-to-end tracing.

### Health Endpoint
`GET /health` returns per-store `last_event_timestamp` and flags
`STALE_FEED` if the last event is more than 10 minutes old. This is the
first endpoint an on-call engineer checks.

### Thread Safety
The `VisitorRegistry` uses `threading.RLock` for all mutations. The
SQLite layer uses a module-level `threading.RLock` around all connections.
Each camera pipeline gets its own `OSNetEmbedder` instance — no shared
model state between threads.

---

## Known Limitations

See `LIMITATIONS.md` for a full list. Key items:

1. **Staff detection** is rule-based (top-of-frame heuristic). A VLM-based
   approach using uniform colour classification would be more accurate.
2. **Direction detection** (ENTRY vs EXIT) requires a virtual line crossing
   check on the tracker centroid. This is implemented in the event schema
   but the pipeline currently maps NEW_VISITOR → ENTRY and VISITOR_EXITED → EXIT.
3. **Zone mapping** requires `store_layout.json` to be loaded and camera
   positions to be calibrated. This is the largest remaining gap.
4. **POS correlation** is implemented as a proxy (billing zone dwell).
   True correlation requires matching `pos_transactions.csv` timestamps to
   visitor sessions within a 5-minute window per store.
