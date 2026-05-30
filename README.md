# Store Intelligence API

> **Engineering Hiring Challenge Submission**
> End-to-end retail analytics: raw CCTV footage → live store KPI API.

[![API Health](https://img.shields.io/badge/API-FastAPI%202.0-009688?style=flat-square)](http://localhost:8000/docs)
[![Docker Ready](https://img.shields.io/badge/Docker-docker%20compose%20up-2496ED?style=flat-square)](./docker-compose.yml)
[![Tests](https://img.shields.io/badge/Tests-pytest-green?style=flat-square)](./tests/)

---

## What This System Does

```
ZIP Dataset (clips + layout + POS)
         │
         ▼
  Detection Pipeline          — YOLOv8 detects people, ByteTrack tracks them,
  (main.py)                     OSNet embeds them, StaffDetector classifies staff
         │
         ▼ POST /events/ingest
  Store Intelligence API      — Ingest events, compute KPIs, detect anomalies
  (FastAPI + SQLite)
         │
         ▼
  Live Web Dashboard          — Metrics updating in real time
  (Node.js + WebSocket)
```

**North Star metric:** `Conversion Rate = Visitors who purchased ÷ Total unique visitors`

---

## Part 1 — Docker Quick Start (Acceptance Gate)

> ⚠️ You need **Docker Desktop** installed and running. No other dependencies needed.

### Step 1 — Clone the repository

```bash
git clone <your-repo-url>
cd store-intelligence
```

### Step 2 — Unzip the dataset

Unzip the provided challenge dataset ZIP into the project root:

```
store-intelligence/
├── footage/
│   ├── BRIGADE_BLR/
│   │   ├── CAM_1.mp4          ← Entry/Exit camera
│   │   ├── CAM_2.mp4          ← Floor camera
│   │   └── ...
│   └── <any_other_store_id>/
├── store_layout.json           ← Zone definitions (Update this for new datasets)
├── pos_transactions.csv        ← POS transaction records
└── sample_events.jsonl         ← Example events for validation
```

> 💡 **No dataset yet?** The API and dashboard still start in demo mode. Skip to Step 3.

### Step 3 — Start everything

```bash
docker compose up
```

This starts two services:
| Service | URL | What it does |
|---|---|---|
| `api` | http://localhost:8000 | Store Intelligence REST API |
| `dashboard` | http://localhost:3000 | Live web dashboard |

**Expected output in ~60 seconds:**

```
store-intelligence-api        | INFO: Store Intelligence DB ready total_events=0
store-intelligence-api        | INFO: Application startup complete.
store-intelligence-api        | INFO: Uvicorn running on http://0.0.0.0:8000
store-intelligence-dashboard  | Dashboard listening on port 3000
```

### Step 4 — Verify the API is alive

```bash
# Health check (what the challenge graders check first)
curl http://localhost:8000/health
```

Expected response:
```json
{
  "status": "ok",
  "uptime_seconds": 12.4,
  "version": "2.0.0",
  "total_events": 0,
  "stores": {}
}
```

### Step 5 — Open the live dashboard

```
http://localhost:3000
```

The dashboard shows live metrics as events flow in. During dataset processing, you will see visitor counts, zone heatmaps, and anomaly alerts updating in real time.

---

## Part 2 — Processing the Dataset (Detection Pipeline)

Once the API is running, process the CCTV clips to generate events.

### Option A — Run pipeline inside Docker (recommended)

1. **Update `store_layout.json`** to define the zones and camera IDs for the new dataset.
2. **Run the pipeline** using the files provided in the ZIP:

```bash
# Process a single store (Cameras run in parallel)
docker compose run --rm api python main.py \
    --cameras /app/footage/BRIGADE_BLR/CAM_1.mp4 \
               /app/footage/BRIGADE_BLR/CAM_2.mp4 \
               /app/footage/BRIGADE_BLR/CAM_4.mp4 \
    --camera-ids CAM_1 CAM_2 CAM_4 \
    --store-id BRIGADE_BLR

# The pipeline:
# 1. Detects persons with YOLOv8
# 2. Tracks them with ByteTrack
# 3. Embeds crops with OSNet (512-d appearance vectors)
# 4. Resolves identities in the VisitorRegistry
# 5. Classifies staff (spatial zone + dwell pattern)
# 6. Emits structured events → POST http://api:8000/events/ingest
```

### Option B — Run pipeline locally (faster for development)

```bash
# Install dependencies
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS

pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
pip install git+https://github.com/KaiyangZhou/deep-person-reid.git

# Run on footage files (API must be running separately via docker compose up)
python main.py \
    --cameras footage/BRIGADE_BLR/CAM_1.mp4 \
               footage/BRIGADE_BLR/CAM_2.mp4 \
               footage/BRIGADE_BLR/CAM_4.mp4 \
    --camera-ids CAM_1 CAM_2 CAM_4 \
    --store-id BRIGADE_BLR
```

### Processing all stores at once

```bash
# Process all stores sequentially
for store in BRIGADE_BLR; do
  docker compose run --rm api python main.py \
      --cameras /app/footage/${store}/CAM_1.mp4 \
                /app/footage/${store}/CAM_4.mp4 \
      --camera-ids CAM_1 CAM_4 \
      --store-id ${store}
done
```

> ⏱️ **Time estimate:** 20 min of footage processes in ~20 min (real-time) on CPU, ~5 min on GPU.

---

## Part 3 — Verifying the System Works

### 3.1 Run the challenge's own test assertions

The dataset includes `assertions.py` with 10 test assertions:

```bash
# Copy assertions.py from the dataset into the project root, then:
docker compose run --rm api python assertions.py
```

All 10 assertions should pass after processing the provided footage.

### 3.2 Run the full test suite

```bash
# Inside Docker
docker compose run --rm api pytest tests/ -v

# Locally
pytest tests/ -v
```

Expected output:
```
tests/test_api.py::TestIngest::test_valid_events_accepted        PASSED
tests/test_api.py::TestIngest::test_idempotency_same_payload_twice  PASSED
tests/test_api.py::TestIngest::test_batch_size_limit             PASSED
tests/test_api.py::TestStoreMetrics::test_staff_excluded_from_metrics  PASSED
tests/test_api.py::TestFunnel::test_reentry_not_double_counted   PASSED
tests/test_api.py::TestAnomalies::test_queue_spike_detected      PASSED
... (25 tests total)

========================= 25 passed in 3.2s =========================
```

### 3.3 Validate the API returns data

After processing the footage, call each graded endpoint:

```bash
STORE=BRIGADE_BLR
BASE=http://localhost:8000

# ── Acceptance gate endpoints ──────────────────────────────────────
# Gate 3: ingest endpoint
curl -s -X POST $BASE/events/ingest \
  -H "Content-Type: application/json" \
  -d '{"events":[]}' | python -m json.tool

# Gate 4: metrics endpoint (must return valid JSON)
curl -s $BASE/stores/$STORE/metrics | python -m json.tool

# ── Full scored endpoint suite ──────────────────────────────────────
curl -s $BASE/stores/$STORE/metrics   | python -m json.tool
curl -s $BASE/stores/$STORE/funnel    | python -m json.tool
curl -s $BASE/stores/$STORE/heatmap   | python -m json.tool
curl -s $BASE/stores/$STORE/anomalies | python -m json.tool
curl -s $BASE/health                  | python -m json.tool
```

### 3.4 Expected API responses

**GET /stores/BRIGADE_BLR/metrics**
```json
{
  "store_id": "BRIGADE_BLR",
  "window_start": "2026-03-03T00:00:00Z",
  "window_end": "2026-03-03T23:59:59Z",
  "unique_visitors": 142,
  "conversion_rate": 0.38,
  "avg_dwell_per_zone": {
    "SKINCARE": 45200,
    "MAKEUP_FLOOR": 28100,
    "BILLING":  72400
  },
  "queue_depth": 3,
  "abandonment_rate": 0.12
}
```

**GET /stores/BRIGADE_BLR/funnel**
```json
{
  "store_id": "BRIGADE_BLR",
  "stages": [
    { "stage": "entry",         "visitors": 142, "drop_off_pct": 0.0  },
    { "stage": "zone_visit",    "visitors": 128, "drop_off_pct": 9.9  },
    { "stage": "billing_queue", "visitors": 61,  "drop_off_pct": 52.3 },
    { "stage": "purchase",      "visitors": 54,  "drop_off_pct": 11.5 }
  ]
}
```

**GET /stores/BRIGADE_BLR/anomalies**
```json
{
  "store_id": "BRIGADE_BLR",
  "anomalies": [
    {
      "anomaly_type": "BILLING_QUEUE_SPIKE",
      "severity": "WARN",
      "detail": "Queue depth is 8 (threshold: 5)",
      "suggested_action": "Open additional billing counter or call supervisor",
      "detected_at": "2026-03-03T14:38:00Z"
    }
  ]
}
```

### 3.5 Validate sample_events.jsonl against the schema

Use the provided sample events to validate your pipeline's output format:

```bash
# Ingest the sample events from the dataset
docker compose run --rm api \
  python -c "
import json, httpx
events = [json.loads(l) for l in open('/app/sample_events.jsonl')]
r = httpx.post('http://localhost:8000/events/ingest', json={'events': events[:200]})
print(r.json())
"

# Expected: all 200 accepted, 0 rejected
# { 'accepted': 200, 'rejected': 0, 'duplicate': 0, 'errors': [] }
```

---

## Part 4 — Live Dashboard Verification

The dashboard at **http://localhost:3000** shows:

| Widget | What it shows |
|---|---|
| **Visitor Counter** | Unique visitors today (excludes staff) |
| **Conversion Rate** | Live % with trend arrow |
| **Zone Heatmap** | 0–100 normalised visit scores per zone |
| **Funnel Chart** | Entry → Zone → Billing → Purchase drop-off |
| **Anomaly Feed** | Real-time alerts (queue spike, conversion drop) |
| **Active Visitors** | Currently in-store count |

To see metrics update live while the pipeline processes footage:

```bash
# Terminal 1: Start API + dashboard
docker compose up

# Terminal 2: Start processing footage (sends events to the API in real time)
docker compose run --rm api python main.py \
    --cameras /app/footage/BRIGADE_BLR/CAM_1.mp4 \
               /app/footage/BRIGADE_BLR/CAM_4.mp4 \
    --camera-ids CAM_1 CAM_4 \
    --store-id BRIGADE_BLR

# Watch http://localhost:3000 — metrics update every 2 seconds
```

---

## Part 5 — Structured Logs (Production Readiness)

Every API request produces a structured JSON log line:

```json
{
  "timestamp": "2026-03-03T14:38:12+00:00",
  "level": "info",
  "event": "request_complete",
  "trace_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "method": "GET",
  "path": "/stores/BRIGADE_BLR/metrics",
  "store_id": "BRIGADE_BLR",
  "status_code": 200,
  "latency_ms": 4.2,
  "event_count": null
}
```

View logs:
```bash
docker compose logs api --follow
```

---

## Part 6 — Configuration Reference

Copy `.env.example` to `.env` to override any setting:

```bash
cp .env.example .env
```

| Variable | Default | Description |
|---|---|---|
| `REID_SIMILARITY_THRESHOLD` | `0.82` | Cross-camera match cosine threshold |
| `REENTRY_SIMILARITY_THRESHOLD` | `0.80` | Re-entry detection threshold |
| `REENTRY_WINDOW_SECONDS` | `300` | Re-entry window (seconds) |
| `YOLO_MODEL` | `yolov8n.pt` | Detection model (`yolov8n` / `yolov8s` / `yolov8m`) |
| `YOLO_CONFIDENCE` | `0.40` | Min detection confidence |
| `STAFF_ZONE_TOP_PCT` | `0.15` | Fraction of frame height = staff zone (Tier 1) |
| `STAFF_DWELL_FRAMES` | `150` | Frames of low movement to classify staff (Tier 2) |
| `STAFF_UNIFORM_HUE_MIN` | `-1` | HSV hue min for uniform detection, -1 = disabled |
| `STAFF_UNIFORM_HUE_MAX` | `-1` | HSV hue max for uniform detection, -1 = disabled |
| `LOG_FORMAT` | `json` | `json` (Docker) or `console` (local dev) |
| `REID_DEVICE` | `auto` | `auto` / `cpu` / `cuda` / `mps` |

---

## Project Structure

```
store-intelligence/
│
├── pipeline/
│   └── orchestrator.py     # Per-camera detect → track → embed → match loop
│
├── reid/
│   ├── embedder.py         # OSNet wrapper → 512-d L2 embeddings
│   ├── registry.py         # VisitorRegistry — cross-camera deduplication
│   ├── similarity.py       # Cosine similarity engine
│   ├── staff_detector.py   # 3-tier staff classifier (spatial/dwell/uniform)
│   ├── events.py           # Event schema + publisher (stdout / Redis)
│   └── crop_utils.py       # Bounding-box crop + preprocessing
│
├── tracking/
│   ├── detector.py         # YOLOv8 person detector
│   └── tracker.py          # ByteTrack integration
│
├── api/
│   ├── server.py           # FastAPI app factory + all endpoints
│   ├── models.py           # Pydantic event schema (challenge-compliant)
│   ├── db.py               # SQLite WAL persistence layer
│   ├── ingestion.py        # POST /events/ingest (idempotent)
│   ├── store_metrics.py    # GET /stores/{id}/metrics
│   ├── funnel.py           # GET /stores/{id}/funnel
│   ├── heatmap.py          # GET /stores/{id}/heatmap
│   ├── anomalies.py        # GET /stores/{id}/anomalies
│   └── middleware.py       # Structured logging (trace_id, latency_ms)
│
├── config/
│   └── settings.py         # All env-var config (pydantic-settings)
│
├── tests/
│   ├── test_api.py         # 25 integration tests (all endpoints)
│   ├── test_registry.py    # VisitorRegistry unit tests
│   ├── test_embedder.py    # OSNet embedder unit tests
│   └── test_similarity.py  # Cosine similarity unit tests
│
├── backend/                # Node.js dashboard backend
├── dashboard/              # Dashboard frontend (HTML/CSS/JS)
├── footage/                # ← Place dataset clips here
│
├── Dockerfile              # Python API container
├── Dockerfile.dashboard    # Node.js dashboard container
├── docker-compose.yml      # Orchestrates both services
├── .dockerignore           # Excludes node_modules, .git, footage
├── .env.example            # Configuration template
├── requirements.txt        # Python dependencies
├── main.py                 # CLI entry point for detection pipeline
│
├── DESIGN.md               # Architecture + AI-Assisted Decisions
├── CHOICES.md              # Model selection + 3 design decisions
└── LIMITATIONS.md          # Known limitations + mitigations
```

---

## Edge Cases Handled

| Challenge Edge Case | How the System Handles It |
|---|---|
| **Group entry** (2–4 people together) | YOLOv8 detects individuals within a group; each gets a separate `visitor_id` |
| **Staff movement** | 3-tier `StaffDetector` (spatial zone + dwell pattern + uniform colour); `is_staff=true` events excluded from all KPIs |
| **Re-entry** (customer returns) | `VisitorRegistry` holds exited visitors for 5 min; cosine similarity match → `REENTRY` event; not double-counted in funnel |
| **Partial occlusion** | ByteTrack's two-stage association keeps tracks alive through occlusion; low-confidence crops are skipped (not silently elevated) |
| **Billing queue buildup** | `BILLING_QUEUE_JOIN` events with `queue_depth` metadata; `BILLING_QUEUE_SPIKE` anomaly fires at depth > 5 |
| **Empty store periods** | All endpoints return zeros (not null, not 500) when no events exist for a store |
| **Camera angle overlap** | Cross-camera ReID via OSNet embeddings; same person in two cameras = one `visitor_id`, one count |

---

## Submission Checklist

- [x] `docker compose up` starts everything — no manual steps beyond `git clone`
- [x] README explains how to run detection pipeline against arbitrary data clips
- [x] `POST /events/ingest` accepts events without 5xx
- [x] `GET /stores/BRIGADE_BLR/metrics` returns valid JSON
- [x] `DESIGN.md` exists, >250 words, includes AI-Assisted Decisions section
- [x] `CHOICES.md` exists, >250 words, covers model selection + 3 decisions
- [x] Prompt blocks at top of each test file (`# PROMPT:` / `# CHANGES MADE:`)
- [x] Live dashboard at `http://localhost:3000`
- [x] Structured logging with `trace_id`, `store_id`, `latency_ms`
- [x] Idempotent ingest (tested in `tests/test_api.py::TestIngest::test_idempotency_same_payload_twice`)
- [x] Graceful 503 when DB unavailable — no raw stack traces
- [x] Staff excluded from all customer metrics

---

## Troubleshooting

**API container keeps restarting / health-check failing**
```bash
# Check the API logs
docker compose logs api --tail=50

# Common causes:
# - First run: YOLO model download takes 30-60 seconds — wait for it
# - Port 8000 already in use: lsof -i :8000 (Linux) or netstat (Windows)
```

**`footage/` volume mount error**
```bash
# Create the footage directory if it doesn't exist
mkdir footage
# Then place clips inside:
# footage/BRIGADE_BLR/CAM_1.mp4 etc.
```

**`torchreid` install failed in Docker**
```bash
# The API still works — only live camera processing is disabled
# To check: curl http://localhost:8000/health should return "ok"
# The /events/ingest endpoint accepts events from any source
```

**Dashboard shows "Connecting..."**
```bash
# Dashboard waits for the API to be healthy first
# Wait ~60 seconds, then refresh http://localhost:3000
# Or check: curl http://localhost:8000/health
```

**Windows line endings causing script failures**
```bash
# If running on Linux/macOS after editing on Windows:
dos2unix main.py pipeline/orchestrator.py
```
