"""
api/funnel.py
─────────────
Conversion funnel computation for GET /stores/{id}/funnel.

Funnel stages
-------------
1. Entry         — visitors who crossed the entry threshold (ENTRY event)
2. Zone Visit    — visitors who entered at least one named zone (ZONE_ENTER)
3. Billing Queue — visitors who joined the billing queue (BILLING_QUEUE_JOIN)
4. Purchase      — billing joiners who did NOT abandon (proxy for POS conversion)

Session semantics
-----------------
The unit of analysis is the visitor session, not raw events.
Re-entries (REENTRY events) are de-duplicated: if a visitor_id appears
in both ENTRY and REENTRY events, they count ONCE in the funnel.

This prevents the known "re-entry inflation" vendor problem described
in the challenge: the same physical person re-entering must not add a
second funnel entry.

Drop-off computation
--------------------
drop_off_pct at stage N = (stage[N-1].visitors - stage[N].visitors)
                         / stage[N-1].visitors * 100
First stage always has 0 % drop-off.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from api.db import query_events
from api.models import FunnelResponse, FunnelStage

logger = logging.getLogger(__name__)

_ENTRY_TYPES   = {"ENTRY", "NEW_VISITOR", "REENTRY"}
_ZONE_TYPES    = {"ZONE_ENTER"}
_BILLING_JOIN  = "BILLING_QUEUE_JOIN"
_BILLING_ABAND = "BILLING_QUEUE_ABANDON"


def _today_window() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    sod = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return sod.isoformat().replace("+00:00", "Z"), now.isoformat().replace("+00:00", "Z")


def compute_funnel(
    store_id: str,
    since:    Optional[str] = None,
    until:    Optional[str] = None,
) -> FunnelResponse:
    """
    Compute the visitor conversion funnel for a store.

    Parameters
    ----------
    store_id : Target store identifier
    since    : ISO-8601 window start (default: today)
    until    : ISO-8601 window end   (default: now)

    Returns
    -------
    FunnelResponse with stages list
    """
    if not since or not until:
        _since, _until = _today_window()
        since = since or _since
        until = until or _until

    rows = query_events(
        store_id=store_id,
        since=since,
        until=until,
        exclude_staff=True,
    )

    # ── Collect per-visitor event sets ────────────────────────────────
    entry_visitors:   set = set()
    zone_visitors:    set = set()
    billing_joiners:  set = set()
    billing_abandons: set = set()

    for row in rows:
        et  = row["event_type"]
        vid = row["visitor_id"]

        if et in _ENTRY_TYPES:
            entry_visitors.add(vid)

        elif et in _ZONE_TYPES:
            zone_visitors.add(vid)
            entry_visitors.add(vid)   # ensure they are counted in entry stage too

        elif et == _BILLING_JOIN:
            billing_joiners.add(vid)
            entry_visitors.add(vid)
            zone_visitors.add(vid)    # must have passed through zones

        elif et == _BILLING_ABAND:
            billing_abandons.add(vid)

    # ── Build funnel stages ───────────────────────────────────────────
    # Purchase = billing joiners who did NOT abandon
    purchase_visitors = billing_joiners - billing_abandons

    counts = [
        ("entry",         len(entry_visitors)),
        ("zone_visit",    len(zone_visitors)),
        ("billing_queue", len(billing_joiners)),
        ("purchase",      len(purchase_visitors)),
    ]

    stages: List[FunnelStage] = []
    for i, (stage_name, n) in enumerate(counts):
        prev = counts[i - 1][1] if i > 0 else n
        if prev > 0 and i > 0:
            drop_off_pct = round((prev - n) / prev * 100, 1)
        else:
            drop_off_pct = 0.0

        stages.append(FunnelStage(
            stage=stage_name,
            visitors=n,
            drop_off_pct=max(0.0, drop_off_pct),
        ))

    logger.debug(
        "Funnel computed  store=%s  entry=%d  zone=%d  billing=%d  purchase=%d",
        store_id, counts[0][1], counts[1][1], counts[2][1], counts[3][1],
    )

    return FunnelResponse(
        store_id=store_id,
        window="today",
        stages=stages,
    )
