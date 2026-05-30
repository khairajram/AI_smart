'use strict';
/**
 * backend/routes/dataset.js
 * ──────────────────────────
 * Dataset management REST endpoints:
 *
 *   POST   /api/dataset/upload           Upload a ZIP dataset
 *   GET    /api/dataset                  List all datasets
 *   GET    /api/dataset/:id              Get single dataset record
 *   POST   /api/dataset/:id/process      Kick off Python processor
 *   POST   /api/dataset/:id/progress     Python processor reports progress (internal)
 *   POST   /api/dataset/ingest           Ingest batch of events from Python
 *   DELETE /api/dataset/:id              Delete a dataset
 */

const express  = require('express');
const multer   = require('multer');
const path     = require('path');
const { spawn }= require('child_process');
const fs       = require('fs');

const router        = express.Router();
const datasetMgr    = require('../services/datasetManager');
const db            = require('../db');
const metricsStore  = require('../services/metricsStore');
const visitorRegistry = require('../services/visitorRegistry');
const cameraManager = require('../services/cameraManager');

// ── Multer config ────────────────────────────────────────────────────
const storage = multer.diskStorage({
  destination: (req, file, cb) => {
    cb(null, datasetMgr.ensureUploadDir());
  },
  filename: (req, file, cb) => {
    const safe = file.originalname.replace(/[^a-zA-Z0-9._-]/g, '_');
    cb(null, `${Date.now()}_${safe}`);
  },
});

const upload = multer({
  storage,
  limits: { fileSize: 2 * 1024 * 1024 * 1024 },  // 2 GB max
  fileFilter: (req, file, cb) => {
    if (file.mimetype === 'application/zip' ||
        file.originalname.toLowerCase().endsWith('.zip')) {
      cb(null, true);
    } else {
      cb(new Error('Only ZIP files are accepted'));
    }
  },
});

// ── Helpers ───────────────────────────────────────────────────────────
let _io = null;   // injected by server.js

function setIO(io) { _io = io; }

function broadcast(event, data) {
  if (_io) _io.emit(event, data);
}

function asyncH(fn) {
  return (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);
}

// ── POST /api/dataset/upload ─────────────────────────────────────────
router.post('/upload', upload.single('dataset'), asyncH(async (req, res) => {
  if (!req.file) {
    return res.status(400).json({ error: 'No ZIP file provided. Use field name "dataset".' });
  }

  const rec = datasetMgr.createDataset(req.file.originalname, req.file.path);

  broadcast('dataset_update', { type: 'uploaded', dataset: _sanitize(rec) });

  res.status(201).json({
    success: true,
    dataset: _sanitize(rec),
    next: `POST /api/dataset/${rec.id}/process  to start processing`,
  });
}));

// ── GET /api/dataset ──────────────────────────────────────────────────
router.get('/', (req, res) => {
  res.json(datasetMgr.listDatasets().map(_sanitize));
});

// ── GET /api/dataset/:id ──────────────────────────────────────────────
router.get('/:id', (req, res) => {
  const rec = datasetMgr.getDataset(req.params.id);
  if (!rec) return res.status(404).json({ error: 'Dataset not found' });
  res.json(_sanitize(rec));
});

// ── POST /api/dataset/:id/process ────────────────────────────────────
router.post('/:id/process', asyncH(async (req, res) => {
  const rec = datasetMgr.getDataset(req.params.id);
  if (!rec)              return res.status(404).json({ error: 'Dataset not found' });
  if (rec.status === 'processing')
    return res.status(409).json({ error: 'Already processing' });

  const {
    dashboard_url  = `http://localhost:${process.env.PORT || 3000}`,
    fps            = 10,
    max_frames     = null,
    camera_map     = null,    // "cam1:ENTRANCE,cam2:AISLE_1"
    output_jsonl   = null,
  } = req.body || {};

  // Python executable — prefer project-venv, fall back to system python
  const pythonExe = _findPython();
  const scriptPath = path.join(__dirname, '..', '..', 'tools', 'dataset_processor.py');

  const args = [
    scriptPath,
    '--zip',          rec.zipPath,
    '--dashboard-url', dashboard_url,
    '--fps',          String(fps),
    '--dataset-id',   rec.id,
  ];
  if (max_frames)  args.push('--max-frames', String(max_frames));
  if (camera_map)  args.push('--camera-map', camera_map);
  if (output_jsonl) args.push('--output', output_jsonl);

  datasetMgr.updateDataset(rec.id, { status: 'processing', startedAt: Date.now() });
  broadcast('dataset_update', { type: 'processing_started', dataset: _sanitize(rec) });

  // Launch Python non-blocking
  const proc = spawn(pythonExe, args, {
    cwd: path.join(__dirname, '..', '..'),
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  let stdout = '', stderr = '';
  proc.stdout.on('data', d => { stdout += d; _broadcastLog(rec.id, d.toString()); });
  proc.stderr.on('data', d => { stderr += d; });

  proc.on('close', code => {
    if (code === 0) {
      datasetMgr.updateDataset(rec.id, { status: 'done', finishedAt: Date.now() });
      broadcast('dataset_update', { type: 'done', dataset: _sanitize(datasetMgr.getDataset(rec.id)) });
    } else {
      const errMsg = stderr.slice(-500) || `Exit code ${code}`;
      datasetMgr.updateDataset(rec.id, { status: 'error', error: errMsg, finishedAt: Date.now() });
      broadcast('dataset_update', { type: 'error', dataset: _sanitize(datasetMgr.getDataset(rec.id)) });
    }
  });

  res.json({ success: true, dataset_id: rec.id, pid: proc.pid });
}));

// ── POST /api/dataset/:id/progress  (Python → Node) ──────────────────
router.post('/:id/progress', asyncH(async (req, res) => {
  const { camera_id, frames_done, total_frames, pct, status, ...rest } = req.body || {};
  const rec = datasetMgr.updateProgress(req.params.id, camera_id || '_all', {
    frames_done, total_frames, pct, status,
  });
  if (!rec) return res.status(404).json({ error: 'Dataset not found' });

  if (status === 'done') {
    datasetMgr.updateDataset(req.params.id, { metrics: rest, finishedAt: Date.now(), status: 'done' });
  }

  broadcast('dataset_progress', { dataset_id: req.params.id, progress: rec.progress, metrics: rec.metrics });
  res.json({ ok: true });
}));

// ── POST /api/dataset/ingest  (Python → Node, batch events) ──────────
router.post('/ingest', asyncH(async (req, res) => {
  const { events = [], dataset_id } = req.body || {};
  if (!Array.isArray(events) || !events.length) {
    return res.status(400).json({ error: 'events must be a non-empty array' });
  }

  events.forEach(event => {
    try {
      // Persist + update live state exactly like the real event pipeline
      db.insertEvent(event);
      metricsStore.updateFromEvent(event);
      visitorRegistry.upsertVisitor(event);
      let eventDate;
      if (typeof event.timestamp === 'number') {
        // Pipeline sends UNIX seconds (float). JS Date needs milliseconds.
        // Guard: if value looks like it's already in ms (>1e12) use directly.
        eventDate = new Date(event.timestamp > 1e12 ? event.timestamp : event.timestamp * 1000);
      } else {
        eventDate = new Date(event.timestamp);
      }
      
      if (isNaN(eventDate.getTime())) {
        console.error('[Ingest] Invalid timestamp for event:', event.event_id, event.timestamp);
        return;
      }
      
      cameraManager.updateCamera(event.camera_id, {
        last_event:   eventDate.toISOString(),
        events_today: (cameraManager.getCameraById(event.camera_id)?.events_today || 0) + 1,
      });
      // Broadcast to dashboard live
      broadcast('reid_event', event);
    } catch (err) { console.error('[Ingest] Error processing event:', err.message); }
  });

  // Update metrics broadcast
  broadcast('metrics_update', metricsStore.getMetrics());

  if (dataset_id) {
    datasetMgr.addEvents(dataset_id, events);
  }

  res.json({ ok: true, accepted: events.length });
}));

// ── DELETE /api/dataset/:id ───────────────────────────────────────────
router.delete('/:id', asyncH(async (req, res) => {
  const ok = datasetMgr.deleteDataset(req.params.id);
  if (!ok) return res.status(404).json({ error: 'Dataset not found' });
  broadcast('dataset_update', { type: 'deleted', dataset_id: req.params.id });
  res.json({ ok: true });
}));

// ── Helpers ───────────────────────────────────────────────────────────

function _sanitize(rec) {
  if (!rec) return null;
  return {
    id:          rec.id,
    name:        rec.name,
    status:      rec.status,
    format:      rec.format,
    cameras:     rec.cameras,
    uploadedAt:  rec.uploadedAt,
    startedAt:   rec.startedAt,
    finishedAt:  rec.finishedAt,
    progress:    rec.progress,
    metrics:     rec.metrics,
    error:       rec.error,
    eventCount:  rec.eventCount,
    fileSize:    _getFileSize(rec.zipPath),
  };
}

function _getFileSize(zipPath) {
  try { return fs.statSync(zipPath).size; } catch { return 0; }
}

function _broadcastLog(datasetId, line) {
  if (_io) _io.emit('dataset_log', { dataset_id: datasetId, line: line.trim() });
}

function _findPython() {
  // Check for venv in project root first
  const root    = path.join(__dirname, '..', '..');
  const venvWin = path.join(root, 'venv', 'Scripts', 'python.exe');
  const venvUnx = path.join(root, 'venv', 'bin', 'python');
  if (fs.existsSync(venvWin)) return venvWin;
  if (fs.existsSync(venvUnx)) return venvUnx;
  return process.platform === 'win32' ? 'python' : 'python3';
}

module.exports = router;
module.exports.setIO = setIO;
