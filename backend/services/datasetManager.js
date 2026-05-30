'use strict';
/**
 * backend/services/datasetManager.js
 * ────────────────────────────────────
 * Tracks the lifecycle of uploaded datasets and their processing state.
 * All state is in-memory; no native modules needed.
 */

const { v4: uuidv4 } = require('uuid');
const path  = require('path');
const fs    = require('fs');

// ── Storage ──────────────────────────────────────────────────────────
/** @type {Map<string, DatasetRecord>} */
const _datasets = new Map();

/**
 * @typedef {Object} DatasetRecord
 * @property {string}  id
 * @property {string}  name           - Original filename
 * @property {string}  zipPath        - Absolute path to the uploaded ZIP
 * @property {string}  status         - 'uploaded'|'detecting'|'processing'|'done'|'error'
 * @property {string}  format         - Detected format (or null)
 * @property {string[]} cameras       - Camera IDs found
 * @property {number}  uploadedAt     - unix ms
 * @property {number|null} startedAt
 * @property {number|null} finishedAt
 * @property {Object}  progress       - { [camera_id]: { frames_done, total_frames, pct } }
 * @property {Object}  metrics        - Final summary from Python
 * @property {string}  error          - Error message (if status=error)
 * @property {number}  eventCount     - Total events ingested from this dataset
 */

// ── CRUD ─────────────────────────────────────────────────────────────

function createDataset(filename, zipPath) {
  const id = uuidv4();
  const rec = {
    id,
    name:       filename,
    zipPath,
    status:     'uploaded',
    format:     null,
    cameras:    [],
    uploadedAt: Date.now(),
    startedAt:  null,
    finishedAt: null,
    progress:   {},
    metrics:    {},
    error:      null,
    eventCount: 0,
  };
  _datasets.set(id, rec);
  return rec;
}

function getDataset(id) {
  return _datasets.get(id) || null;
}

function listDatasets() {
  return Array.from(_datasets.values()).sort((a, b) => b.uploadedAt - a.uploadedAt);
}

function updateDataset(id, patch) {
  const rec = _datasets.get(id);
  if (!rec) return null;
  Object.assign(rec, patch);
  return rec;
}

function updateProgress(id, cameraId, framesData) {
  const rec = _datasets.get(id);
  if (!rec) return null;
  rec.progress[cameraId] = { ...rec.progress[cameraId], ...framesData };
  // Overall pct = average of all cameras
  const cams = Object.values(rec.progress);
  if (cams.length) {
    rec.progress._overall_pct = Math.round(
      cams.reduce((s, c) => s + (c.pct || 0), 0) / cams.length
    );
  }
  return rec;
}

function addEvents(id, events) {
  const rec = _datasets.get(id);
  if (!rec) return 0;
  rec.eventCount += events.length;
  return rec.eventCount;
}

function deleteDataset(id) {
  const rec = _datasets.get(id);
  if (!rec) return false;
  // Clean up ZIP file
  try { if (fs.existsSync(rec.zipPath)) fs.unlinkSync(rec.zipPath); } catch (_) {}
  _datasets.delete(id);
  return true;
}

// ── Upload directory ─────────────────────────────────────────────────
const UPLOAD_DIR = path.join(__dirname, '..', 'uploads');

function ensureUploadDir() {
  if (!fs.existsSync(UPLOAD_DIR)) fs.mkdirSync(UPLOAD_DIR, { recursive: true });
  return UPLOAD_DIR;
}

module.exports = {
  createDataset,
  getDataset,
  listDatasets,
  updateDataset,
  updateProgress,
  addEvents,
  deleteDataset,
  ensureUploadDir,
  UPLOAD_DIR,
};
