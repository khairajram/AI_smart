"""
api/ingestion.py
────────────────
Event ingestion logic for POST /events/ingest.

Responsibilities
----------------
* Validate each event against the StoreEvent Pydantic schema
* Deduplicate against the DB (idempotent by event_id)
* Persist accepted events to SQLite
* Return partial-success response: accepted / rejected / duplicate counts

Idempotency guarantee
---------------------
Calling POST /events/ingest twice with the same payload is safe.
The DB layer uses INSERT OR IGNORE on event_id, so duplicate events
are counted and reported but never double-written.

Batch size limit
----------------
Up to 500 events per request (enforced by Pydantic IngestRequest model).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from pydantic import ValidationError

from api.db import insert_events
from api.models import IngestErrorDetail, IngestRequest, IngestResponse, StoreEvent

logger = logging.getLogger(__name__)


def ingest_events(raw_events: List[Dict[str, Any]]) -> IngestResponse:
    """
    Validate and persist a batch of raw event dictionaries.

    Parameters
    ----------
    raw_events : list of dicts from the request body (pre-parsed JSON)

    Returns
    -------
    IngestResponse with accepted / rejected / duplicate / errors
    """
    validated: List[StoreEvent] = []
    errors:    List[IngestErrorDetail] = []

    # ── 1. Per-event validation ──────────────────────────────────────
    for i, raw in enumerate(raw_events):
        eid = raw.get("event_id", f"<index:{i}>")
        try:
            event = StoreEvent.model_validate(raw)
            validated.append(event)
        except ValidationError as exc:
            # Collect all field errors for this event
            error_msgs = "; ".join(
                f"{e['loc']}: {e['msg']}" for e in exc.errors()
            )
            logger.warning(
                "Event validation failed  event_id=%s  errors=%s", eid, error_msgs
            )
            errors.append(IngestErrorDetail(event_id=str(eid), error=error_msgs))

    rejected = len(errors)

    # ── 2. Persist validated events (idempotent) ─────────────────────
    if validated:
        event_dicts = [e.model_dump() for e in validated]
        counts = insert_events(event_dicts)
        accepted  = counts["accepted"]
        duplicate = counts["duplicate"]
    else:
        accepted  = 0
        duplicate = 0

    logger.info(
        "Ingest complete  total=%d  accepted=%d  duplicate=%d  rejected=%d",
        len(raw_events), accepted, duplicate, rejected,
    )

    return IngestResponse(
        accepted=accepted,
        rejected=rejected,
        duplicate=duplicate,
        errors=errors,
    )
