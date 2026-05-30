'use strict';

/**
 * eventSimulator.js
 * ------------------
 * Produces realistic ReID events for DEMO MODE.
 *
 * Simulates a retail store with ENTRANCE, AISLE_1, and CHECKOUT cameras.
 * Maintains a pool of visitor UUIDs and emits events that follow the real
 * ReID event schema understood by the rest of the system.
 *
 * Event mix (approximate):
 *   60% NEW_VISITOR
 *   15% CROSS_CAMERA_MATCH
 *   15% VISITOR_EXITED
 *   10% REENTRY
 */

const { v4: uuidv4 } = require('uuid');
const config = require('../config');

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let _timer = null;
let _onEvent = null;

/** Visitors currently "inside" the store */
const _activePool = new Map(); // visitor_id → { camera_id, track_id }

/** Visitors who have exited (candidates for re-entry) */
const _exitedPool = new Map(); // visitor_id → { camera_id, track_id, exited_at }

/** Running counter for sequential track IDs */
let _nextTrackId = 1;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const CAMERAS = config.CAMERAS;

function _randCamera(exclude = null) {
  const choices = exclude ? CAMERAS.filter((c) => c !== exclude) : CAMERAS;
  return choices[Math.floor(Math.random() * choices.length)];
}

function _randFloat(min, max) {
  return parseFloat((Math.random() * (max - min) + min).toFixed(4));
}

function _randInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

function _randBbox() {
  const x1 = _randInt(10, 400);
  const y1 = _randInt(10, 300);
  const x2 = x1 + _randInt(60, 180);
  const y2 = y1 + _randInt(150, 350);
  return [x1, y1, x2, y2];
}

function _timestamp() {
  return Date.now() / 1000;
}

/** Pick a random key from a Map */
function _randomKey(map) {
  const keys = Array.from(map.keys());
  if (!keys.length) return null;
  return keys[Math.floor(Math.random() * keys.length)];
}

// ---------------------------------------------------------------------------
// Event factories
// ---------------------------------------------------------------------------

function _makeNewVisitor() {
  const visitor_id = `V-${uuidv4().substring(0, 8).toUpperCase()}`;
  const camera_id = _randCamera();
  const track_id = _nextTrackId++;

  _activePool.set(visitor_id, { camera_id, track_id });

  return {
    event_id: uuidv4(),
    event_type: 'NEW_VISITOR',
    visitor_id,
    camera_id,
    track_id,
    reid_confidence: 1.0,
    timestamp: _timestamp(),
    bbox: _randBbox(),
  };
}

function _makeCrossCamera() {
  const visitor_id = _randomKey(_activePool);
  if (!visitor_id) return _makeNewVisitor(); // fallback

  const current = _activePool.get(visitor_id);
  const new_camera = _randCamera(current.camera_id);
  const track_id = _nextTrackId++;

  _activePool.set(visitor_id, { camera_id: new_camera, track_id });

  return {
    event_id: uuidv4(),
    event_type: 'CROSS_CAMERA_MATCH',
    visitor_id,
    camera_id: new_camera,
    track_id,
    reid_confidence: _randFloat(0.82, 0.99),
    timestamp: _timestamp(),
    bbox: _randBbox(),
    previous_camera: current.camera_id,
  };
}

function _makeVisitorExited() {
  const visitor_id = _randomKey(_activePool);
  if (!visitor_id) return _makeNewVisitor(); // no one to exit

  const current = _activePool.get(visitor_id);
  _activePool.delete(visitor_id);
  _exitedPool.set(visitor_id, { ...current, exited_at: _timestamp() });

  return {
    event_id: uuidv4(),
    event_type: 'VISITOR_EXITED',
    visitor_id,
    camera_id: current.camera_id,
    track_id: current.track_id,
    reid_confidence: 1.0,
    timestamp: _timestamp(),
  };
}

function _makeReentry() {
  const visitor_id = _randomKey(_exitedPool);
  if (!visitor_id) return _makeNewVisitor(); // no one to re-enter

  const prev = _exitedPool.get(visitor_id);
  const camera_id = _randCamera();
  const track_id = _nextTrackId++;

  _exitedPool.delete(visitor_id);
  _activePool.set(visitor_id, { camera_id, track_id });

  return {
    event_id: uuidv4(),
    event_type: 'REENTRY',
    visitor_id,
    camera_id,
    track_id,
    reid_confidence: _randFloat(0.80, 0.97),
    timestamp: _timestamp(),
    bbox: _randBbox(),
    previous_camera: prev.camera_id,
    time_away_seconds: Math.floor(_timestamp() - prev.exited_at),
  };
}

// ---------------------------------------------------------------------------
// Scheduler
// ---------------------------------------------------------------------------

/**
 * Pre-populate the active pool with a few visitors so the dashboard isn't
 * empty on startup.
 */
function _seed(count = 5) {
  for (let i = 0; i < count; i++) {
    const visitor_id = `V-${uuidv4().substring(0, 8).toUpperCase()}`;
    const camera_id = _randCamera();
    const track_id = _nextTrackId++;
    _activePool.set(visitor_id, { camera_id, track_id });
  }
}

/**
 * Pick the next event type based on weighted probabilities and current pool
 * state.
 */
function _pickEventType() {
  const hasActive = _activePool.size > 0;
  const hasExited = _exitedPool.size > 0;

  // Keep pool size reasonable (2-18 active visitors)
  if (_activePool.size > 18) {
    return 'VISITOR_EXITED';
  }
  if (_activePool.size < 2) {
    return 'NEW_VISITOR';
  }

  const roll = Math.random();

  if (roll < 0.60) return 'NEW_VISITOR';
  if (roll < 0.75) return hasActive ? 'CROSS_CAMERA_MATCH' : 'NEW_VISITOR';
  if (roll < 0.90) return hasActive ? 'VISITOR_EXITED' : 'NEW_VISITOR';
  return hasExited ? 'REENTRY' : 'NEW_VISITOR';
}

function _generateEvent() {
  const type = _pickEventType();

  switch (type) {
    case 'NEW_VISITOR':       return _makeNewVisitor();
    case 'CROSS_CAMERA_MATCH': return _makeCrossCamera();
    case 'VISITOR_EXITED':    return _makeVisitorExited();
    case 'REENTRY':           return _makeReentry();
    default:                  return _makeNewVisitor();
  }
}

/** Randomise next tick interval between SIMULATE_INTERVAL_MS * 0.75 and * 1.5 */
function _scheduleNext() {
  const base = config.SIMULATE_INTERVAL_MS;
  const jitter = base * 0.75 + Math.random() * base * 0.75; // 0.75x – 1.5x
  _timer = setTimeout(() => {
    if (_onEvent) {
      try {
        const event = _generateEvent();
        _onEvent(event);
      } catch (err) {
        console.error('[Simulator] Error generating event:', err.message);
      }
    }
    _scheduleNext();
  }, jitter);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Start the simulator.
 * @param {function(object): void} onEvent - Callback invoked with each generated event.
 */
function start(onEvent) {
  if (_timer) return; // already running
  _onEvent = onEvent;
  _seed(5);
  _scheduleNext();
  console.log('[Simulator] Started – DEMO MODE active');
}

/**
 * Stop the simulator.
 */
function stop() {
  if (_timer) {
    clearTimeout(_timer);
    _timer = null;
  }
  _onEvent = null;
  console.log('[Simulator] Stopped');
}

/**
 * @returns {boolean} Whether the simulator is currently running.
 */
function isRunning() {
  return _timer !== null;
}

/**
 * Return current pool sizes (useful for diagnostics).
 */
function getPoolStats() {
  return {
    active_in_pool: _activePool.size,
    exited_in_pool: _exitedPool.size,
  };
}

module.exports = { start, stop, isRunning, getPoolStats };
