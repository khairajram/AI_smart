"""
reid/events.py
──────────────
ReID event schema definition and publishing infrastructure.

Every identity decision made by the VisitorRegistry is published as a
structured JSON event so downstream services (analytics, dashboards,
alerting) can consume real-time updates.

Event Types
-----------
NEW_VISITOR        — First time a person is detected; a new visitor_id is created.
CROSS_CAMERA_MATCH — The same person appeared in a different camera.
                     Existing visitor_id is reused; no new session created.
REENTRY            — A person re-entered the store within the reentry window.
                     Original visitor_id is reused.
VISITOR_EXITED     — A tracked person has left the frame for more than
                     ACTIVE_VISITOR_TIMEOUT_SECONDS.

Publishers
----------
stdout  — prints JSON lines to standard output (default, production-safe,
          pipe-friendly)
redis   — publishes to a Redis Pub/Sub channel for distributed consumers
"""

from __future__ import annotations

import json
import logging
import time
from enum import Enum
from typing import Any, Dict, Optional
from uuid import uuid4

from config.settings import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
#  Event type enumeration
# ─────────────────────────────────────────────────────────────────────

class EventType(str, Enum):
    # ── Challenge-defined event types (emitted in final events) ──────
    ENTRY                 = "ENTRY"
    EXIT                  = "EXIT"
    ZONE_ENTER            = "ZONE_ENTER"
    ZONE_EXIT             = "ZONE_EXIT"
    ZONE_DWELL            = "ZONE_DWELL"
    BILLING_QUEUE_JOIN    = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY               = "REENTRY"
    # ── Internal ReID events (used inside VisitorRegistry only) ──────
    NEW_VISITOR        = "NEW_VISITOR"
    CROSS_CAMERA_MATCH = "CROSS_CAMERA_MATCH"
    VISITOR_EXITED     = "VISITOR_EXITED"


# Maps internal ReID event types → challenge schema event types
# so every published event validates against the challenge schema.
_INTERNAL_TO_CHALLENGE: dict = {
    EventType.NEW_VISITOR:    EventType.ENTRY,
    EventType.VISITOR_EXITED: EventType.EXIT,
    # REENTRY, CROSS_CAMERA_MATCH stay as-is (both are in challenge catalogue)
}


# ─────────────────────────────────────────────────────────────────────
#  Event schema
# ─────────────────────────────────────────────────────────────────────

def build_event(
    event_type: EventType,
    visitor_id: str,
    camera_id: str,
    track_id: int,
    reid_confidence: float,
    store_id: str = "UNKNOWN_STORE",
    timestamp: Optional[float] = None,
    zone_id: Optional[str] = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    queue_depth: Optional[int] = None,
    session_seq: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Construct a fully-populated event dictionary that matches the
    challenge schema exactly.

    Internal event types are automatically mapped to challenge types:
        NEW_VISITOR    → ENTRY
        VISITOR_EXITED → EXIT

    Parameters
    ----------
    event_type      : ReID or challenge EventType
    visitor_id      : Global stable visitor identifier (UUID)
    camera_id       : Source camera (e.g. "CAM_ENTRY_01")
    track_id        : ByteTrack per-camera track ID
    reid_confidence : Cosine similarity score (0–1)
    store_id        : Store identifier from store_layout.json
    timestamp       : Unix epoch seconds (defaults to now)
    zone_id         : Zone name from store_layout.json (None for ENTRY/EXIT)
    dwell_ms        : Duration in zone (ms); 0 for instantaneous events
    is_staff        : True if StaffDetector classified this track as staff
    queue_depth     : Current billing queue depth (BILLING_QUEUE_JOIN only)
    session_seq     : Ordinal position of this event in the visitor session
    extra           : Additional payload fields

    Returns
    -------
    dict : JSON-serialisable event payload matching challenge schema
    """
    # Map internal types to challenge schema types
    challenge_type = _INTERNAL_TO_CHALLENGE.get(event_type, event_type)

    ts = timestamp if timestamp is not None else time.time()
    # Convert unix epoch → ISO-8601 UTC string (challenge schema requires this)
    from datetime import datetime, timezone as _tz
    ts_iso = datetime.fromtimestamp(ts, tz=_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    event: Dict[str, Any] = {
        "event_id":   str(uuid4()),
        "store_id":   store_id,
        "camera_id":  camera_id,
        "visitor_id": visitor_id,
        "event_type": challenge_type.value if hasattr(challenge_type, "value") else str(challenge_type),
        "timestamp":  ts_iso,
        "zone_id":    zone_id,
        "dwell_ms":   dwell_ms,
        "is_staff":   is_staff,
        "confidence": round(float(reid_confidence), 4),
        "metadata": {
            "queue_depth":  queue_depth,
            "sku_zone":     zone_id,   # mirror zone_id as sku_zone label
            "session_seq":  session_seq,
            # Internal fields preserved for debugging
            "_track_id":    track_id,
            "_reid_confidence": round(float(reid_confidence), 4),
        },
    }
    if extra:
        event["metadata"].update(extra)
    return event


# ─────────────────────────────────────────────────────────────────────
#  Publisher abstraction
# ─────────────────────────────────────────────────────────────────────

class EventPublisher:
    """
    Abstract base class for event publishers.

    Subclasses implement the `publish` method for different transports.
    """

    def publish(self, event: Dict[str, Any]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class StdoutPublisher(EventPublisher):
    """
    Publishes events as JSON lines to stdout.

    Ideal for:
    - Docker containers (stdout → log aggregator)
    - Development / debugging
    - Piping into jq or other stream processors
    """

    def publish(self, event: Dict[str, Any]) -> None:
        line = json.dumps(event, separators=(",", ":"))
        print(line, flush=True)
        logger.debug("Published event: %s  visitor=%s", event["event_type"], event["visitor_id"])


class RedisPublisher(EventPublisher):
    """
    Publishes events to a Redis Pub/Sub channel.

    Requires: pip install redis
    Configure via REDIS_URL and REDIS_CHANNEL environment variables.
    """

    def __init__(self) -> None:
        self._client = None
        self._channel = settings.REDIS_CHANNEL

    def _get_client(self):
        if self._client is None:
            try:
                import redis
                self._client = redis.from_url(settings.REDIS_URL, decode_responses=True)
                self._client.ping()
                logger.info(
                    "Redis publisher connected to %s  channel=%s",
                    settings.REDIS_URL, self._channel,
                )
            except Exception as exc:
                logger.error("Redis connection failed: %s — falling back to stdout", exc)
                return None
        return self._client

    def publish(self, event: Dict[str, Any]) -> None:
        client = self._get_client()
        if client is None:
            # Graceful fallback
            StdoutPublisher().publish(event)
            return
        try:
            payload = json.dumps(event, separators=(",", ":"))
            client.publish(self._channel, payload)
        except Exception as exc:
            logger.warning("Redis publish failed: %s", exc)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass


class HttpPublisher(EventPublisher):
    """
    Publishes events to the Store Intelligence API via HTTP POST.
    Batches events up to 50 or flushes on interval.
    """

    def __init__(self) -> None:
        import threading
        import os
        self.batch: List[Dict[str, Any]] = []
        self.lock = threading.RLock()
        
        # Use STORE_API_URL if provided, else try to use 'api' if in docker, else localhost
        default_host = "api" if os.environ.get("EVENT_PUBLISHER") == "http" and os.environ.get("API_HOST") == "0.0.0.0" else "127.0.0.1"
        base_url = os.environ.get("STORE_API_URL", f"http://{default_host}:8000")
        self.api_url = f"{base_url}/events/ingest"

    def publish(self, event: Dict[str, Any]) -> None:
        with self.lock:
            self.batch.append(event)
            # Flush if batch is large enough
            if len(self.batch) >= 1:
                self.flush()

    def flush(self) -> None:
        if not self.batch:
            return
        import urllib.request
        import urllib.error
        
        with self.lock:
            payload = json.dumps({"events": self.batch}).encode('utf-8')
            self.batch.clear()

        req = urllib.request.Request(
            self.api_url, 
            data=payload, 
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        try:
            with urllib.request.urlopen(req, timeout=5.0) as response:
                body = response.read().decode('utf-8')
                logger.info(f"HttpPublisher POST {self.api_url} -> {body}")
        except Exception as exc:
            logger.warning(f"HttpPublisher failed to POST to {self.api_url}: {exc}")

    def close(self) -> None:
        self.flush()


def create_publisher() -> EventPublisher:
    """
    Factory — creates the correct publisher based on settings.EVENT_PUBLISHER.
    """
    if settings.EVENT_PUBLISHER == "redis":
        return RedisPublisher()
    elif settings.EVENT_PUBLISHER == "http":
        return HttpPublisher()
    return StdoutPublisher()
