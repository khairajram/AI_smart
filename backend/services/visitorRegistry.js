'use strict';

/**
 * visitorRegistry.js
 * -------------------
 * In-memory registry mirroring the Python ReID tracker's visitor records.
 * Two maps are maintained:
 *   active  – visitors currently present in the store
 *   exited  – visitors who have left (retained for re-entry detection)
 */

// ---------------------------------------------------------------------------
// Storage
// ---------------------------------------------------------------------------

/** @type {Map<string, VisitorRecord>} */
const active = new Map();

/** @type {Map<string, VisitorRecord>} */
const exited = new Map();

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

/**
 * Build a VisitorRecord from a ReID event.
 * @param {object} event
 * @returns {VisitorRecord}
 */
function _recordFromEvent(event) {
  return {
    visitor_id: event.visitor_id,
    camera_id: event.camera_id,
    track_id: event.track_id ?? null,
    reid_confidence: event.reid_confidence ?? 1.0,
    first_seen: event.timestamp ?? Date.now() / 1000,
    last_seen: event.timestamp ?? Date.now() / 1000,
    event_count: 1,
    cameras_visited: [event.camera_id],
    is_reentry: event.event_type === 'REENTRY',
    reentry_count: event.event_type === 'REENTRY' ? 1 : 0,
  };
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Return array of all active visitors.
 * @returns {VisitorRecord[]}
 */
function getActive() {
  return Array.from(active.values());
}

/**
 * Return array of all exited visitors.
 * @returns {VisitorRecord[]}
 */
function getExited() {
  return Array.from(exited.values());
}

/**
 * Insert or update a visitor from an incoming event.
 * Handles NEW_VISITOR, REENTRY, and CROSS_CAMERA_MATCH event types.
 * @param {object} event
 */
function upsertVisitor(event) {
  const { visitor_id, camera_id, timestamp, event_type, reid_confidence } = event;
  const ts = timestamp ?? Date.now() / 1000;

  if (event_type === 'VISITOR_EXITED') {
    // Move from active to exited
    markExited(visitor_id);
    return;
  }

  if (active.has(visitor_id)) {
    // Update existing active record
    const rec = active.get(visitor_id);
    rec.last_seen = ts;
    rec.camera_id = camera_id;
    rec.event_count += 1;
    if (reid_confidence !== undefined) rec.reid_confidence = reid_confidence;

    if (!rec.cameras_visited.includes(camera_id)) {
      rec.cameras_visited.push(camera_id);
    }

    if (event_type === 'REENTRY') {
      rec.reentry_count = (rec.reentry_count || 0) + 1;
      rec.is_reentry = true;
    }
  } else {
    // Create new active record (could be returning from exited)
    const existing = exited.get(visitor_id);
    if (existing) {
      // Re-activate
      existing.camera_id = camera_id;
      existing.last_seen = ts;
      existing.event_count += 1;
      existing.is_reentry = true;
      existing.reentry_count = (existing.reentry_count || 0) + 1;
      if (!existing.cameras_visited.includes(camera_id)) {
        existing.cameras_visited.push(camera_id);
      }
      active.set(visitor_id, existing);
      exited.delete(visitor_id);
    } else {
      // Brand new visitor
      active.set(visitor_id, _recordFromEvent(event));
    }
  }
}

/**
 * Move a visitor from active to exited.
 * If they are already exited or unknown, this is a no-op.
 * @param {string} visitor_id
 */
function markExited(visitor_id) {
  if (active.has(visitor_id)) {
    const rec = active.get(visitor_id);
    rec.exited_at = Date.now() / 1000;
    exited.set(visitor_id, rec);
    active.delete(visitor_id);
  }
}

/**
 * Clear all active and exited records.
 */
function reset() {
  active.clear();
  exited.clear();
}

/**
 * Return aggregate statistics about the registry.
 * @returns {object}
 */
function getStats() {
  const activeArr = getActive();
  const exitedArr = getExited();

  const reentryCount = activeArr.filter((v) => v.is_reentry).length
    + exitedArr.filter((v) => v.is_reentry).length;

  const allCameras = new Set();
  for (const v of [...activeArr, ...exitedArr]) {
    for (const c of v.cameras_visited || []) allCameras.add(c);
  }

  return {
    total_active: active.size,
    total_exited: exited.size,
    total_reentries: reentryCount,
    cameras_with_visitors: Array.from(allCameras),
  };
}

module.exports = {
  getActive,
  getExited,
  upsertVisitor,
  markExited,
  reset,
  getStats,
};
