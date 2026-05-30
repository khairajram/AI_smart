'use strict';
/**
 * server.js
 * ─────────
 * Main entry point for the ReID Analytics Dashboard backend.
 *
 * Start order:
 *   1. Load env + config
 *   2. Init Express + Socket.io
 *   3. Middleware stack
 *   4. Static dashboard files
 *   5. API routes
 *   6. Init SQLite DB
 *   7. Init services (metrics, registry, cameras)
 *   8. Setup Socket.io handlers
 *   9. Try Python FastAPI connection → demo fallback
 *  10. Periodic broadcasts
 *  11. Listen
 */

const path        = require('path');
require('dotenv').config({ path: path.join(__dirname, '.env') });
const http        = require('http');
const express     = require('express');
const { Server }  = require('socket.io');
const cors        = require('cors');
const compression = require('compression');
const morgan      = require('morgan');

const config       = require('./config');
const db           = require('./db');
const apiRouter    = require('./routes/api');
const datasetRouter = require('./routes/dataset');
const metricsStore = require('./services/metricsStore');
const visitorReg   = require('./services/visitorRegistry');
const cameraMgr    = require('./services/cameraManager');
const reidClient   = require('./services/reidClient');
const simulator    = require('./services/eventSimulator');
const {
  setupSocketHandlers,
  broadcastEvent,
  broadcastMetrics,
  broadcastCameras,
  broadcastRegistry,
  broadcastModeChange,
} = require('./socket/handlers');

// ── App setup ────────────────────────────────────────────────────────
const app    = express();
const server = http.createServer(app);
const io     = new Server(server, {
  cors: { origin: '*', methods: ['GET', 'POST'] },
  pingTimeout:  60000,
  pingInterval: 25000,
});

// ── Middleware ────────────────────────────────────────────────────────
app.use(cors());
app.use(compression());
app.use(morgan('dev'));
app.use(express.json());
app.use(express.urlencoded({ extended: false }));

// ── Static dashboard ─────────────────────────────────────────────────
// In Docker: WORKDIR=/app, dashboard files are at /app/dashboard/
// __dirname is /app, so we join directly (no '..' needed)
const fs = require('fs');
let dashboardPath = path.join(__dirname, 'dashboard');
if (!fs.existsSync(dashboardPath)) {
  dashboardPath = path.join(__dirname, '..', 'dashboard');
}
app.use(express.static(dashboardPath));
app.get('/', (req, res) => {
  res.sendFile(path.join(dashboardPath, 'index.html'));
});

// ── API routes ────────────────────────────────────────────────────────
// IMPORTANT: dataset router must be mounted BEFORE apiRouter to avoid
// the apiRouter's catch-all 404 handler intercepting /api/dataset requests.
app.use('/api/dataset', datasetRouter);
app.use('/api', apiRouter);

// ── 404 fallback ──────────────────────────────────────────────────────
app.use((req, res) => {
  if (req.path.startsWith('/api')) {
    return res.status(404).json({ error: 'Not found' });
  }
  // SPA fallback — only if index.html exists
  const indexPath = path.join(dashboardPath, 'index.html');
  res.sendFile(indexPath, (err) => {
    if (err) res.status(200).send('<html><body><h2>Dashboard starting...</h2><script>setTimeout(()=>location.reload(),2000)</script></body></html>');
  });
});

// ── Error handler ─────────────────────────────────────────────────────
app.use((err, req, res, _next) => {
  console.error('[Error]', err.stack);
  res.status(500).json({ error: err.message });
});

// ── Central event processor ───────────────────────────────────────────
/**
 * Called for every ReID event regardless of source (simulator or live Python).
 * Updates all in-memory state, persists to DB, and broadcasts to clients.
 */
function processEvent(event) {
  try {
    // 1. Persist to SQLite
    db.insertEvent(event);

    // 2. Update in-memory metrics
    metricsStore.updateFromEvent(event);

    // 3. Update visitor registry
    if (event.event_type === 'VISITOR_EXITED') {
      visitorReg.markExited(event.visitor_id);
    } else {
      visitorReg.upsertVisitor(event);
    }

    // 4. Update camera state
    if (event.camera_id) {
      cameraMgr.updateCamera(event.camera_id, {
        last_event:  new Date(event.timestamp * 1000),
        status:      'active',
      });
      if (event.event_type === 'NEW_VISITOR') {
        const cam = cameraMgr.getCameraById(event.camera_id);
        if (cam) {
          cameraMgr.updateCamera(event.camera_id, {
            active_visitors: cam.active_visitors + 1,
            total_visitors:  cam.total_visitors + 1,
            events_today:    cam.events_today + 1,
          });
        }
      } else if (event.event_type === 'VISITOR_EXITED') {
        const cam = cameraMgr.getCameraById(event.camera_id);
        if (cam) {
          cameraMgr.updateCamera(event.camera_id, {
            active_visitors: Math.max(0, cam.active_visitors - 1),
            events_today:    cam.events_today + 1,
          });
        }
      } else {
        const cam = cameraMgr.getCameraById(event.camera_id);
        if (cam) {
          cameraMgr.updateCamera(event.camera_id, {
            events_today: cam.events_today + 1,
          });
        }
      }
    }

    // 5. Broadcast event to all connected WebSocket clients
    broadcastEvent(event);

  } catch (err) {
    console.error('[processEvent] Error:', err.message);
  }
}

// ── Initialise and start ───────────────────────────────────────────────
async function start() {
  console.log('\n╔════════════════════════════════════════╗');
  console.log('║   ReID Analytics Dashboard — Backend   ║');
  console.log('╚════════════════════════════════════════╝\n');

  // 1. Init SQLite DB
  db.init();
  console.log('[DB] SQLite initialised');

  // 2. Init services
  metricsStore.init();
  cameraMgr.initCameras(config.CAMERAS);
  console.log(`[Cameras] Initialised: ${config.CAMERAS.join(', ')}`);

  // 3. Setup Socket.io handlers
  setupSocketHandlers(io, {
    metricsStore,
    cameraManager: cameraMgr,
    visitorRegistry: visitorReg,
    db,
  });

  // Inject io into dataset router for real-time broadcasts
  datasetRouter.setIO(io);

  // 4. Try Python FastAPI connection
  let pythonConnected = false;
  if (!config.DEMO_MODE) {
    console.log(`[Python] Checking connection to ${config.PYTHON_API_URL} …`);
    pythonConnected = await reidClient.checkConnection();
  }

  if (pythonConnected) {
    config.DEMO_MODE = false;
    console.log('[Python] ✓ Connected — running in LIVE mode');
    broadcastModeChange('live', true);

    // Poll Python /metrics every 2 seconds
    reidClient.pollForever(2000, (metrics) => {
      metricsStore.updateFromPythonMetrics(metrics);
    });
  } else {
    config.DEMO_MODE = true;
    console.log('[Simulator] Python API not available — running in DEMO mode');
    broadcastModeChange('demo', false);

    // Start event simulator
    simulator.start(processEvent);
    console.log(`[Simulator] ✓ Started — interval: ${config.SIMULATE_INTERVAL_MS}ms`);
  }

  // 5. Periodic broadcast: metrics + cameras every 2s
  setInterval(() => {
    try {
      const metrics = metricsStore.getMetrics();
      broadcastMetrics(metrics);
      broadcastCameras(cameraMgr.getCameras());
    } catch (err) {
      console.error('[Broadcast] metrics/cameras error:', err.message);
    }
  }, 2000);

  // 6. Periodic broadcast: registry snapshot every 10s + DB snapshot
  setInterval(() => {
    try {
      broadcastRegistry(visitorReg.getActive(), visitorReg.getExited());
      const metrics = metricsStore.getMetrics();
      db.insertMetricSnapshot(metrics);
    } catch (err) {
      console.error('[Broadcast] registry/snapshot error:', err.message);
    }
  }, 10000);

  // 7. Listen
  server.listen(config.PORT, () => {
    console.log(`\n[Server] ✓ Listening on http://localhost:${config.PORT}`);
    console.log(`[Dashboard] → http://localhost:${config.PORT}/`);
    console.log(`[API Docs]  → http://localhost:${config.PORT}/api/health`);
    console.log(`[Mode]      → ${config.DEMO_MODE ? 'DEMO (simulator active)' : 'LIVE (Python API connected)'}`);
    console.log('\nPress Ctrl+C to stop.\n');
  });

  // 8. Graceful shutdown
  process.on('SIGINT',  () => gracefulShutdown('SIGINT'));
  process.on('SIGTERM', () => gracefulShutdown('SIGTERM'));
}

function gracefulShutdown(signal) {
  console.log(`\n[Server] ${signal} received — shutting down gracefully…`);
  simulator.stop();
  server.close(() => {
    db.close();
    console.log('[Server] Closed. Goodbye.');
    process.exit(0);
  });
  setTimeout(() => process.exit(1), 5000);
}

start().catch((err) => {
  console.error('[Server] Fatal startup error:', err);
  process.exit(1);
});
