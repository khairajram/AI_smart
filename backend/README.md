# ReID Analytics Dashboard — Backend

A production-ready **Node.js / Express** server powering the ReID Analytics Dashboard.

---

## Quick Start

```bash
# Install dependencies
cd "f:\projects\computer vision\AI_smart\backend"
npm install

# (Optional) copy and edit environment file
copy .env.example .env

# Start in demo mode (no Python API needed)
npm start

# Start with hot-reload for development
npm run dev
```

The server starts on **http://localhost:3000** by default.

---

## Architecture

```
backend/
├── server.js                  ← Main entry point
├── config.js                  ← Centralised configuration (env-aware)
├── db.js                      ← SQLite schema + query helpers
├── routes/
│   └── api.js                 ← REST API router  (/api/*)
├── services/
│   ├── metricsStore.js        ← In-memory metrics (EPM, counters)
│   ├── visitorRegistry.js     ← Active / exited visitor maps
│   ├── cameraManager.js       ← Per-camera state & counters
│   ├── eventSimulator.js      ← Demo-mode realistic event generator
│   └── reidClient.js          ← HTTP client for Python FastAPI
└── socket/
    └── handlers.js            ← Socket.io connection handlers
```

---

## REST API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | System status, uptime, mode |
| GET | `/api/metrics` | Current live metrics |
| GET | `/api/metrics/history?hours=24` | Historical metric snapshots |
| GET | `/api/events?limit=100&type=NEW_VISITOR&since=<ts>` | Event log from SQLite |
| GET | `/api/events/timeline?hours=24` | Events grouped by hour |
| GET | `/api/registry` | Active + exited visitor lists |
| POST | `/api/registry/reset` | Clear registry, events, and metrics |
| GET | `/api/cameras` | Per-camera state |
| GET | `/api/config` | Current runtime config |
| PATCH | `/api/config` | Update thresholds / intervals |

---

## Socket.io Events

### Server → Client

| Event | Payload |
|-------|---------|
| `reid_event` | Full ReID event object |
| `metrics_update` | Metrics snapshot (every 2 s) |
| `camera_update` | Array of camera states (every 2 s) |
| `registry_snapshot` | `{ active, exited, stats }` (every 10 s) |
| `config_update` | Updated config object |
| `mode_change` | `{ mode: 'demo'\|'live', python_connected: bool }` |

### Client → Server

| Event | Description |
|-------|-------------|
| `get_registry` | Request immediate registry snapshot |
| `reset_registry` | Clear registry & events, broadcast to all clients |
| `update_config` | Patch runtime config fields |

---

## Modes

### Demo Mode (default)
Activated automatically when the Python FastAPI backend is unreachable.  
The **event simulator** generates realistic NEW_VISITOR, CROSS_CAMERA_MATCH,
VISITOR_EXITED, and REENTRY events at randomised intervals.

### Live Mode
When `PYTHON_API_URL` is reachable, the server polls `/metrics` every 2 seconds
and syncs the registry from the Python backend's state.

The server checks connection status every broadcast cycle and automatically
switches between modes if the Python API goes up or down.

---

## Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | 3000 | HTTP listen port |
| `PYTHON_API_URL` | http://localhost:8000 | Python FastAPI base URL |
| `DEMO_MODE` | true | Force demo mode |
| `DB_PATH` | ./reid_events.db | SQLite file path |
| `CAMERAS` | ENTRANCE,AISLE_1,CHECKOUT | Comma-separated camera IDs |
| `REID_THRESHOLD` | 0.82 | Minimum confidence for ReID match |
| `REENTRY_THRESHOLD` | 0.80 | Minimum confidence for re-entry |
| `REENTRY_WINDOW_SECONDS` | 300 | Re-entry detection window |

---

## Database Schema

### `events`
| Column | Type | Description |
|--------|------|-------------|
| `event_id` | TEXT UNIQUE | UUID from event source |
| `event_type` | TEXT | NEW_VISITOR / CROSS_CAMERA_MATCH / REENTRY / VISITOR_EXITED |
| `visitor_id` | TEXT | Visitor UUID |
| `camera_id` | TEXT | Camera identifier |
| `track_id` | INTEGER | Tracker-assigned track ID |
| `reid_confidence` | REAL | Match confidence score |
| `timestamp` | REAL | Unix epoch (float) |
| `extra_json` | TEXT | JSON blob for extra fields |

### `metrics_snapshots`
Periodic snapshots of aggregate counters for historical charting.
