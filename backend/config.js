'use strict';

require('dotenv').config();

/**
 * Central application configuration.
 * All values are read from environment variables with safe defaults.
 */
const config = {
  // Server
  PORT: parseInt(process.env.PORT, 10) || 3000,

  // Python FastAPI backend
  PYTHON_API_URL: process.env.PYTHON_API_URL || 'http://localhost:8000',

  // When true the event simulator runs instead of the live Python API
  DEMO_MODE: process.env.DEMO_MODE !== 'false', // default true

  // SQLite database path (relative to project root or absolute)
  DB_PATH: process.env.DB_PATH || './reid_events.db',

  // How often (ms) the simulator fires a new event
  SIMULATE_INTERVAL_MS: parseInt(process.env.SIMULATE_INTERVAL_MS, 10) || 2000,

  // Camera IDs recognised by the system
  CAMERAS: process.env.CAMERAS
    ? process.env.CAMERAS.split(',').map((c) => c.trim())
    : ['ENTRANCE', 'AISLE_1', 'CHECKOUT'],

  // ReID / re-entry matching thresholds
  REID_THRESHOLD: parseFloat(process.env.REID_THRESHOLD) || 0.82,
  REENTRY_THRESHOLD: parseFloat(process.env.REENTRY_THRESHOLD) || 0.80,

  // Seconds within which a re-entry counts as a re-entry vs a new visit
  REENTRY_WINDOW_SECONDS: parseInt(process.env.REENTRY_WINDOW_SECONDS, 10) || 300,

  // Metrics broadcast intervals (ms)
  METRICS_BROADCAST_INTERVAL_MS: parseInt(process.env.METRICS_BROADCAST_INTERVAL_MS, 10) || 2000,
  SNAPSHOT_INTERVAL_MS: parseInt(process.env.SNAPSHOT_INTERVAL_MS, 10) || 10000,

  // How often to poll the Python API for metrics (ms)
  PYTHON_POLL_INTERVAL_MS: parseInt(process.env.PYTHON_POLL_INTERVAL_MS, 10) || 2000,

  // Application version – bumped manually on releases
  VERSION: '1.0.0',
};

module.exports = config;
