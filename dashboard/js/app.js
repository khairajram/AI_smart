/**
 * dashboard/js/app.js
 * ───────────────────
 * Main ReID Dashboard application logic.
 * Bridges the Socket.io/REST backend with the HTML defined in index.html.
 */

'use strict';

// ─────────────────────────────────────────────────────────────────────
//  State
// ─────────────────────────────────────────────────────────────────────
const state = {
  metrics:        {},
  events:         [],       // last 200 events
  cameras:        [],
  activeVisitors: [],
  exitedVisitors: [],
  config:         {},
  confidenceScores: [],
  isConnected:    false,
  mode:           'demo',
  isPaused:       false,
  selectedCamera: null,
  activeFilter:   'ALL',
  registrySortCol: 'last_seen',
  registrySortDir: 'desc',
  prevMetrics:    {},
};

// ─────────────────────────────────────────────────────────────────────
//  Event type config
// ─────────────────────────────────────────────────────────────────────
const EVENT_CONFIG = {
  NEW_VISITOR:        { color: '#10b981', icon: '👤', label: 'New Visitor',        bg: 'rgba(16,185,129,0.08)' },
  CROSS_CAMERA_MATCH: { color: '#3b82f6', icon: '🔗', label: 'Cross-Camera Match', bg: 'rgba(59,130,246,0.08)' },
  REENTRY:            { color: '#f59e0b', icon: '↩️',  label: 'Re-entry',           bg: 'rgba(245,158,11,0.08)' },
  VISITOR_EXITED:     { color: '#6b7280', icon: '🚪', label: 'Visitor Exited',      bg: 'rgba(107,114,128,0.08)' },
};

// ─────────────────────────────────────────────────────────────────────
//  Utility
// ─────────────────────────────────────────────────────────────────────
function formatTimeAgo(timestamp) {
  const ts    = typeof timestamp === 'number' ? (timestamp > 1e12 ? timestamp : timestamp * 1000) : new Date(timestamp).getTime();
  const diffS = Math.floor((Date.now() - ts) / 1000);
  if (diffS < 5)  return 'just now';
  if (diffS < 60) return `${diffS}s ago`;
  const diffM = Math.floor(diffS / 60);
  if (diffM < 60) return `${diffM}m ago`;
  return `${Math.floor(diffM / 60)}h ago`;
}

function formatVisitorId(id) {
  if (!id) return 'unknown';
  return id.replace(/-/g, '').substring(0, 8).toUpperCase();
}

function formatUptime(s) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
  if (h > 0) return `${h}h ${m}m ${sec}s`;
  if (m > 0) return `${m}m ${sec}s`;
  return `${sec}s`;
}

function formatTimestamp(timestamp) {
  const ts = typeof timestamp === 'number' ? (timestamp > 1e12 ? timestamp : timestamp * 1000) : new Date(timestamp).getTime();
  return new Date(ts).toLocaleTimeString('en-US', { hour12: false });
}

function clamp(v, lo, hi) { return Math.min(Math.max(v, lo), hi); }

function animateCounter(el, from, to, dur = 700) {
  if (!el) return;
  const diff = to - from;
  if (diff === 0) { el.textContent = to.toLocaleString(); return; }
  const start = performance.now();
  const tick  = (now) => {
    const p = clamp((now - start) / dur, 0, 1);
    el.textContent = Math.round(from + diff * (1 - Math.pow(1 - p, 3))).toLocaleString();
    if (p < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function showToast(msg, type = 'success') {
  const c = document.getElementById('toastContainer') || document.getElementById('toast-container');
  if (!c) return;
  const t = document.createElement('div');
  t.className = `toast toast--${type}`;
  t.textContent = msg;
  c.appendChild(t);
  requestAnimationFrame(() => t.classList.add('toast--show'));
  setTimeout(() => { t.classList.remove('toast--show'); setTimeout(() => t.remove(), 350); }, 3000);
}

function updateClock() {
  const el = document.getElementById('headerClock') || document.getElementById('live-clock');
  if (el) el.textContent = new Date().toLocaleTimeString('en-US', { hour12: false });
}

// ─────────────────────────────────────────────────────────────────────
//  Metric Cards
// ─────────────────────────────────────────────────────────────────────
function updateMetricCards(metrics) {
  const prev = state.prevMetrics;
  [
    ['metric-active',   'active_visitors'],
    ['metric-unique',   'total_unique_visitors'],
    ['metric-reentry',  'total_reentries'],
    ['metric-crosscam', 'total_cross_camera'],
  ].forEach(([id, key]) => {
    const el   = document.getElementById(id);
    const from = typeof prev[key] === 'number' ? prev[key] : 0;
    const to   = typeof metrics[key] === 'number' ? metrics[key] : 0;
    if (el) animateCounter(el, from, to);
  });

  const epmEl = document.getElementById('metric-epm');
  if (epmEl) epmEl.textContent = (metrics.events_per_minute || 0).toFixed(1);

  const uptimeEl = document.getElementById('sysUptime') || document.getElementById('system-uptime');
  if (uptimeEl) uptimeEl.textContent = formatUptime(metrics.uptime_seconds || 0);

  const totalEvEl = document.getElementById('sysTotalEvents');
  if (totalEvEl) totalEvEl.textContent = (
    (metrics.total_unique_visitors || 0) + (metrics.total_reentries || 0) +
    (metrics.total_cross_camera || 0) + (metrics.total_exits_recorded || 0)
  ).toLocaleString();

  state.prevMetrics = { ...metrics };
  state.metrics     = { ...metrics };
}

// ─────────────────────────────────────────────────────────────────────
//  Camera Grid
// ─────────────────────────────────────────────────────────────────────
function updateCameraGrid(cameras) {
  if (!cameras || !cameras.length) return;
  state.cameras = cameras;

  cameras.forEach((cam) => {
    // Active visitor count
    const activeEl = document.getElementById(`cam-active-${cam.camera_id}`);
    if (activeEl) activeEl.textContent = cam.active_visitors ?? 0;

    // Total events
    const evEl = document.getElementById(`cam-events-${cam.camera_id}`);
    if (evEl) evEl.textContent = cam.events_today ?? 0;

    // FPS
    const fpsEl = document.getElementById(`cam-fps-${cam.camera_id}`);
    if (fpsEl) fpsEl.textContent = cam.fps ? `${cam.fps}fps` : '—';

    // Last event
    const lastEl = document.getElementById(`cam-last-${cam.camera_id}`);
    if (lastEl) lastEl.textContent = cam.last_event ? formatTimeAgo(cam.last_event) : 'No events yet';

    // Status dot
    const statusEl = document.getElementById(`cam-status-${cam.camera_id}`);
    if (statusEl) {
      const isActive = cam.status === 'active';
      statusEl.className = `camera-status ${isActive ? 'online' : 'offline'}`;
      const label = statusEl.querySelector('.status-label');
      if (label) label.textContent = isActive ? 'Online' : 'Offline';
    }
  });

  if (window.chartsManager) window.chartsManager.updateCameraActivity(cameras);
}

// ─────────────────────────────────────────────────────────────────────
//  Event Feed
// ─────────────────────────────────────────────────────────────────────
function addEventToFeed(event) {
  const cfg = EVENT_CONFIG[event.event_type] || EVENT_CONFIG.NEW_VISITOR;

  // Filter by camera
  if (state.selectedCamera && event.camera_id !== state.selectedCamera) return;
  // Filter by event type
  if (state.activeFilter !== 'ALL' && event.event_type !== state.activeFilter) return;

  state.events.unshift(event);
  if (state.events.length > 200) state.events.pop();

  // Update count badge
  const countEl = document.getElementById('feedCount');
  if (countEl) countEl.textContent = `${Math.min(state.events.length, 50)} events`;

  // Hide "waiting" empty state
  const emptyEl = document.getElementById('feedEmpty');
  if (emptyEl) emptyEl.style.display = 'none';

  if (state.isPaused) return;

  const feed = document.getElementById('eventFeed') || document.getElementById('event-feed');
  if (!feed) return;

  const item = document.createElement('div');
  item.className = 'feed-item';
  item.style.borderLeftColor = cfg.color;
  item.style.background      = cfg.bg;

  item.innerHTML = `
    <div class="feed-item-icon" title="${cfg.label}">${cfg.icon}</div>
    <div class="feed-item-body">
      <div class="feed-item-top">
        <span class="feed-item-type" style="color:${cfg.color}">${cfg.label}</span>
        <span class="feed-item-time">${formatTimeAgo(event.timestamp)}</span>
      </div>
      <div class="feed-item-bottom">
        <code class="feed-item-id">ID: ${formatVisitorId(event.visitor_id)}</code>
        <span class="feed-item-cam">${event.camera_id || '—'}</span>
        <span class="feed-item-conf" style="background:${cfg.color}22;color:${cfg.color}">
          ${(event.reid_confidence || 1.0).toFixed(2)}
        </span>
      </div>
    </div>
  `;

  feed.prepend(item);
  requestAnimationFrame(() => item.classList.add('feed-item--visible'));

  // Trim to 50
  const items = feed.querySelectorAll('.feed-item');
  if (items.length > 50) for (let i = 50; i < items.length; i++) items[i].remove();
}

function renderInitialEvents(events) {
  const feed = document.getElementById('eventFeed') || document.getElementById('event-feed');
  if (!feed) return;
  feed.innerHTML = '';
  [...events].reverse().forEach(ev => addEventToFeed(ev));
}

// ─────────────────────────────────────────────────────────────────────
//  Alerts Sidebar
// ─────────────────────────────────────────────────────────────────────
const _alertCounts = { REENTRY: 0, CROSS_CAMERA_MATCH: 0 };

function updateAlertsSidebar(event) {
  if (event.event_type !== 'REENTRY' && event.event_type !== 'CROSS_CAMERA_MATCH') return;
  const cfg = EVENT_CONFIG[event.event_type];

  const listId   = event.event_type === 'REENTRY' ? 'reentryAlerts' : 'crosscamAlerts';
  const countId  = event.event_type === 'REENTRY' ? 'reentryAlertCount' : 'crosscamAlertCount';
  const container = document.getElementById(listId);
  if (!container) return;

  // Clear "no events" placeholder
  const empty = container.querySelector('.alert-empty');
  if (empty) empty.remove();

  _alertCounts[event.event_type]++;
  const countEl = document.getElementById(countId);
  if (countEl) countEl.textContent = _alertCounts[event.event_type];

  const card = document.createElement('div');
  card.className = 'alert-card';
  card.style.borderLeftColor = cfg.color;
  card.innerHTML = `
    <div class="alert-card-top">
      <span>${cfg.icon} <strong>${formatVisitorId(event.visitor_id)}</strong></span>
      <span style="color:${cfg.color};font-size:0.75rem;font-weight:600">${(event.reid_confidence||1).toFixed(2)}</span>
    </div>
    <div class="alert-card-bottom">
      <span>${event.camera_id || '—'}</span>
      <span>${formatTimeAgo(event.timestamp)}</span>
    </div>
  `;

  container.prepend(card);
  requestAnimationFrame(() => card.classList.add('alert-card--visible'));

  const cards = container.querySelectorAll('.alert-card');
  if (cards.length > 5) for (let i = 5; i < cards.length; i++) cards[i].remove();
}

// ─────────────────────────────────────────────────────────────────────
//  Registry Table
// ─────────────────────────────────────────────────────────────────────
function updateRegistry(data) {
  if (!data) return;
  state.activeVisitors = data.active || [];
  state.exitedVisitors = data.exited || [];
  renderRegistryTable();

  const countEl = document.getElementById('registryCount') || document.getElementById('registry-count');
  if (countEl) countEl.textContent = `${state.activeVisitors.length} visitors`;

  const updateEl = document.getElementById('registryUpdateTime');
  if (updateEl) updateEl.textContent = `Updated ${formatTimeAgo(Date.now() / 1000)}`;
}

function renderRegistryTable() {
  const tbody = document.getElementById('registryBody') || document.getElementById('registry-tbody');
  if (!tbody) return;

  const visitors = [...state.activeVisitors].sort((a, b) => {
    const col = state.registrySortCol;
    const dir = state.registrySortDir === 'asc' ? 1 : -1;
    const av = a[col] ?? '', bv = b[col] ?? '';
    return (typeof av === 'number' ? av - bv : String(av).localeCompare(String(bv))) * dir;
  });

  if (!visitors.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text-muted);padding:2rem">No active visitors</td></tr>';
    return;
  }

  tbody.innerHTML = visitors.map((v) => {
    const hasReentry = (v.reentry_count || 0) > 0;
    const conf       = (v.reid_confidence || 1.0).toFixed(2);
    const confColor  = v.reid_confidence >= 0.85 ? '#10b981' : v.reid_confidence >= 0.75 ? '#f59e0b' : '#ef4444';
    return `
      <tr class="${hasReentry ? 'row--reentry' : ''}">
        <td style="display:flex;align-items:center;gap:8px">
          <span class="status-pulse-dot"></span>
          <code style="font-family:monospace;font-size:0.8rem">${formatVisitorId(v.visitor_id)}</code>
        </td>
        <td>${v.camera_id || '—'}</td>
        <td style="color:var(--text-secondary);font-size:0.8rem">${v.first_seen ? formatTimestamp(v.first_seen) : '—'}</td>
        <td style="color:var(--text-secondary);font-size:0.8rem">${v.last_seen  ? formatTimestamp(v.last_seen)  : '—'}</td>
        <td style="${hasReentry ? 'color:#f59e0b;font-weight:600' : ''}">${v.reentry_count || 0}</td>
        <td>
          <span style="display:inline-block;padding:2px 8px;border-radius:12px;font-size:0.75rem;font-weight:600;background:${confColor}22;color:${confColor}">
            ${conf}
          </span>
        </td>
        <td>
          <span style="display:inline-flex;align-items:center;gap:4px;font-size:0.75rem;color:#10b981;font-weight:500">
            <span style="width:6px;height:6px;border-radius:50%;background:#10b981;animation:pulse-dot 1.5s infinite"></span>
            Active
          </span>
        </td>
      </tr>
    `;
  }).join('');
}

function sortRegistry(col) {
  if (state.registrySortCol === col) state.registrySortDir = state.registrySortDir === 'asc' ? 'desc' : 'asc';
  else { state.registrySortCol = col; state.registrySortDir = 'desc'; }
  renderRegistryTable();
}

// ─────────────────────────────────────────────────────────────────────
//  Configuration
// ─────────────────────────────────────────────────────────────────────
function renderConfig(cfg) {
  state.config = cfg;

  const reidSlider    = document.getElementById('threshold-reid');
  const reentrySlider = document.getElementById('threshold-reentry');
  const windowSel     = document.getElementById('reentry-window');
  const badgeReid     = document.getElementById('badge-reid');
  const badgeReentry  = document.getElementById('badge-reentry');

  if (reidSlider && cfg.reid_threshold) {
    reidSlider.value = cfg.reid_threshold;
    if (badgeReid) badgeReid.textContent = parseFloat(cfg.reid_threshold).toFixed(2);
    updateSliderFill(reidSlider, 'fill-reid', 0.70, 0.95);
  }
  if (reentrySlider && cfg.reentry_threshold) {
    reentrySlider.value = cfg.reentry_threshold;
    if (badgeReentry) badgeReentry.textContent = parseFloat(cfg.reentry_threshold).toFixed(2);
    updateSliderFill(reentrySlider, 'fill-reentry', 0.70, 0.92);
  }
  if (windowSel && cfg.reentry_window) windowSel.value = cfg.reentry_window;
}

function updateSliderFill(slider, fillId, min, max) {
  const fillEl = document.getElementById(fillId);
  if (!fillEl) return;
  const pct = ((parseFloat(slider.value) - min) / (max - min)) * 100;
  fillEl.style.width = `${clamp(pct, 0, 100)}%`;
}

async function handleConfigSave() {
  const payload = {
    reid_threshold:    parseFloat(document.getElementById('threshold-reid')?.value    || 0.82),
    reentry_threshold: parseFloat(document.getElementById('threshold-reentry')?.value || 0.80),
    reentry_window:    parseInt(document.getElementById('reentry-window')?.value       || 300),
  };
  try {
    const res = await fetch('/api/config', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (res.ok) {
      showToast('Configuration saved ✓');
      if (window.socketManager) window.socketManager.emit('update_config', payload);
    } else { showToast('Failed to save config', 'error'); }
  } catch { showToast('Network error', 'error'); }
}

function openResetModal() {
  const m = document.getElementById('resetModal') || document.getElementById('reset-modal');
  if (m) m.classList.add('modal--visible');
}

function closeResetModal() {
  const m = document.getElementById('resetModal') || document.getElementById('reset-modal');
  if (m) m.classList.remove('modal--visible');
}

async function confirmRegistryReset() {
  closeResetModal();
  try {
    const res = await fetch('/api/registry/reset', { method: 'POST' });
    if (res.ok) {
      showToast('Registry cleared ✓');
      state.activeVisitors = [];
      state.exitedVisitors = [];
      state.events         = [];
      renderRegistryTable();
      const feed = document.getElementById('eventFeed');
      if (feed) { feed.innerHTML = ''; const em = document.getElementById('feedEmpty'); if (em) em.style.display = ''; }
      if (window.socketManager) window.socketManager.emit('reset_registry');
    } else { showToast('Reset failed', 'error'); }
  } catch { showToast('Network error', 'error'); }
}

// ─────────────────────────────────────────────────────────────────────
//  Connection status
// ─────────────────────────────────────────────────────────────────────
function setConnectionStatus(connected) {
  state.isConnected = connected;
  const el    = document.getElementById('connectionStatus') || document.getElementById('connection-pill');
  const label = document.getElementById('connLabel')        || document.getElementById('connection-text');
  if (el)    el.className    = `connection-status ${connected ? 'connected' : 'disconnected'}`;
  if (label) label.textContent = connected ? 'Connected' : 'Disconnected';
}

function setMode(mode) {
  state.mode = mode;
  const textEl = document.getElementById('modeText') || document.getElementById('mode-badge');
  if (textEl) textEl.textContent = mode === 'demo' ? 'DEMO' : 'LIVE';
  const badge  = document.getElementById('modeBadge');
  if (badge)   badge.className  = `mode-badge mode-badge--${mode}`;
}

// ─────────────────────────────────────────────────────────────────────
//  Chart tabs
// ─────────────────────────────────────────────────────────────────────
function initChartTabs() {
  const tabs   = document.querySelectorAll('.chart-tab');
  const panels = document.querySelectorAll('.chart-panel');
  const indicator = document.getElementById('tabIndicator');

  if (!tabs.length) return;

  function activateTab(tab, idx) {
    tabs.forEach(t   => t.classList.remove('active'));
    panels.forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    if (panels[idx]) panels[idx].classList.add('active');
    if (indicator) {
      indicator.style.width     = `${tab.offsetWidth}px`;
      indicator.style.transform = `translateX(${tab.offsetLeft}px)`;
    }
    setTimeout(() => { if (window.chartsManager) window.chartsManager.resize?.(); }, 60);
  }

  tabs.forEach((tab, idx) => tab.addEventListener('click', () => activateTab(tab, idx)));
  activateTab(tabs[0], 0);
}

// ─────────────────────────────────────────────────────────────────────
//  Filter pills
// ─────────────────────────────────────────────────────────────────────
function initFilterPills() {
  document.querySelectorAll('.filter-pill').forEach(pill => {
    pill.addEventListener('click', () => {
      document.querySelectorAll('.filter-pill').forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      state.activeFilter = pill.dataset.filter || 'ALL';
      // Re-render feed
      const feed = document.getElementById('eventFeed');
      if (feed) feed.innerHTML = '';
      [...state.events].slice(0, 50).reverse().forEach(ev => addEventToFeed(ev));
    });
  });
}

// ─────────────────────────────────────────────────────────────────────
//  Camera card filter
// ─────────────────────────────────────────────────────────────────────
function initCameraFilter() {
  document.querySelectorAll('.camera-card').forEach(card => {
    card.addEventListener('click', () => {
      const camId = card.dataset.camera;
      if (state.selectedCamera === camId) {
        state.selectedCamera = null;
        document.querySelectorAll('.camera-card').forEach(c => c.classList.remove('camera-card--selected'));
      } else {
        state.selectedCamera = camId;
        document.querySelectorAll('.camera-card').forEach(c => c.classList.remove('camera-card--selected'));
        card.classList.add('camera-card--selected');
      }
      const feed = document.getElementById('eventFeed');
      if (feed) { feed.innerHTML = ''; [...state.events].slice(0, 50).reverse().forEach(ev => addEventToFeed(ev)); }
    });
  });
}

// ─────────────────────────────────────────────────────────────────────
//  UI listeners
// ─────────────────────────────────────────────────────────────────────
function initUIListeners() {
  // Slider live preview
  const reidSlider    = document.getElementById('threshold-reid');
  const reentrySlider = document.getElementById('threshold-reentry');

  reidSlider?.addEventListener('input', () => {
    const el = document.getElementById('badge-reid');
    if (el) el.textContent = parseFloat(reidSlider.value).toFixed(2);
    updateSliderFill(reidSlider, 'fill-reid', 0.70, 0.95);
  });
  reentrySlider?.addEventListener('input', () => {
    const el = document.getElementById('badge-reentry');
    if (el) el.textContent = parseFloat(reentrySlider.value).toFixed(2);
    updateSliderFill(reentrySlider, 'fill-reentry', 0.70, 0.92);
  });

  document.getElementById('saveConfigBtn')?.addEventListener('click', handleConfigSave);
  document.getElementById('resetRegistryBtn')?.addEventListener('click', openResetModal);
  document.getElementById('modalCancelBtn')?.addEventListener('click', closeResetModal);
  document.getElementById('modalConfirmBtn')?.addEventListener('click', confirmRegistryReset);

  // Modal backdrop
  document.getElementById('resetModal')?.addEventListener('click', e => {
    if (e.target.id === 'resetModal') closeResetModal();
  });

  // Event feed pause
  const feed = document.getElementById('eventFeed');
  const pauseOverlay = document.getElementById('feedPausedOverlay');
  feed?.addEventListener('mouseenter', () => { state.isPaused = true;  if (pauseOverlay) pauseOverlay.classList.add('visible'); });
  feed?.addEventListener('mouseleave', () => { state.isPaused = false; if (pauseOverlay) pauseOverlay.classList.remove('visible'); });

  // Clear feed
  document.getElementById('clearFeedBtn')?.addEventListener('click', () => {
    if (feed) feed.innerHTML = '';
    state.events = [];
    const emptyEl = document.getElementById('feedEmpty');
    if (emptyEl) emptyEl.style.display = '';
  });

  // Registry sort
  document.querySelectorAll('.sortable[data-col]').forEach(th => {
    th.addEventListener('click', () => sortRegistry(th.dataset.col));
    th.style.cursor = 'pointer';
  });

  // Sidebar toggle
  document.getElementById('sidebarToggle')?.addEventListener('click', () => {
    document.getElementById('alertsSidebar')?.classList.toggle('collapsed');
  });

  // Nav links (scroll to sections)
  document.querySelectorAll('.nav-link').forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
      link.classList.add('active');
    });
  });
}

// ─────────────────────────────────────────────────────────────────────
//  Charts integration
// ─────────────────────────────────────────────────────────────────────
function updateChartsFromEvent(event) {
  if (!window.chartsManager) return;
  if (event.reid_confidence) {
    state.confidenceScores.push(event.reid_confidence);
    if (state.confidenceScores.length > 500) state.confidenceScores.shift();
    window.chartsManager.updateConfidence(state.confidenceScores);
  }
  const counts = { NEW_VISITOR: 0, CROSS_CAMERA_MATCH: 0, REENTRY: 0, VISITOR_EXITED: 0 };
  state.events.slice(0, 200).forEach(ev => { if (counts[ev.event_type] !== undefined) counts[ev.event_type]++; });
  window.chartsManager.updateDistribution(counts);
}

// ─────────────────────────────────────────────────────────────────────
//  REST bootstrap
// ─────────────────────────────────────────────────────────────────────
async function fetchInitialData() {
  try {
    const [mR, eR, cR, rR, cfgR] = await Promise.allSettled([
      fetch('/api/metrics'),
      fetch('/api/events?limit=50'),
      fetch('/api/cameras'),
      fetch('/api/registry'),
      fetch('/api/config'),
    ]);

    if (mR.status === 'fulfilled' && mR.value.ok)   updateMetricCards(await mR.value.json());
    if (cR.status === 'fulfilled' && cR.value.ok)   updateCameraGrid(await cR.value.json());
    if (rR.status === 'fulfilled' && rR.value.ok)   updateRegistry(await rR.value.json());
    if (cfgR.status === 'fulfilled' && cfgR.value.ok) renderConfig(await cfgR.value.json());

    if (eR.status === 'fulfilled' && eR.value.ok) {
      const data   = await eR.value.json();
      const events = Array.isArray(data) ? data : (data.events || []);
      state.events = events;
      renderInitialEvents(events);
      const counts = { NEW_VISITOR: 0, CROSS_CAMERA_MATCH: 0, REENTRY: 0, VISITOR_EXITED: 0 };
      events.forEach(ev => { if (counts[ev.event_type] !== undefined) counts[ev.event_type]++; });
      if (window.chartsManager) window.chartsManager.updateDistribution(counts);
    }

    const tlRes = await fetch('/api/events/timeline?hours=1');
    if (tlRes.ok && window.chartsManager) {
      const data = await tlRes.json();
      window.chartsManager.updateTimeline(data.timeline || data);
    }
  } catch (err) {
    console.warn('[App] Init data error:', err.message);
  }
}

// ─────────────────────────────────────────────────────────────────────
//  Socket handlers
// ─────────────────────────────────────────────────────────────────────
function setupSocketHandlers() {
  if (!window.socketManager) return;
  const sm = window.socketManager;

  sm.onEvent('connect',    () => setConnectionStatus(true));
  sm.onEvent('disconnect', () => setConnectionStatus(false));

  sm.onEvent('reid_event', (event) => {
    addEventToFeed(event);
    updateAlertsSidebar(event);
    updateChartsFromEvent(event);
  });

  sm.onEvent('metrics_update', (metrics) => {
    updateMetricCards(metrics);
    if (window.chartsManager) window.chartsManager.addTimelinePoint(metrics.active_visitors || 0);
  });

  sm.onEvent('camera_update',     (cameras) => updateCameraGrid(cameras));
  sm.onEvent('registry_snapshot', (data)    => updateRegistry(data));
  sm.onEvent('mode_change',       (data)    => setMode(data.mode));
  sm.onEvent('config_update',     (cfg)     => renderConfig(cfg));
}

// ─────────────────────────────────────────────────────────────────────
//  Init
// ─────────────────────────────────────────────────────────────────────
async function initApp() {
  console.log('[ReID Dashboard] Starting…');
  updateClock();
  setInterval(updateClock, 1000);
  initChartTabs();
  initFilterPills();
  initCameraFilter();
  initUIListeners();
  await new Promise(r => setTimeout(r, 150)); // let charts initialise
  await fetchInitialData();
  setupSocketHandlers();
  console.log('[ReID Dashboard] ✓ Ready');
}

document.readyState === 'loading'
  ? document.addEventListener('DOMContentLoaded', initApp)
  : initApp();
