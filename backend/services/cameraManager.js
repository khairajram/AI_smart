'use strict';

/**
 * cameraManager.js
 * -----------------
 * Tracks per-camera state for the ReID dashboard.
 * State is held purely in memory; the frontend is kept up-to-date via
 * periodic Socket.io broadcasts.
 */

const config = require('../config');

// ---------------------------------------------------------------------------
// Type definition (JSDoc only – no runtime overhead)
// ---------------------------------------------------------------------------
/**
 * @typedef {object} CameraState
 * @property {string}      camera_id
 * @property {'active'|'inactive'} status
 * @property {number}      active_visitors
 * @property {number}      total_visitors
 * @property {number}      frame_count
 * @property {number}      fps
 * @property {Date|null}   last_event
 * @property {number}      events_today
 */

// ---------------------------------------------------------------------------
// Internal map: camera_id → CameraState
// ---------------------------------------------------------------------------
/** @type {Map<string, CameraState>} */
const _cameras = new Map();

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------
function _defaultState(camera_id) {
  return {
    camera_id,
    status: 'active',
    active_visitors: 0,
    total_visitors: 0,
    frame_count: 0,
    fps: 0,
    last_event: null,
    events_today: 0,
  };
}

// Reset events_today every midnight
function _scheduleDailyReset() {
  const now = new Date();
  const midnight = new Date(now);
  midnight.setHours(24, 0, 0, 0);
  const msUntilMidnight = midnight - now;

  setTimeout(() => {
    for (const cam of _cameras.values()) {
      cam.events_today = 0;
    }
    _scheduleDailyReset(); // reschedule for next midnight
  }, msUntilMidnight);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Initialise cameras from a list of IDs.
 * Existing camera state is preserved if already initialised.
 * @param {string[]} cameraIds
 */
function initCameras(cameraIds) {
  for (const id of cameraIds) {
    if (!_cameras.has(id)) {
      _cameras.set(id, _defaultState(id));
    }
  }
  _scheduleDailyReset();
}

/**
 * Return an array of all camera state objects.
 * @returns {CameraState[]}
 */
function getCameras() {
  return Array.from(_cameras.values());
}

/**
 * Return the state for a single camera, or null if not found.
 * @param {string} id
 * @returns {CameraState|null}
 */
function getCameraById(id) {
  return _cameras.get(id) || null;
}

/**
 * Update a camera's state.
 * Merges `data` onto the existing state; unknown cameras are auto-created.
 *
 * Recognised special fields in `data`:
 *  - `delta_active`   {number} – adjust active_visitors by this signed delta
 *  - `increment_total` {boolean} – increment total_visitors by 1
 *  - `increment_events` {boolean} – increment events_today by 1
 *
 * All other fields are merged directly.
 *
 * @param {string} camera_id
 * @param {object} data
 */
function updateCamera(camera_id, data) {
  if (!_cameras.has(camera_id)) {
    _cameras.set(camera_id, _defaultState(camera_id));
  }

  const cam = _cameras.get(camera_id);

  // Handle delta adjustments before spreading
  if (typeof data.delta_active === 'number') {
    cam.active_visitors = Math.max(0, cam.active_visitors + data.delta_active);
  }
  if (data.increment_total) {
    cam.total_visitors += 1;
  }
  if (data.increment_events) {
    cam.events_today += 1;
  }

  // Merge the rest (excluding our virtual helper keys)
  const { delta_active, increment_total, increment_events, ...rest } = data;
  Object.assign(cam, rest);

  // Always stamp last_event
  cam.last_event = new Date();
  cam.status = 'active';
}

/**
 * Process a ReID event and update the affected camera's counters.
 * @param {object} event
 */
function handleEvent(event) {
  const { camera_id, event_type } = event;
  if (!camera_id) return;

  switch (event_type) {
    case 'NEW_VISITOR':
    case 'REENTRY':
      updateCamera(camera_id, {
        delta_active: 1,
        increment_total: true,
        increment_events: true,
      });
      break;

    case 'CROSS_CAMERA_MATCH':
      // Visitor moved — the originating camera is not known here, so we just
      // record the event on the destination camera.
      updateCamera(camera_id, { increment_events: true });
      break;

    case 'VISITOR_EXITED':
      updateCamera(camera_id, {
        delta_active: -1,
        increment_events: true,
      });
      break;

    default:
      updateCamera(camera_id, { increment_events: true });
  }
}

/**
 * Mark a camera as inactive (e.g. stream lost).
 * @param {string} camera_id
 */
function markInactive(camera_id) {
  if (_cameras.has(camera_id)) {
    _cameras.get(camera_id).status = 'inactive';
  }
}

module.exports = {
  initCameras,
  getCameras,
  getCameraById,
  updateCamera,
  handleEvent,
  markInactive,
};
