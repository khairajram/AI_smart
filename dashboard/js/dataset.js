/**
 * dashboard/js/dataset.js
 * ───────────────────────
 * Dataset upload, processing, and results display logic.
 * Runs independently from app.js — just needs window.socketManager.
 */

'use strict';

// ── State ─────────────────────────────────────────────────────────────
const dsState = {
  selectedFile:   null,
  currentDataset: null,   // dataset being processed
  datasets:       [],
};

// ── DOM refs ──────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

function initDataset() {
  const dropZone       = $('dropZone');
  const fileInput      = $('datasetFileInput');
  const datasetOptions = $('datasetOptions');
  const cancelBtn      = $('cancelUploadBtn');
  const uploadBtn      = $('uploadAndProcessBtn');

  if (!dropZone) return;   // section not in DOM

  // ── File selection via click ──────────────────────────────────────
  fileInput?.addEventListener('change', () => {
    if (fileInput.files?.[0]) onFileSelected(fileInput.files[0]);
  });

  // ── Drag & drop ───────────────────────────────────────────────────
  dropZone.addEventListener('dragover',  e => { e.preventDefault(); dropZone.classList.add('drop-zone--over'); });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drop-zone--over'));
  dropZone.addEventListener('drop', e => {
    e.preventDefault();
    dropZone.classList.remove('drop-zone--over');
    const file = e.dataTransfer?.files?.[0];
    if (file?.name.toLowerCase().endsWith('.zip')) {
      onFileSelected(file);
    } else {
      showToast?.('Only ZIP files are supported', 'error');
    }
  });

  // ── Buttons ───────────────────────────────────────────────────────
  cancelBtn?.addEventListener('click', resetUploadUI);
  uploadBtn?.addEventListener('click', handleUploadAndProcess);

  // ── Socket events ─────────────────────────────────────────────────
  if (window.socketManager) {
    window.socketManager.onEvent('dataset_update',   onDatasetUpdate);
    window.socketManager.onEvent('dataset_progress', onDatasetProgress);
    window.socketManager.onEvent('dataset_log',      onDatasetLog);
  }

  // ── Load existing datasets ────────────────────────────────────────
  loadDatasetList();
}

// ── File selected callback ────────────────────────────────────────────
function onFileSelected(file) {
  dsState.selectedFile = file;
  const infoEl = $('optFileInfo');
  if (infoEl) {
    infoEl.innerHTML = `
      <span class="opt-file-name">&#128190; ${file.name}</span>
      <span class="opt-file-size">${formatBytes(file.size)}</span>
    `;
  }
  $('datasetOptions').style.display = '';
  $('dropZone').style.display = 'none';
}

function resetUploadUI() {
  dsState.selectedFile = null;
  const fi = $('datasetFileInput');
  if (fi) fi.value = '';
  if ($('datasetOptions')) $('datasetOptions').style.display = 'none';
  if ($('dropZone'))       $('dropZone').style.display = '';
  if ($('datasetProgress')) $('datasetProgress').style.display = 'none';
}

// ── Upload + trigger processing ────────────────────────────────────────
async function handleUploadAndProcess() {
  if (!dsState.selectedFile) return;

  const btn = $('uploadAndProcessBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Uploading…'; }

  const opts = {
    fps:        parseInt($('optFps')?.value || '10'),
    max_frames: $('optMaxFrames')?.value ? parseInt($('optMaxFrames').value) : null,
    camera_map: $('optCameraMap')?.value?.trim() || null,
  };

  try {
    // 1. Upload ZIP
    showLog('Uploading ZIP…');
    const formData = new FormData();
    formData.append('dataset', dsState.selectedFile);

    const uploadRes = await fetch('/api/dataset/upload', { method: 'POST', body: formData });
    if (!uploadRes.ok) {
      const err = await uploadRes.json();
      throw new Error(err.error || 'Upload failed');
    }
    const { dataset } = await uploadRes.json();
    dsState.currentDataset = dataset;

    showLog(`✓ Uploaded: ${dataset.name} (id: ${dataset.id.slice(0, 8)}…)`);
    if (btn) { btn.disabled = true; btn.textContent = 'Processing…'; }

    // Show progress panel
    if ($('datasetOptions'))  $('datasetOptions').style.display  = 'none';
    if ($('datasetProgress')) $('datasetProgress').style.display = '';

    // 2. Start processing
    const processRes = await fetch(`/api/dataset/${dataset.id}/process`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        fps:       opts.fps,
        max_frames: opts.max_frames,
        camera_map: opts.camera_map,
      }),
    });

    if (!processRes.ok) {
      const err = await processRes.json();
      throw new Error(err.error || 'Processing start failed');
    }

    showLog('✓ Python processor started. Waiting for events…');

  } catch (err) {
    showLog(`✗ Error: ${err.message}`, 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Upload & Process'; }
    showToast?.(err.message, 'error');
  }
}

// ── Socket handlers ───────────────────────────────────────────────────
function onDatasetUpdate({ type, dataset, dataset_id }) {
  if (type === 'done') {
    const pct = $('progressPct');
    const bar = $('progressBarFill');
    const title = $('progressTitle');
    if (pct) pct.textContent = '100%';
    if (bar) bar.style.width = '100%';
    if (title) title.textContent = `Done — ${dataset?.name || ''}`;
    showLog('✅ Processing complete!', 'success');
    showToast?.('Dataset processed successfully ✓', 'success');

    const btn = $('uploadAndProcessBtn');
    if (btn) { btn.disabled = false; btn.textContent = 'Upload & Process'; }

    loadDatasetList();
  } else if (type === 'error') {
    showLog(`✗ Error: ${dataset?.error || 'Unknown error'}`, 'error');
    showToast?.(dataset?.error || 'Processing failed', 'error');
    const btn = $('uploadAndProcessBtn');
    if (btn) { btn.disabled = false; btn.textContent = 'Upload & Process'; }
  } else if (type === 'uploaded' || type === 'deleted') {
    loadDatasetList();
  }
}

function onDatasetProgress({ dataset_id, progress, metrics }) {
  if (!progress) return;
  const overall = progress._overall_pct ?? 0;
  const pct  = $('progressPct');
  const bar  = $('progressBarFill');
  if (pct) pct.textContent = `${overall}%`;
  if (bar) bar.style.width = `${overall}%`;

  // Update per-camera steps
  const steps = $('progressSteps');
  if (steps) {
    const cams = Object.entries(progress).filter(([k]) => !k.startsWith('_'));
    steps.innerHTML = cams.map(([camId, info]) => `
      <div class="progress-step">
        <span class="step-cam">${camId}</span>
        <div class="step-bar-wrap">
          <div class="step-bar-fill" style="width:${info.pct || 0}%"></div>
        </div>
        <span class="step-pct">${info.pct || 0}%</span>
        <span class="step-status ${info.status === 'done' ? 'done' : ''}">
          ${info.status === 'done' ? '✓' : info.frames_done || 0} frames
        </span>
      </div>
    `).join('');
  }

  // Show metrics if done
  if (metrics && Object.keys(metrics).length) {
    showLog(`Metrics: ${metrics.unique_visitors || 0} unique, ${metrics.total_reentries || 0} re-entries`);
  }
}

function onDatasetLog({ dataset_id, line }) {
  if (!line?.trim()) return;
  showLog(line);
}

// ── Dataset list ──────────────────────────────────────────────────────
async function loadDatasetList() {
  try {
    const res = await fetch('/api/dataset');
    if (!res.ok) return;
    const datasets = await res.json();
    dsState.datasets = datasets;
    renderDatasetList(datasets);
    const countEl = $('datasetCount');
    if (countEl) countEl.textContent = `${datasets.length} dataset${datasets.length !== 1 ? 's' : ''}`;
  } catch (_) {}
}

function renderDatasetList(datasets) {
  const list = $('datasetList');
  if (!list) return;

  if (!datasets.length) {
    list.innerHTML = '<div class="ds-empty">No datasets uploaded yet</div>';
    return;
  }

  list.innerHTML = datasets.map(ds => {
    const statusCls  = { done: 'status-done', error: 'status-error', processing: 'status-processing', uploaded: 'status-uploaded' }[ds.status] || '';
    const statusIcon = { done: '✅', error: '❌', processing: '⚙️', uploaded: '📦' }[ds.status] || '?';
    const elapsed    = ds.finishedAt && ds.startedAt ? `${((ds.finishedAt - ds.startedAt) / 1000).toFixed(0)}s` : '—';
    const fileSize   = formatBytes(ds.fileSize || 0);

    return `
      <div class="ds-record" data-id="${ds.id}">
        <div class="ds-record-header">
          <span class="ds-icon">${statusIcon}</span>
          <span class="ds-name">${ds.name}</span>
          <span class="ds-badge ${statusCls}">${ds.status}</span>
          <span class="ds-size">${fileSize}</span>
        </div>
        <div class="ds-record-meta">
          <span>Format: ${ds.format || '—'}</span>
          <span>Cameras: ${(ds.cameras || []).join(', ') || '—'}</span>
          <span>Events: ${ds.eventCount || 0}</span>
          <span>Time: ${elapsed}</span>
          ${ds.metrics?.unique_visitors != null ? `<span>Unique: ${ds.metrics.unique_visitors}</span>` : ''}
          ${ds.metrics?.total_reentries  != null ? `<span>Re-entries: ${ds.metrics.total_reentries}</span>` : ''}
        </div>
        ${ds.error ? `<div class="ds-error">${ds.error.slice(0, 200)}</div>` : ''}
        <div class="ds-actions">
          ${ds.status === 'uploaded' ? `<button class="btn btn-primary btn-sm" onclick="triggerProcess('${ds.id}')">▶ Process</button>` : ''}
          <button class="btn btn-ghost btn-sm" onclick="deleteDataset('${ds.id}')">🗑 Delete</button>
        </div>
      </div>
    `;
  }).join('');
}

window.triggerProcess = async (id) => {
  const fps    = parseInt($('optFps')?.value || '10');
  const camMap = $('optCameraMap')?.value?.trim() || null;
  try {
    await fetch(`/api/dataset/${id}/process`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fps, camera_map: camMap }),
    });
    if ($('datasetProgress')) $('datasetProgress').style.display = '';
    showLog(`Started processing dataset ${id.slice(0, 8)}…`);
    loadDatasetList();
  } catch (err) { showToast?.(err.message, 'error'); }
};

window.deleteDataset = async (id) => {
  if (!confirm('Delete this dataset? This cannot be undone.')) return;
  try {
    await fetch(`/api/dataset/${id}`, { method: 'DELETE' });
    loadDatasetList();
    showToast?.('Dataset deleted');
  } catch (err) { showToast?.(err.message, 'error'); }
};

// ── Helpers ───────────────────────────────────────────────────────────
function showLog(msg, type = 'info') {
  const logEl = $('progressLog');
  if (!logEl) return;
  const line = document.createElement('div');
  line.className = `log-line log-${type}`;
  line.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  logEl.prepend(line);
  if (logEl.children.length > 100) logEl.lastChild?.remove();
}

function formatBytes(b) {
  if (!b) return '—';
  if (b < 1024)         return `${b} B`;
  if (b < 1024 ** 2)    return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1024 ** 3)    return `${(b / 1024 ** 2).toFixed(1)} MB`;
  return `${(b / 1024 ** 3).toFixed(2)} GB`;
}

// ── Boot ──────────────────────────────────────────────────────────────
document.readyState === 'loading'
  ? document.addEventListener('DOMContentLoaded', initDataset)
  : initDataset();
