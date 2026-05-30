'use strict';
/**
 * db.js
 * ─────
 * Pure-JS in-memory event store with optional JSON file persistence.
 * No native modules required — works on any Node.js installation.
 *
 * Data is kept in memory for fast queries and persisted to a JSON file
 * every FLUSH_INTERVAL_MS milliseconds so it survives restarts.
 */

const fs   = require('fs');
const path = require('path');
const config = require('./config');

// ── Storage ──────────────────────────────────────────────────────────
/** @type {object[]} */
let _events = [];
/** @type {object[]} */
let _snapshots = [];

const FLUSH_INTERVAL_MS = 30_000;   // flush to disk every 30 s
const MAX_EVENTS        = 10_000;   // cap in-memory event count
const MAX_SNAPSHOTS     = 2_000;

// ── File paths ───────────────────────────────────────────────────────
const dataDir      = path.join(__dirname, 'data');
const eventsPath   = path.join(dataDir, 'events.json');
const snapshotsPath = path.join(dataDir, 'snapshots.json');

// ── Init ─────────────────────────────────────────────────────────────
function init() {
  try {
    if (!fs.existsSync(dataDir)) fs.mkdirSync(dataDir, { recursive: true });
    if (fs.existsSync(eventsPath))    _events    = JSON.parse(fs.readFileSync(eventsPath,    'utf8'));
    if (fs.existsSync(snapshotsPath)) _snapshots = JSON.parse(fs.readFileSync(snapshotsPath, 'utf8'));
    // Keep only recent data on load
    const cutoff = Date.now() / 1000 - 86400; // last 24h
    _events    = _events.filter(e => e.timestamp >= cutoff).slice(-MAX_EVENTS);
    _snapshots = _snapshots.filter(s => s.timestamp >= cutoff).slice(-MAX_SNAPSHOTS);
    console.log(`[DB] Loaded ${_events.length} events, ${_snapshots.length} snapshots from disk`);
  } catch (err) {
    console.warn('[DB] Could not load persisted data (starting fresh):', err.message);
    _events    = [];
    _snapshots = [];
  }

  // Periodic flush
  setInterval(_flush, FLUSH_INTERVAL_MS).unref();
}

function _flush() {
  try {
    fs.writeFileSync(eventsPath,    JSON.stringify(_events,    null, 0), 'utf8');
    fs.writeFileSync(snapshotsPath, JSON.stringify(_snapshots, null, 0), 'utf8');
  } catch (err) {
    console.warn('[DB] Flush error:', err.message);
  }
}

function close() {
  _flush(); // final flush on shutdown
}

// ── Events ────────────────────────────────────────────────────────────

/**
 * Insert a ReID event. Silently ignores duplicates by event_id.
 * @param {object} event
 */
function insertEvent(event) {
  // Dedup check
  if (_events.some(e => e.event_id === event.event_id)) return;

  const record = {
    id: _events.length + 1,
    event_id:       event.event_id,
    event_type:     event.event_type,
    visitor_id:     event.visitor_id,
    camera_id:      event.camera_id,
    track_id:       event.track_id ?? 0,
    reid_confidence: event.reid_confidence ?? 0,
    timestamp:      typeof event.timestamp === 'number' ? event.timestamp : Date.now() / 1000,
    created_at:     Math.floor(Date.now() / 1000),
    // Preserve any extra fields
    ...(event.bbox && { bbox: event.bbox }),
    ...(event.previous_camera && { previous_camera: event.previous_camera }),
  };

  _events.push(record);

  // Cap memory
  if (_events.length > MAX_EVENTS) {
    _events = _events.slice(-MAX_EVENTS);
  }
}

/**
 * Retrieve events with optional filtering.
 * @param {{ limit?: number, type?: string|null, since?: number|string|null }} opts
 * @returns {object[]}
 */
function getEvents({ limit = 100, type = null, since = null } = {}) {
  let result = _events;

  if (type) {
    result = result.filter(e => e.event_type === type);
  }

  if (since !== null) {
    const sinceTs = typeof since === 'number' ? since : new Date(since).getTime() / 1000;
    result = result.filter(e => e.timestamp >= sinceTs);
  }

  const safeLimit = Math.min(Math.max(parseInt(limit, 10) || 100, 1), 5000);
  // Return newest first
  return [...result].reverse().slice(0, safeLimit);
}

/**
 * Return events grouped by hour for the timeline chart.
 * @param {number} hours
 * @returns {{ hour: string, total: number, NEW_VISITOR: number, REENTRY: number, CROSS_CAMERA_MATCH: number, VISITOR_EXITED: number }[]}
 */
function getTimeline(hours = 24) {
  const sinceTs = Date.now() / 1000 - hours * 3600;
  const relevant = _events.filter(e => e.timestamp >= sinceTs);

  /** @type {Map<string, object>} */
  const byHour = new Map();

  for (const ev of relevant) {
    const d    = new Date(ev.timestamp * 1000);
    const hour = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}T${String(d.getHours()).padStart(2,'0')}:00:00`;

    if (!byHour.has(hour)) {
      byHour.set(hour, { hour, total: 0, NEW_VISITOR: 0, CROSS_CAMERA_MATCH: 0, REENTRY: 0, VISITOR_EXITED: 0 });
    }
    const bucket = byHour.get(hour);
    bucket.total++;
    if (bucket[ev.event_type] !== undefined) bucket[ev.event_type]++;
  }

  return Array.from(byHour.values()).sort((a, b) => a.hour < b.hour ? -1 : 1);
}

// ── Metric snapshots ──────────────────────────────────────────────────

/**
 * Persist a metric snapshot.
 * @param {object} metrics
 */
function insertMetricSnapshot(metrics) {
  _snapshots.push({
    id:              _snapshots.length + 1,
    timestamp:       Math.floor(Date.now() / 1000),
    active_visitors: metrics.active_visitors  || 0,
    unique_visitors: metrics.total_unique_visitors || 0,
    reentries:       metrics.total_reentries  || 0,
    cross_camera:    metrics.total_cross_camera || 0,
    exits:           metrics.total_exits_recorded || 0,
  });
  if (_snapshots.length > MAX_SNAPSHOTS) {
    _snapshots = _snapshots.slice(-MAX_SNAPSHOTS);
  }
}

/**
 * Return metric snapshots for the last N hours.
 * @param {number} hours
 * @returns {object[]}
 */
function getMetricsHistory(hours = 24) {
  const sinceTs = Math.floor(Date.now() / 1000) - hours * 3600;
  return _snapshots.filter(s => s.timestamp >= sinceTs);
}

/**
 * Delete all events (used on registry reset).
 */
function clearEvents() {
  _events = [];
}

module.exports = { init, close, insertEvent, getEvents, getTimeline, insertMetricSnapshot, getMetricsHistory, clearEvents };
