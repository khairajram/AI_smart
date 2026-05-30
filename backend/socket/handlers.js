'use strict';
/**
 * socket/handlers.js
 * ──────────────────
 * Socket.io connection lifecycle handler.
 * Manages per-client subscriptions and broadcasts real-time data.
 */

const config = require('../config');

let _io = null;
let _deps = {};

/**
 * Setup socket.io handlers.
 * @param {import('socket.io').Server} io
 * @param {{ metricsStore, cameraManager, visitorRegistry, db }} deps
 */
function setupSocketHandlers(io, deps) {
  _io = io;
  _deps = deps;

  io.on('connection', (socket) => {
    const clientId = socket.id;
    console.log(`[Socket] Client connected: ${clientId}`);

    // ── Immediate snapshot on connect ──────────────────────────────
    try {
      socket.emit('metrics_update', deps.metricsStore.getMetrics());
      socket.emit('camera_update', deps.cameraManager.getCameras());
      socket.emit('registry_snapshot', {
        active: deps.visitorRegistry.getActive(),
        exited: deps.visitorRegistry.getExited(),
      });
      socket.emit('config_update', {
        reid_threshold:       config.REID_THRESHOLD,
        reentry_threshold:    config.REENTRY_THRESHOLD,
        reentry_window:       config.REENTRY_WINDOW_SECONDS,
        cameras:              config.CAMERAS,
        simulate_interval_ms: config.SIMULATE_INTERVAL_MS,
      });
      socket.emit('mode_change', {
        mode: config.DEMO_MODE ? 'demo' : 'live',
        python_connected: !config.DEMO_MODE,
      });
    } catch (err) {
      console.error('[Socket] Error sending initial snapshot:', err.message);
    }

    // ── Client → Server events ──────────────────────────────────────

    socket.on('get_registry', () => {
      try {
        socket.emit('registry_snapshot', {
          active: deps.visitorRegistry.getActive(),
          exited: deps.visitorRegistry.getExited(),
        });
      } catch (err) {
        console.error('[Socket] get_registry error:', err.message);
      }
    });

    socket.on('reset_registry', () => {
      try {
        deps.visitorRegistry.reset();
        deps.metricsStore.reset();
        deps.cameraManager.initCameras(config.CAMERAS);
        io.emit('registry_snapshot', { active: [], exited: [] });
        io.emit('metrics_update', deps.metricsStore.getMetrics());
        io.emit('camera_update', deps.cameraManager.getCameras());
        socket.emit('registry_reset_ack', { success: true, timestamp: Date.now() });
        console.log('[Socket] Registry reset by client:', clientId);
      } catch (err) {
        console.error('[Socket] reset_registry error:', err.message);
        socket.emit('registry_reset_ack', { success: false, error: err.message });
      }
    });

    socket.on('update_config', (data) => {
      try {
        if (data.reid_threshold !== undefined)    config.REID_THRESHOLD = parseFloat(data.reid_threshold);
        if (data.reentry_threshold !== undefined) config.REENTRY_THRESHOLD = parseFloat(data.reentry_threshold);
        if (data.reentry_window !== undefined)    config.REENTRY_WINDOW_SECONDS = parseInt(data.reentry_window);
        const updated = {
          reid_threshold:    config.REID_THRESHOLD,
          reentry_threshold: config.REENTRY_THRESHOLD,
          reentry_window:    config.REENTRY_WINDOW_SECONDS,
        };
        io.emit('config_update', updated);
        console.log('[Socket] Config updated:', updated);
      } catch (err) {
        console.error('[Socket] update_config error:', err.message);
      }
    });

    socket.on('disconnect', (reason) => {
      console.log(`[Socket] Client disconnected: ${clientId} — ${reason}`);
    });

    socket.on('error', (err) => {
      console.error(`[Socket] Client error (${clientId}):`, err.message);
    });
  });
}

/**
 * Broadcast a ReID event to all connected clients.
 */
function broadcastEvent(event) {
  if (_io) _io.emit('reid_event', event);
}

/**
 * Broadcast metrics to all connected clients.
 */
function broadcastMetrics(metrics) {
  if (_io) _io.emit('metrics_update', metrics);
}

/**
 * Broadcast camera status to all connected clients.
 */
function broadcastCameras(cameras) {
  if (_io) _io.emit('camera_update', cameras);
}

/**
 * Broadcast registry snapshot to all connected clients.
 */
function broadcastRegistry(active, exited) {
  if (_io) _io.emit('registry_snapshot', { active, exited });
}

/**
 * Broadcast mode change (demo ↔ live).
 */
function broadcastModeChange(mode, pythonConnected) {
  if (_io) _io.emit('mode_change', { mode, python_connected: pythonConnected });
}

module.exports = {
  setupSocketHandlers,
  broadcastEvent,
  broadcastMetrics,
  broadcastCameras,
  broadcastRegistry,
  broadcastModeChange,
};
