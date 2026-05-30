/**
 * charts.js — Chart.js 4.x Manager
 * ReID Analytics Dashboard
 */

(function () {
  'use strict';

  /* ── Global Chart.js Defaults ───────────────────────────────────── */
  Chart.defaults.color          = '#94a3b8';
  Chart.defaults.borderColor    = 'rgba(255,255,255,0.06)';
  Chart.defaults.font.family    = "'Inter', system-ui, sans-serif";
  Chart.defaults.font.size      = 12;
  Chart.defaults.animation.duration = 600;

  /* ── Color Tokens ────────────────────────────────────────────────── */
  const C = {
    purple:  '#8b5cf6',
    blue:    '#3b82f6',
    emerald: '#10b981',
    amber:   '#f59e0b',
    gray:    '#6b7280',
    muted:   'rgba(255,255,255,0.08)',
    text:    '#f1f5f9',
    textSub: '#94a3b8',
  };

  /* ── Helper: create gradient fill ───────────────────────────────── */
  function createVerticalGradient(ctx, colorTop, colorBottom = 'transparent') {
    const h = ctx.canvas.height;
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, colorTop);
    grad.addColorStop(1, colorBottom);
    return grad;
  }

  /* ── Helper: create horizontal bar gradient ──────────────────────── */
  function createHorizGradient(ctx, colorLeft, colorRight) {
    const w = ctx.canvas.width;
    const grad = ctx.createLinearGradient(0, 0, w, 0);
    grad.addColorStop(0, colorLeft);
    grad.addColorStop(1, colorRight);
    return grad;
  }

  /* ── Common axis/tooltip config ──────────────────────────────────── */
  function axisDefaults(overrides = {}) {
    return {
      grid: { color: 'rgba(255,255,255,0.06)', drawBorder: false, ...overrides.grid },
      ticks: { color: '#94a3b8', font: { size: 11 }, ...overrides.ticks },
      border: { dash: [4, 4], color: 'transparent', ...overrides.border },
    };
  }

  const tooltipPlugin = {
    backgroundColor: 'rgba(15,18,33,0.95)',
    titleColor: '#f1f5f9',
    bodyColor: '#94a3b8',
    borderColor: 'rgba(139,92,246,0.3)',
    borderWidth: 1,
    cornerRadius: 8,
    padding: 10,
    displayColors: true,
    boxWidth: 10,
    boxHeight: 10,
    boxPadding: 4,
  };

  /* ════════════════════════════════════════════════════════════════
     CHART 1 — Visitor Timeline (Line)
  ════════════════════════════════════════════════════════════════ */
  let timelineChart = null;

  function buildTimelineLabels() {
    const labels = [];
    const now = new Date();
    for (let i = 59; i >= 0; i--) {
      const t = new Date(now.getTime() - i * 60 * 1000);
      labels.push(t.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false }));
    }
    return labels;
  }

  function initTimelineChart() {
    const canvas = document.getElementById('chart-timeline');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    const fillGrad = createVerticalGradient(ctx, 'rgba(139,92,246,0.35)', 'rgba(139,92,246,0)');

    timelineChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: buildTimelineLabels(),
        datasets: [{
          label: 'Visitors',
          data: Array.from({ length: 60 }, () => Math.floor(Math.random() * 8 + 1)),
          borderColor: C.purple,
          backgroundColor: fillGrad,
          borderWidth: 2.5,
          fill: true,
          tension: 0.45,
          pointRadius: 0,
          pointHoverRadius: 5,
          pointHoverBackgroundColor: C.purple,
          pointHoverBorderColor: '#fff',
          pointHoverBorderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: { ...tooltipPlugin },
        },
        scales: {
          x: {
            ...axisDefaults({ grid: { display: false } }),
            ticks: {
              color: '#94a3b8',
              maxTicksLimit: 8,
              font: { size: 11 },
              maxRotation: 0,
            },
          },
          y: {
            ...axisDefaults(),
            beginAtZero: true,
            ticks: {
              color: '#94a3b8',
              font: { size: 11 },
              stepSize: 1,
              callback: (v) => Number.isInteger(v) ? v : null,
            },
          },
        },
      },
    });
  }

  /* ════════════════════════════════════════════════════════════════
     CHART 2 — Event Distribution (Doughnut)
  ════════════════════════════════════════════════════════════════ */
  let distributionChart = null;

  const EVENT_LABELS = ['New Visitor', 'Cross-Camera', 'Re-entry', 'Exited'];
  const EVENT_COLORS = [C.emerald, C.blue, C.amber, C.gray];

  // Custom centre-text plugin
  const doughnutCentrePlugin = {
    id: 'doughnutCentre',
    afterDraw(chart) {
      if (chart.config.type !== 'doughnut') return;
      const { ctx, chartArea } = chart;
      const cx = (chartArea.left + chartArea.right) / 2;
      const cy = (chartArea.top + chartArea.bottom) / 2;
      const total = chart.data.datasets[0].data.reduce((a, b) => a + b, 0);

      ctx.save();
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';

      ctx.font = `700 28px Inter, system-ui, sans-serif`;
      ctx.fillStyle = '#f1f5f9';
      ctx.fillText(total, cx, cy - 8);

      ctx.font = `500 11px Inter, system-ui, sans-serif`;
      ctx.fillStyle = '#94a3b8';
      ctx.fillText('TOTAL', cx, cy + 14);
      ctx.restore();
    },
  };

  Chart.register(doughnutCentrePlugin);

  function initDistributionChart() {
    const canvas = document.getElementById('chart-distribution');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    distributionChart = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: EVENT_LABELS,
        datasets: [{
          data: [0, 0, 0, 0],
          backgroundColor: EVENT_COLORS.map(c => c + '33'),
          borderColor: EVENT_COLORS,
          borderWidth: 2,
          hoverBackgroundColor: EVENT_COLORS.map(c => c + '55'),
          hoverBorderWidth: 3,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '70%',
        plugins: {
          legend: {
            position: 'right',
            labels: {
              color: '#94a3b8',
              font: { size: 11, weight: '500' },
              padding: 12,
              usePointStyle: true,
              pointStyle: 'circle',
            },
          },
          tooltip: {
            ...tooltipPlugin,
            callbacks: {
              label(ctx) {
                const total = ctx.dataset.data.reduce((a, b) => a + b, 0);
                const pct = total ? ((ctx.parsed / total) * 100).toFixed(1) : 0;
                return ` ${ctx.label}: ${ctx.parsed} (${pct}%)`;
              },
            },
          },
        },
      },
    });
  }

  /* ════════════════════════════════════════════════════════════════
     CHART 3 — Camera Activity (Bar)
  ════════════════════════════════════════════════════════════════ */
  let cameraActivityChart = null;

  function initCameraActivityChart() {
    const canvas = document.getElementById('chart-cameras');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    const barGrad = createVerticalGradient(ctx, 'rgba(59,130,246,0.9)', 'rgba(139,92,246,0.4)');

    cameraActivityChart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: ['ENTRANCE', 'AISLE_1', 'CHECKOUT'],
        datasets: [{
          label: 'Events',
          data: [0, 0, 0],
          backgroundColor: barGrad,
          borderColor: [C.blue, C.purple, C.emerald],
          borderWidth: 2,
          borderRadius: 8,
          borderSkipped: false,
          hoverBackgroundColor: 'rgba(139,92,246,0.6)',
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { ...tooltipPlugin },
        },
        scales: {
          x: {
            ...axisDefaults({ grid: { display: false } }),
          },
          y: {
            ...axisDefaults(),
            beginAtZero: true,
            ticks: {
              color: '#94a3b8',
              font: { size: 11 },
              callback: (v) => Number.isInteger(v) ? v : null,
            },
          },
        },
      },
    });
  }

  /* ════════════════════════════════════════════════════════════════
     CHART 4 — Confidence Distribution (Histogram / Bar)
  ════════════════════════════════════════════════════════════════ */
  let confidenceChart = null;

  const CONF_BINS  = ['0.75–0.80', '0.80–0.85', '0.85–0.90', '0.90–0.95', '0.95–1.00'];
  const CONF_LOWER = [0.75, 0.80, 0.85, 0.90, 0.95];
  const CONF_UPPER = [0.80, 0.85, 0.90, 0.95, 1.00];

  function scoresToBins(scores) {
    const bins = [0, 0, 0, 0, 0];
    for (const s of scores) {
      for (let i = 0; i < CONF_LOWER.length; i++) {
        if (s >= CONF_LOWER[i] && (i === CONF_LOWER.length - 1 ? s <= CONF_UPPER[i] : s < CONF_UPPER[i])) {
          bins[i]++;
          break;
        }
      }
    }
    return bins;
  }

  function initConfidenceChart() {
    const canvas = document.getElementById('chart-confidence');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    const purpleGrad = createVerticalGradient(ctx, 'rgba(139,92,246,0.85)', 'rgba(59,130,246,0.3)');

    confidenceChart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: CONF_BINS,
        datasets: [{
          label: 'Count',
          data: [0, 0, 0, 0, 0],
          backgroundColor: purpleGrad,
          borderColor: C.purple,
          borderWidth: 2,
          borderRadius: 8,
          borderSkipped: false,
          hoverBackgroundColor: 'rgba(139,92,246,0.9)',
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            ...tooltipPlugin,
            callbacks: {
              title: (ctx) => `Confidence: ${ctx[0].label}`,
              label: (ctx) => ` Matches: ${ctx.parsed.y}`,
            },
          },
        },
        scales: {
          x: {
            ...axisDefaults({ grid: { display: false } }),
            title: { display: true, text: 'Confidence Range', color: '#94a3b8', font: { size: 11 } },
          },
          y: {
            ...axisDefaults(),
            beginAtZero: true,
            title: { display: true, text: 'Count', color: '#94a3b8', font: { size: 11 } },
            ticks: {
              color: '#94a3b8',
              font: { size: 11 },
              callback: (v) => Number.isInteger(v) ? v : null,
            },
          },
        },
      },
    });
  }

  /* ── Auto-add timeline point every minute ────────────────────────── */
  let timelineInterval = null;

  function startTimelineAutoUpdate() {
    if (timelineInterval) clearInterval(timelineInterval);
    timelineInterval = setInterval(() => {
      if (!timelineChart) return;
      const now = new Date();
      const label = now.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
      timelineChart.data.labels.push(label);
      timelineChart.data.labels.shift();
      // Keep the last data value as a reference; app.js can call addTimelinePoint
      const lastVal = timelineChart.data.datasets[0].data.at(-1) || 0;
      timelineChart.data.datasets[0].data.push(lastVal);
      timelineChart.data.datasets[0].data.shift();
      timelineChart.update('none');
    }, 60_000);
  }

  /* ── Initialise All Charts ───────────────────────────────────────── */
  function initAll() {
    initTimelineChart();
    initDistributionChart();
    initCameraActivityChart();
    initConfidenceChart();
    startTimelineAutoUpdate();
  }

  /* ── Public API ──────────────────────────────────────────────────── */
  window.chartsManager = {
    init: initAll,

    /**
     * Replace the entire timeline dataset.
     * @param {Array<{label:string, value:number}>} data
     */
    updateTimeline(data) {
      if (!timelineChart) return;
      timelineChart.data.labels         = data.map(d => d.label);
      timelineChart.data.datasets[0].data = data.map(d => d.value);
      timelineChart.update();
    },

    /**
     * Push a single new data point to the timeline (rolling window).
     * @param {number} value
     */
    addTimelinePoint(value) {
      if (!timelineChart) return;
      const now   = new Date();
      const label = now.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
      const data  = timelineChart.data.datasets[0].data;
      const labels = timelineChart.data.labels;
      // Only push a new bucket at a new minute
      if (labels.at(-1) !== label) {
        labels.push(label);
        labels.shift();
        data.push(value);
        data.shift();
      } else {
        data[data.length - 1] = value;
      }
      timelineChart.update('none');
    },

    /**
     * @param {{NEW_VISITOR:number, CROSS_CAMERA_MATCH:number, REENTRY:number, VISITOR_EXITED:number}} counts
     */
    updateDistribution(counts) {
      if (!distributionChart) return;
      distributionChart.data.datasets[0].data = [
        counts.NEW_VISITOR        || 0,
        counts.CROSS_CAMERA_MATCH || 0,
        counts.REENTRY            || 0,
        counts.VISITOR_EXITED     || 0,
      ];
      distributionChart.update();
    },

    /**
     * @param {{ENTRANCE:number, AISLE_1:number, CHECKOUT:number}} cameras
     */
    updateCameraActivity(cameras) {
      if (!cameraActivityChart) return;
      cameraActivityChart.data.datasets[0].data = [
        cameras.ENTRANCE  || 0,
        cameras.AISLE_1   || 0,
        cameras.CHECKOUT  || 0,
      ];
      cameraActivityChart.update();
    },

    /**
     * @param {number[]} scores  Array of raw confidence scores (0–1)
     */
    updateConfidence(scores) {
      if (!confidenceChart) return;
      confidenceChart.data.datasets[0].data = scoresToBins(scores);
      confidenceChart.update();
    },

    /**
     * Rebuild the timeline gradient (call after panel becomes visible).
     */
    resizeTimeline() {
      if (timelineChart) timelineChart.resize();
    },
  };

  // Auto-init once DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
  } else {
    // Slight defer to ensure canvases are visible
    setTimeout(initAll, 50);
  }

})();
