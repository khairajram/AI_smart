"""
api/server.py
─────────────
Store Intelligence API — FastAPI application.

Endpoints
---------
POST /events/ingest                — Ingest up to 500 events (idempotent by event_id)
GET  /stores/{store_id}/metrics    — Real-time store KPIs
GET  /stores/{store_id}/funnel     — Conversion funnel with drop-off percentages
GET  /stores/{store_id}/heatmap    — Zone heatmap (visit frequency + avg dwell)
GET  /stores/{store_id}/anomalies  — Active anomalies (queue spike, conversion drop, dead zone)
GET  /health                       — Service health + per-store STALE_FEED detection
GET  /metrics                      — Legacy ReID subsystem metrics (VisitorRegistry counters)
GET  /registry/active              — Active visitor snapshot
GET  /registry/exited              — Exited visitor snapshot (within re-entry window)
POST /registry/reset               — ⚠️ Admin: clear all registry + event state
GET  /config                       — Read-only configuration dump
GET  /prometheus                   — Prometheus scrape endpoint

Production notes
----------------
* Structured request logging via StructuredLoggingMiddleware (trace_id, latency_ms, store_id)
* Graceful 503 when DB is unavailable — no raw stack traces in responses
* CORS enabled for dashboard integration
* SQLite DB initialised on startup via FastAPI lifespan handler
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import structlog
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from api.anomalies import detect_anomalies
from api.db import event_count, get_last_event_per_store, init_db
from api.funnel import compute_funnel
from api.heatmap import compute_heatmap
from api.ingestion import ingest_events
from api.middleware import StructuredLoggingMiddleware
from api.models import (
    AnomalyResponse,
    FunnelResponse,
    HeatmapResponse,
    IngestRequest,
    IngestResponse,
    StoreHealth,
    StoreMetrics,
)
from api.store_metrics import compute_metrics
from config.settings import settings

# ── Import ReID registry with graceful fallback ──────────────────────
# If torchreid or torch is not available (e.g. stripped Docker image),
# the API still starts — only live camera endpoints are degraded.
try:
    from reid.registry import VisitorRegistry
    _REID_AVAILABLE = True
except Exception as _reid_import_err:
    VisitorRegistry = None  # type: ignore[misc,assignment]
    _REID_AVAILABLE = False
    logging.warning("ReID subsystem unavailable: %s", _reid_import_err)

# ── Configure structlog for JSON output (required by challenge) ───────
def _configure_logging(log_format: str = "json") -> None:
    """Set up structlog with JSON or console rendering."""
    shared_processors = [
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]
    if log_format == "json":
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(),
        ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


# Configure at import time so all loggers get JSON format from the start
_configure_logging(log_format=settings.LOG_FORMAT)

logger = structlog.get_logger(__name__)

# ── Module-level shared state (injected at startup) ──────────────────
_registry:     Optional[VisitorRegistry] = None
_startup_time: float = 0.0
_STALE_FEED_SECONDS = 600   # 10 minutes


# ─────────────────────────────────────────────────────────────────────
#  Legacy response models (kept for backwards compatibility)
# ─────────────────────────────────────────────────────────────────────

class ReIDMetricsResponse(BaseModel):
    active_visitors:        int
    exited_visitors_cached: int
    total_unique_visitors:  int
    total_reentries:        int
    total_cross_camera:     int
    total_exits_recorded:   int


class ResetResponse(BaseModel):
    message:   str
    timestamp: float


# ─────────────────────────────────────────────────────────────────────
#  App factory
# ─────────────────────────────────────────────────────────────────────

def create_app(registry: Optional[Any] = None) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Parameters
    ----------
    registry : Optional VisitorRegistry — injected by main.py for legacy endpoints.
               May be None when running the API standalone (e.g. docker-compose).
    """
    global _registry, _startup_time
    _registry = registry

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _startup_time
        _startup_time = time.time()
        # Initialise SQLite schema on every startup (idempotent)
        try:
            init_db()
            logger.info("Store Intelligence DB ready", total_events=event_count())
        except Exception as exc:
            logger.error("DB init failed", error=str(exc))
            # Don't crash — let /health report the problem
        yield
        logger.info("Store Intelligence API shutting down")

    app = FastAPI(
        title="Store Intelligence API",
        description=(
            "Real-time retail analytics API.\n\n"
            "Ingests CCTV-derived behavioural events and exposes:\n"
            "- Store KPI metrics (visitors, conversion, dwell, queues)\n"
            "- Conversion funnel analysis\n"
            "- Zone heatmaps\n"
            "- Operational anomaly detection\n"
            "- Person re-identification (cross-camera deduplication)"
        ),
        version="2.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # ── Middleware ────────────────────────────────────────────────────
    app.add_middleware(StructuredLoggingMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
        expose_headers=["X-Trace-ID", "X-Event-Count"],
    )

    # ── Global exception handler — no raw stack traces ────────────────
    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        logger.error("Unhandled exception", path=request.url.path, error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "detail": str(exc)},
        )

    # ─────────────────────────────────────────────────────────────────
    #  Stage 3 — Intelligence API endpoints (challenge-required)
    # ─────────────────────────────────────────────────────────────────

    @app.post(
        "/events/ingest",
        response_model=IngestResponse,
        status_code=status.HTTP_200_OK,
        tags=["Ingest"],
        summary="Ingest up to 500 behavioural events",
    )
    async def events_ingest(
        request: Request,
        body:    IngestRequest,
    ) -> IngestResponse:
        """
        Accepts a batch of up to 500 events from the detection pipeline.

        * Validates each event against the schema — rejects malformed events.
        * Deduplicates by event_id — safe to call twice with the same payload.
        * Returns partial-success: accepted + rejected + duplicate counts.
        * Errors per rejected event are included in the response body.
        """
        try:
            raw_events = body.events
            result = ingest_events(raw_events)
            # Expose event_count for middleware logging
            request.state.event_count = result.accepted + result.duplicate
            response = JSONResponse(
                content=result.model_dump(),
                headers={"X-Event-Count": str(len(body.events))},
            )
            return response  # type: ignore[return-value]
        except Exception as exc:
            logger.error("Ingest failed", error=str(exc))
            raise HTTPException(status_code=503, detail={"error": "Ingest unavailable", "detail": str(exc)})

    @app.get(
        "/stores/{store_id}/metrics",
        response_model=StoreMetrics,
        tags=["Analytics"],
        summary="Real-time store KPI metrics",
    )
    async def store_metrics(
        store_id: str,
        since:    Optional[str] = Query(default=None, description="ISO-8601 window start"),
        until:    Optional[str] = Query(default=None, description="ISO-8601 window end"),
    ) -> StoreMetrics:
        """
        Returns today's store KPIs (staff excluded):

        * `unique_visitors` — distinct visitor sessions (re-entries not double-counted)
        * `conversion_rate` — fraction who reached billing and did not abandon
        * `avg_dwell_per_zone` — average time spent per zone in milliseconds
        * `queue_depth` — current billing queue depth (null if no billing events)
        * `abandonment_rate` — BILLING_QUEUE_ABANDON / BILLING_QUEUE_JOIN
        """
        try:
            return compute_metrics(store_id, since, until)
        except Exception as exc:
            logger.error("Metrics failed", store_id=store_id, error=str(exc))
            raise HTTPException(status_code=503, detail={"error": "Metrics unavailable", "detail": str(exc)})

    @app.get(
        "/stores/{store_id}/funnel",
        response_model=FunnelResponse,
        tags=["Analytics"],
        summary="Visitor conversion funnel",
    )
    async def store_funnel(
        store_id: str,
        since:    Optional[str] = Query(default=None),
        until:    Optional[str] = Query(default=None),
    ) -> FunnelResponse:
        """
        Conversion funnel: Entry → Zone Visit → Billing Queue → Purchase.

        * Session is the unit — not raw events.
        * Re-entries are deduplicated: same visitor_id counted once.
        * Drop-off % shown at each stage transition.
        """
        try:
            return compute_funnel(store_id, since, until)
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"error": "Funnel unavailable", "detail": str(exc)})

    @app.get(
        "/stores/{store_id}/heatmap",
        response_model=HeatmapResponse,
        tags=["Analytics"],
        summary="Zone visit frequency heatmap",
    )
    async def store_heatmap(
        store_id: str,
        since:    Optional[str] = Query(default=None),
        until:    Optional[str] = Query(default=None),
    ) -> HeatmapResponse:
        """
        Zone heatmap with normalised 0–100 scores.

        * `score` 100 = most visited zone in the window.
        * `data_confidence` = false when fewer than 20 sessions — heatmap may be misleading.
        * Sorted by score descending.
        """
        try:
            return compute_heatmap(store_id, since, until)
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"error": "Heatmap unavailable", "detail": str(exc)})

    @app.get(
        "/stores/{store_id}/anomalies",
        response_model=AnomalyResponse,
        tags=["Analytics"],
        summary="Active operational anomalies",
    )
    async def store_anomalies(
        store_id: str,
        since:    Optional[str] = Query(default=None),
        until:    Optional[str] = Query(default=None),
    ) -> AnomalyResponse:
        """
        Detect active operational anomalies:

        * `BILLING_QUEUE_SPIKE` — queue depth exceeds threshold (WARN/CRITICAL)
        * `CONVERSION_DROP` — today's rate below 7-day average (WARN/CRITICAL)
        * `DEAD_ZONE` — zone silent for 30+ min despite earlier traffic (INFO)

        Each anomaly includes a `suggested_action` string for on-call use.
        """
        try:
            return detect_anomalies(store_id, since, until)
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"error": "Anomaly detection unavailable", "detail": str(exc)})

    # ─────────────────────────────────────────────────────────────────
    #  Health endpoint (enhanced with per-store STALE_FEED)
    # ─────────────────────────────────────────────────────────────────

    @app.get("/health", tags=["System"], summary="Service health + per-store feed status")
    async def health() -> Dict[str, Any]:
        """
        Service liveness and readiness probe.

        Per-store status:
        * `ok` — last event was within 10 minutes
        * `STALE_FEED` — last event was more than 10 minutes ago
        * `NO_DATA` — store has no ingested events yet
        """
        uptime = round(time.time() - _startup_time, 2)

        try:
            store_last_events = get_last_event_per_store()
            total_events = event_count()
        except Exception as exc:
            # DB unavailable — return 503 with structured body
            return JSONResponse(
                status_code=503,
                content={
                    "status": "degraded",
                    "error": "Database unavailable",
                    "detail": str(exc),
                    "uptime_seconds": uptime,
                    "version": "2.0.0",
                },
            )

        now_ts = time.time()
        stores: Dict[str, Dict[str, Any]] = {}

        for store_id, last_ts in store_last_events.items():
            try:
                # Parse ISO-8601 and compare to now
                from datetime import datetime, timezone
                last_dt    = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                age_s      = now_ts - last_dt.timestamp()
                feed_status = "STALE_FEED" if age_s > _STALE_FEED_SECONDS else "ok"
            except Exception:
                feed_status = "ok"

            stores[store_id] = {
                "last_event_timestamp": last_ts,
                "status": feed_status,
            }

        return {
            "status":         "ok",
            "uptime_seconds": uptime,
            "version":        "2.0.0",
            "total_events":   total_events,
            "stores":         stores,
        }

    # ─────────────────────────────────────────────────────────────────
    #  Legacy ReID subsystem endpoints (preserved for compatibility)
    # ─────────────────────────────────────────────────────────────────

    @app.get(
        "/metrics",
        response_model=ReIDMetricsResponse,
        tags=["ReID"],
        summary="ReID subsystem counters",
    )
    async def reid_metrics() -> ReIDMetricsResponse:
        """Real-time ReID counters (cross-camera matches, re-entries, etc.)."""
        if _registry is None:
            raise HTTPException(status_code=503, detail="ReID registry not initialised")
        data = _registry.get_metrics()
        return ReIDMetricsResponse(**data)

    @app.get(
        "/registry/active",
        response_model=List[Dict[str, Any]],
        tags=["ReID"],
        summary="Active visitor snapshot",
    )
    async def registry_active() -> List[Dict[str, Any]]:
        """All visitors currently active (in-store) across all cameras."""
        if _registry is None:
            raise HTTPException(status_code=503, detail="ReID registry not initialised")
        return _registry.snapshot_active()

    @app.get(
        "/registry/exited",
        response_model=List[Dict[str, Any]],
        tags=["ReID"],
        summary="Exited visitor snapshot",
    )
    async def registry_exited() -> List[Dict[str, Any]]:
        """Visitors who exited and are still within the re-entry window."""
        if _registry is None:
            raise HTTPException(status_code=503, detail="ReID registry not initialised")
        return _registry.snapshot_exited()

    @app.post(
        "/registry/reset",
        response_model=ResetResponse,
        tags=["Admin"],
        summary="⚠️ Clear all registry + event state",
    )
    async def registry_reset() -> ResetResponse:
        """Clear the ReID registry and all ingested events. Irreversible."""
        if _registry is not None:
            _registry.reset()
        # Also clear the SQLite event store
        try:
            from api.db import get_db
            with get_db() as conn:
                conn.execute("DELETE FROM events")
        except Exception:
            pass
        return ResetResponse(
            message="Registry and event store cleared",
            timestamp=time.time(),
        )

    @app.get("/config", tags=["System"], summary="Read-only configuration dump")
    async def config_dump() -> Dict[str, Any]:
        """Current configuration — no secrets exposed."""
        return {
            "reid_similarity_threshold":    settings.REID_SIMILARITY_THRESHOLD,
            "reentry_similarity_threshold": settings.REENTRY_SIMILARITY_THRESHOLD,
            "reentry_window_seconds":       settings.REENTRY_WINDOW_SECONDS,
            "active_visitor_timeout":       settings.ACTIVE_VISITOR_TIMEOUT_SECONDS,
            "osnet_model":                  settings.OSNET_MODEL_NAME,
            "yolo_model":                   settings.YOLO_MODEL,
            "reid_device":                  settings.REID_DEVICE,
            "event_publisher":              settings.EVENT_PUBLISHER,
        }

    @app.get(
        "/prometheus",
        response_class=PlainTextResponse,
        tags=["Observability"],
        summary="Prometheus scrape endpoint",
    )
    async def prometheus_metrics() -> str:
        """Prometheus text-format metrics for Grafana / alertmanager."""
        lines = []

        # ReID registry metrics (if available)
        if _registry is not None:
            m = _registry.get_metrics()
            lines += [
                "# HELP reid_active_visitors Currently active visitors",
                "# TYPE reid_active_visitors gauge",
                f"reid_active_visitors {m['active_visitors']}",
                "# HELP reid_unique_visitors_total Total unique visitors",
                "# TYPE reid_unique_visitors_total counter",
                f"reid_unique_visitors_total {m['total_unique_visitors']}",
                "# HELP reid_reentries_total Total re-entry events",
                "# TYPE reid_reentries_total counter",
                f"reid_reentries_total {m['total_reentries']}",
                "# HELP reid_cross_camera_total Total cross-camera matches",
                "# TYPE reid_cross_camera_total counter",
                f"reid_cross_camera_total {m['total_cross_camera']}",
            ]

        # Event store metrics
        try:
            total_ev = event_count()
            lines += [
                "# HELP store_events_total Total events ingested",
                "# TYPE store_events_total counter",
                f"store_events_total {total_ev}",
            ]
        except Exception:
            pass

        lines.append("")
        return "\n".join(lines)

    return app


# ─────────────────────────────────────────────────────────────────────
#  Standalone server runner
# ─────────────────────────────────────────────────────────────────────

def run_server(registry: Optional[Any] = None) -> None:
    """Start uvicorn in-process (blocking). Called by main.py --serve flag."""
    import uvicorn
    app = create_app(registry)
    uvicorn.run(
        app,
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=settings.API_RELOAD,
        log_level=settings.LOG_LEVEL.lower(),
    )
