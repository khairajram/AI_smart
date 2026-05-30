'use strict';

/**
 * routes/api.js
 * --------------
 * REST API router for the ReID Analytics Dashboard.
 */

const express = require('express');
const router = express.Router();

const config = require('../config');
const db = require('../db');
const metricsStore = require('../services/metricsStore');
const visitorRegistry = require('../services/visitorRegistry');
const cameraManager = require('../services/cameraManager');
const reidClient = require('../services/reidClient');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Safely parse an integer query param, falling back to `defaultVal`.
 */
function qInt(val, defaultVal) {
  const n = parseInt(val, 10);
  return Number.isFinite(n) ? n : defaultVal;
}

/**
 * Wrap async route handlers so errors propagate to Express error handler.
 */
function asyncHandler(fn) {
  return (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);
}

// ---------------------------------------------------------------------------
// In-memory mutable config (a superset of the static config.js values)
// These are the thresholds and settings that can be patched at runtime.
// ---------------------------------------------------------------------------
const _runtimeConfig = {
  reid_threshold:    config.REID_THRESHOLD,
  reentry_threshold: config.REENTRY_THRESHOLD,
  reentry_window:    config.REENTRY_WINDOW_SECONDS,
  simulate_interval_ms: config.SIMULATE_INTERVAL_MS,
  cameras: config.CAMERAS,
};

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

/**
 * GET /api/health
 * System health check.
 */
router.get('/health', (req, res) => {
  const metrics = metricsStore.getMetrics();
  res.json({
    status: 'ok',
    uptime: metrics.uptime_seconds,
    mode: reidClient.isConnected() ? 'live' : 'demo',
    python_connected: reidClient.isConnected(),
    version: config.VERSION,
    timestamp: new Date().toISOString(),
  });
});

/**
 * GET /api/metrics
 * Current in-memory metrics snapshot.
 */
router.get('/metrics', (req, res) => {
  res.json(metricsStore.getMetrics());
});

/**
 * GET /api/metrics/history?hours=24
 * Historical metric snapshots from SQLite.
 */
router.get('/metrics/history', asyncHandler(async (req, res) => {
  const hours = qInt(req.query.hours, 24);
  const history = db.getMetricsHistory(hours);
  res.json({ hours, data: history });
}));

/**
 * GET /api/events?limit=100&type=NEW_VISITOR&since=<unix_ts_or_iso>
 * Retrieve events from SQLite with optional filters.
 */
router.get('/events', asyncHandler(async (req, res) => {
  const limit = qInt(req.query.limit, 100);
  const { type, since } = req.query;
  const events = db.getEvents({ limit, type: type || null, since: since || null });
  res.json({ count: events.length, events });
}));

/**
 * GET /api/events/timeline?hours=24
 * Events grouped by hour for chart rendering.
 */
router.get('/events/timeline', asyncHandler(async (req, res) => {
  const hours = qInt(req.query.hours, 24);
  const timeline = db.getTimeline(hours);
  res.json({ hours, timeline });
}));

/**
 * GET /api/registry
 * Current active and exited visitor registries.
 */
router.get('/registry', (req, res) => {
  const active = visitorRegistry.getActive();
  const exited = visitorRegistry.getExited();
  const stats = visitorRegistry.getStats();
  res.json({ active, exited, stats });
});

/**
 * POST /api/registry/reset
 * Clear the in-memory registry, event DB, and (if connected) the Python registry.
 */
router.post('/registry/reset', asyncHandler(async (req, res) => {
  visitorRegistry.reset();
  metricsStore.reset();
  db.clearEvents();

  if (reidClient.isConnected()) {
    await reidClient.resetRegistry();
  }

  res.json({
    success: true,
    message: 'Registry, metrics, and event history have been reset.',
    timestamp: new Date().toISOString(),
  });
}));

/**
 * GET /api/cameras
 * Per-camera state objects.
 */
router.get('/cameras', (req, res) => {
  res.json(cameraManager.getCameras());
});

/**
 * GET /api/config
 * Current runtime configuration (thresholds etc.).
 */
router.get('/config', (req, res) => {
  res.json({
    ..._runtimeConfig,
    port: config.PORT,
    python_api_url: config.PYTHON_API_URL,
    mode: reidClient.isConnected() ? 'live' : 'demo',
    version: config.VERSION || '1.0.0',
  });
});

/**
 * PATCH /api/config
 * Update mutable runtime config fields (thresholds, intervals).
 * Allowed fields: REID_THRESHOLD, REENTRY_THRESHOLD, REENTRY_WINDOW_SECONDS,
 *                 SIMULATE_INTERVAL_MS
 */
router.patch('/config', asyncHandler(async (req, res) => {
  const allowed = ['reid_threshold', 'reentry_threshold', 'reentry_window', 'simulate_interval_ms'];

  const updates = {};
  for (const key of allowed) {
    const val = req.body[key];
    if (val !== undefined) {
      const value = parseFloat(val);
      if (!Number.isFinite(value)) {
        return res.status(400).json({ error: `Invalid value for ${key}: must be a number` });
      }
      _runtimeConfig[key] = (key === 'reentry_window' || key === 'simulate_interval_ms')
        ? Math.round(value)
        : value;
      updates[key] = _runtimeConfig[key];
    }
  }

  // Also accept PATCH body with old uppercase keys from Python API
  if (req.body.REID_THRESHOLD !== undefined)    _runtimeConfig.reid_threshold    = parseFloat(req.body.REID_THRESHOLD);
  if (req.body.REENTRY_THRESHOLD !== undefined) _runtimeConfig.reentry_threshold = parseFloat(req.body.REENTRY_THRESHOLD);

  // Push to Python backend if connected
  if (reidClient.isConnected() && Object.keys(updates).length) {
    await reidClient.updateConfig(updates);
  }

  res.json({ success: true, updated: updates, config: _runtimeConfig });
}));

// ---------------------------------------------------------------------------
// 404 handler for unmatched /api/* routes
// ---------------------------------------------------------------------------
router.use((req, res) => {
  res.status(404).json({ error: `API route not found: ${req.method} ${req.path}` });
});

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

// Also export _runtimeConfig so server.js / socket handlers can read it
module.exports = router;
module.exports.getRuntimeConfig = () => ({ ..._runtimeConfig });
