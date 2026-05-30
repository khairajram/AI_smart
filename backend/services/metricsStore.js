'use strict';

/**
 * metricsStore.js
 * ----------------
 * In-memory metrics store for the ReID dashboard.
 * Persisting to SQLite is handled externally by the snapshot scheduler in
 * server.js (calls insertMetricSnapshot).
 */

const config = require('../config');

// ---------------------------------------------------------------------------
// Internal state
// ---------------------------------------------------------------------------
const _startTime = Date.now();

/** @type {Map<string, number>} Timestamps (ms) of recent events for EPM calc */
const _recentEventTimes = [];
const EPM_WINDOW_MS = 60_000; // 1 minute

const _state = {
  active_visitors: 0,
  total_unique_visitors: 0,
  total_reentries: 0,
  total_cross_camera: 0,
  total_exits_recorded: 0,
  events_per_minute: 0,
  uptime_seconds: 0,
  start_time: _startTime,
};

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------
function _recordEventTime() {
  const now = Date.now();
  _recentEventTimes.push(now);
  // Prune old entries outside the 1-minute window
  while (_recentEventTimes.length && _recentEventTimes[0] < now - EPM_WINDOW_MS) {
    _recentEventTimes.shift();
  }
  _state.events_per_minute = _recentEventTimes.length;
}

function _updateUptime() {
  _state.uptime_seconds = Math.floor((Date.now() - _startTime) / 1000);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Return a shallow copy of the current metrics object.
 */
function getMetrics() {
  _updateUptime();
  return { ..._state };
}

/**
 * Update metrics based on an incoming ReID event.
 * @param {object} event - A ReID event with at least `event_type` field.
 */
function updateFromEvent(event) {
  _recordEventTime();

  switch (event.event_type) {
    case 'NEW_VISITOR':
      _state.total_unique_visitors += 1;
      _state.active_visitors = Math.max(0, _state.active_visitors + 1);
      break;

    case 'REENTRY':
      _state.total_reentries += 1;
      _state.total_unique_visitors += 1; // each re-entry is a new "session"
      _state.active_visitors = Math.max(0, _state.active_visitors + 1);
      break;

    case 'CROSS_CAMERA_MATCH':
      _state.total_cross_camera += 1;
      // No change to active visitor count — same person, different camera
      break;

    case 'VISITOR_EXITED':
      _state.total_exits_recorded += 1;
      _state.active_visitors = Math.max(0, _state.active_visitors - 1);
      break;

    default:
      // Unknown type – still record the event time for EPM
      break;
  }
}

/**
 * Merge metrics received directly from the Python FastAPI backend.
 * Fields present on the Python payload overwrite local state.
 * @param {object} data - Metrics payload from /metrics endpoint
 */
function updateFromPythonMetrics(data) {
  if (!data || typeof data !== 'object') return;

  const fieldMap = {
    active_visitors: 'active_visitors',
    total_unique_visitors: 'total_unique_visitors',
    total_reentries: 'total_reentries',
    total_cross_camera: 'total_cross_camera',
    total_exits_recorded: 'total_exits_recorded',
  };

  for (const [pyField, localField] of Object.entries(fieldMap)) {
    if (typeof data[pyField] === 'number') {
      _state[localField] = data[pyField];
    }
  }

  _updateUptime();
}

/**
 * Directly increment a counter field by 1.
 * Useful for manual adjustments (e.g. from registry reset).
 * @param {string} field - Key in _state to increment
 * @param {number} [by=1]
 */
function incrementCounter(field, by = 1) {
  if (typeof _state[field] === 'number') {
    _state[field] = Math.max(0, _state[field] + by);
  }
}

/**
 * Hard reset all counters to zero (called on registry reset).
 */
function reset() {
  _state.active_visitors = 0;
  _state.total_unique_visitors = 0;
  _state.total_reentries = 0;
  _state.total_cross_camera = 0;
  _state.total_exits_recorded = 0;
  _state.events_per_minute = 0;
  _recentEventTimes.length = 0;
}

module.exports = {
  init: () => { /* no-op — state initialised at module load */ },
  getMetrics,
  updateFromEvent,
  updateFromPythonMetrics,
  incrementCounter,
  reset,
};
