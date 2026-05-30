"""
api/middleware.py
─────────────────
Structured request-logging middleware.

Every HTTP request through the Store Intelligence API is logged with:
    trace_id    — UUID v4 generated per request (X-Trace-ID header echoed back)
    store_id    — extracted from /stores/{id}/* path segments (if present)
    endpoint    — METHOD /path
    latency_ms  — wall-clock time for the full request
    event_count — number of events (POST /events/ingest only)
    status_code — HTTP response status

The trace_id is also injected into structlog context so all log lines
emitted during the request carry it automatically.
"""

from __future__ import annotations

import logging
import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

logger = structlog.get_logger(__name__)


def _extract_store_id(path: str) -> str:
    """
    Parse store_id from paths like /stores/STORE_BLR_002/metrics.
    Returns empty string if not a store-scoped endpoint.
    """
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "stores":
        return parts[1]
    return ""


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    """
    ASGI middleware that wraps every request with trace_id + latency logging.
    Compatible with FastAPI / Starlette.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        trace_id = str(uuid.uuid4())
        store_id = _extract_store_id(request.url.path)

        # Inject into structlog context for this request's duration
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            trace_id=trace_id,
            store_id=store_id or None,
        )

        t0 = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception as exc:
            latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            logger.error(
                "request_error",
                method=request.method,
                path=request.url.path,
                latency_ms=latency_ms,
                error=str(exc),
            )
            raise

        latency_ms = round((time.perf_counter() - t0) * 1000, 1)

        # Extract event_count from ingest requests (set by ingest handler)
        event_count = response.headers.get("X-Event-Count", "")

        logger.info(
            "request",
            method=request.method,
            endpoint=f"{request.method} {request.url.path}",
            status_code=response.status_code,
            latency_ms=latency_ms,
            event_count=int(event_count) if event_count else None,
        )

        # Echo trace_id back to caller for end-to-end tracing
        response.headers["X-Trace-ID"] = trace_id
        return response
