'use strict';

/**
 * reidClient.js
 * --------------
 * HTTP client for the Python FastAPI ReID backend.
 *
 * On startup, checkConnection() is called. If the backend is not reachable
 * `pythonConnected` is false and DEMO MODE takes over in server.js.
 *
 * All methods return null / empty arrays on failure so callers do not need
 * to handle axios exceptions individually.
 */

const axios = require('axios');
const config = require('../config');

// ---------------------------------------------------------------------------
// Axios instance
// ---------------------------------------------------------------------------
const client = axios.create({
  baseURL: config.PYTHON_API_URL,
  timeout: 5000,
  headers: { 'Content-Type': 'application/json' },
});

// ---------------------------------------------------------------------------
// Internal state
// ---------------------------------------------------------------------------
let _pythonConnected = false;
let _pollTimer = null;

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------
async function _safeGet(url, params = {}) {
  try {
    const res = await client.get(url, { params });
    return res.data;
  } catch (err) {
    _handleError(url, err);
    return null;
  }
}

async function _safePost(url, body = {}) {
  try {
    const res = await client.post(url, body);
    return res.data;
  } catch (err) {
    _handleError(url, err);
    return null;
  }
}

function _handleError(url, err) {
  if (err.code === 'ECONNREFUSED' || err.code === 'ENOTFOUND' || err.code === 'ETIMEDOUT') {
    if (_pythonConnected) {
      console.warn(`[reidClient] Python API unreachable at ${url}. Switching to DEMO MODE.`);
      _pythonConnected = false;
    }
  } else {
    console.error(`[reidClient] Request failed (${url}): ${err.message}`);
  }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Check whether the Python FastAPI backend is reachable.
 * Updates the internal `_pythonConnected` flag.
 * @returns {Promise<boolean>}
 */
async function checkConnection() {
  try {
    await client.get('/health', { timeout: 3000 });
    _pythonConnected = true;
    console.log(`[reidClient] Connected to Python API at ${config.PYTHON_API_URL}`);
  } catch {
    _pythonConnected = false;
    console.warn(`[reidClient] Python API not reachable at ${config.PYTHON_API_URL}. DEMO MODE will be used.`);
  }
  return _pythonConnected;
}

/** @returns {boolean} */
function isConnected() {
  return _pythonConnected;
}

/**
 * Fetch current metrics from the Python backend.
 *
 * NOTE: GET /metrics requires a live VisitorRegistry and returns 503
 * when the API is running standalone (docker-compose without pipeline).
 * We instead call GET /health (always 200) and merge in store-level
 * metrics from GET /stores/{id}/metrics for the first known store.
 *
 * @returns {Promise<object|null>}
 */
async function fetchMetrics() {
  // Health endpoint is always available (no registry dependency)
  const health = await _safeGet('/health');
  if (!health) return null;

  _pythonConnected = true;

  // Build a metrics-like object from health + store metrics
  const storeIds = Object.keys(health.stores || {});
  let storeMetrics = null;
  if (storeIds.length > 0) {
    storeMetrics = await _safeGet(`/stores/${storeIds[0]}/metrics`);
  }

  return {
    // Fields expected by metricsStore.updateFromPythonMetrics()
    active_visitors:        storeMetrics ? storeMetrics.unique_visitors : 0,
    exited_visitors_cached: 0,
    total_unique_visitors:  storeMetrics ? storeMetrics.unique_visitors : 0,
    total_reentries:        storeMetrics ? (storeMetrics.total_reentries || 0) : 0,
    total_cross_camera:     0,
    total_exits_recorded:   storeMetrics ? (storeMetrics.total_exits || 0) : 0,
    // Extra rich fields the dashboard can use
    conversion_rate:        storeMetrics ? storeMetrics.conversion_rate : 0,
    abandonment_rate:       storeMetrics ? storeMetrics.abandonment_rate : 0,
    queue_depth:            storeMetrics ? storeMetrics.queue_depth : null,
    total_events:           health.total_events || 0,
    uptime_seconds:         health.uptime_seconds || 0,
    store_id:               storeIds[0] || null,
  };
}

/**
 * Fetch the active visitor registry.
 * @returns {Promise<object[]|null>}
 */
async function fetchActiveRegistry() {
  const data = await _safeGet('/registry/active');
  return data;
}

/**
 * Fetch the exited visitor registry.
 * @returns {Promise<object[]|null>}
 */
async function fetchExitedRegistry() {
  const data = await _safeGet('/registry/exited');
  return data;
}

/**
 * Send a reset command to the Python backend registry.
 * @returns {Promise<object|null>}
 */
async function resetRegistry() {
  return _safePost('/registry/reset');
}

/**
 * Push updated config (thresholds etc.) to the Python backend.
 * @param {object} configPayload
 * @returns {Promise<object|null>}
 */
async function updateConfig(configPayload) {
  return _safePost('/config', configPayload);
}

/**
 * Poll /metrics every `intervalMs` milliseconds.
 * Calls `onMetrics(data)` with the response. Stops when the API becomes
 * unreachable (transitions back to DEMO MODE).
 *
 * @param {number} intervalMs
 * @param {function(object): void} onMetrics
 */
function pollForever(intervalMs, onMetrics) {
  if (_pollTimer) clearInterval(_pollTimer);

  _pollTimer = setInterval(async () => {
    const data = await fetchMetrics();
    if (data) {
      _pythonConnected = true;
      onMetrics(data);
    } else {
      _pythonConnected = false;
    }
  }, intervalMs);
}

/**
 * Stop the polling loop.
 */
function stopPolling() {
  if (_pollTimer) {
    clearInterval(_pollTimer);
    _pollTimer = null;
  }
}

module.exports = {
  checkConnection,
  isConnected,
  fetchMetrics,
  fetchActiveRegistry,
  fetchExitedRegistry,
  resetRegistry,
  updateConfig,
  pollForever,
  stopPolling,
};
