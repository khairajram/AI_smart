/**
 * socket.js — Socket.io Client Manager
 * ReID Analytics Dashboard
 */

(function () {
  'use strict';

  /* ── Connection Config ──────────────────────────────────────────── */
  const SOCKET_URL = 'http://localhost:3000';
  const RECONNECT_DELAY_MS = 3000;

  /* ── Create Socket Instance ─────────────────────────────────────── */
  const socket = io(SOCKET_URL, {
    transports: ['websocket', 'polling'],
    reconnection: true,
    reconnectionAttempts: Infinity,
    reconnectionDelay: RECONNECT_DELAY_MS,
    reconnectionDelayMax: 10000,
    timeout: 8000,
    autoConnect: true,
  });

  /* ── Internal State ─────────────────────────────────────────────── */
  let _isConnected = false;
  let _reconnectAttempt = 0;

  /* ── DOM Refs (cached lazily) ───────────────────────────────────── */
  const getEl = (id) => document.getElementById(id);

  function setConnectionUI(state) {
    const statusEl = getEl('connectionStatus');
    const labelEl  = getEl('connLabel');
    if (!statusEl || !labelEl) return;

    statusEl.className = 'connection-status';

    switch (state) {
      case 'connected':
        statusEl.classList.add('connected');
        labelEl.textContent = 'Connected';
        _isConnected = true;
        _reconnectAttempt = 0;
        break;
      case 'disconnected':
        statusEl.classList.add('disconnected');
        labelEl.textContent = 'Disconnected';
        _isConnected = false;
        break;
      case 'reconnecting':
        statusEl.classList.add('disconnected');
        labelEl.textContent = `Reconnecting… (${_reconnectAttempt})`;
        _isConnected = false;
        break;
      default:
        labelEl.textContent = 'Connecting…';
    }
  }

  /* ── Socket Lifecycle Events ────────────────────────────────────── */
  socket.on('connect', () => {
    console.log('[Socket] Connected — id:', socket.id);
    setConnectionUI('connected');

    // Notify app.js that the socket is ready
    document.dispatchEvent(new CustomEvent('socket:connected', { detail: { id: socket.id } }));
  });

  socket.on('disconnect', (reason) => {
    console.warn('[Socket] Disconnected — reason:', reason);
    setConnectionUI('disconnected');
    document.dispatchEvent(new CustomEvent('socket:disconnected', { detail: { reason } }));
  });

  socket.on('reconnect_attempt', (attempt) => {
    _reconnectAttempt = attempt;
    setConnectionUI('reconnecting');
    console.log('[Socket] Reconnect attempt', attempt);
  });

  socket.on('reconnect', (attempt) => {
    console.log('[Socket] Reconnected after', attempt, 'attempts');
    setConnectionUI('connected');
    document.dispatchEvent(new CustomEvent('socket:reconnected', { detail: { attempt } }));
  });

  socket.on('reconnect_error', (err) => {
    console.error('[Socket] Reconnect error:', err.message);
  });

  socket.on('connect_error', (err) => {
    console.error('[Socket] Connection error:', err.message);
    setConnectionUI('disconnected');
  });

  /* ── Public API ─────────────────────────────────────────────────── */
  window.socketManager = {
    get socket()       { return socket; },
    get isConnected()  { return _isConnected; },

    /**
     * Register a handler for a socket event.
     * @param {string} eventName
     * @param {Function} handler
     */
    onEvent(eventName, handler) {
      socket.on(eventName, handler);
    },

    /**
     * Emit an event to the server.
     * @param {string} eventName
     * @param {*} data
     */
    emit(eventName, data) {
      if (_isConnected) {
        socket.emit(eventName, data);
      } else {
        console.warn('[Socket] Cannot emit — not connected:', eventName);
      }
    },

    /**
     * Remove a specific listener.
     */
    off(eventName, handler) {
      socket.off(eventName, handler);
    },

    /**
     * Force reconnect.
     */
    reconnect() {
      socket.connect();
    },
  };

  // Set initial UI state
  setConnectionUI('connecting');
  console.log('[Socket] Initialised — connecting to', SOCKET_URL);

})();
