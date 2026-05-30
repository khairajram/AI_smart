"""
api/store_metrics.py
────────────────────
Real-time store metrics computation for GET /stores/{id}/metrics.

Metrics computed
----------------
unique_visitors   : distinct visitor_ids from ENTRY events (staff excluded)
conversion_rate   : fraction of visitors who reached billing zone and did not abandon
avg_dwell_per_zone: average dwell_ms per zone from ZONE_DWELL + ZONE_EXIT events
queue_depth       : most recent queue_depth value from BILLING_QUEUE_JOIN events
abandonment_rate  : BILLING_QUEUE_ABANDON / BILLING_QUEUE_JOIN (per session)
total_entries     : count of ENTRY events
total_exits       : count of EXIT or VISITOR_EXITED events
total_reentries   : count of REENTRY events

Time window
-----------
Default window is "today" (UTC date).  Pass since/until ISO strings
to override.  All computations exclude is_staff=True events.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from api.db import query_events
from api.models import StoreMetrics

logger = logging.getLogger(__name__)

# Event types used for customer metrics (excludes staff-only events)
_BILLING_JOIN     = "BILLING_QUEUE_JOIN"
_BILLING_ABANDON  = "BILLING_QUEUE_ABANDON"
_ENTRY_TYPES      = {"ENTRY", "NEW_VISITOR"}
_EXIT_TYPES       = {"EXIT", "VISITOR_EXITED"}
_DWELL_TYPES      = {"ZONE_DWELL", "ZONE_EXIT"}
_REENTRY_TYPES    = {"REENTRY"}


def _today_window() -> tuple[str, str]:
    """Return (start_of_day_iso, now_iso) in UTC."""
    now  = datetime.now(timezone.utc)
    sod  = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return sod.isoformat().replace("+00:00", "Z"), now.isoformat().replace("+00:00", "Z")


def compute_metrics(
    store_id: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> StoreMetrics:
    """
    Compute real-time store metrics from persisted events.

    Parameters
    ----------
    store_id : Target store identifier
    since    : ISO-8601 start of window (default: start of today UTC)
    until    : ISO-8601 end of window   (default: now)

    Returns
    -------
    StoreMetrics Pydantic model
    """
    if not since or not until:
        _since, _until = _today_window()
        since = since or _since
        until = until or _until

    # Pull all customer events for the window
    rows = query_events(
        store_id=store_id,
        since=since,
        until=until,
        exclude_staff=True,
    )

    # ── Aggregations ──────────────────────────────────────────────────
    unique_visitors: set  = set()
    billing_joiners: set  = set()
    billing_abandons: set = set()
    total_entries  = 0
    total_exits    = 0
    total_reentries = 0

    # zone → list of dwell_ms values
    dwell_by_zone: Dict[str, List[float]] = defaultdict(list)

    # latest queue_depth
    latest_queue_depth: Optional[int] = None

    for row in rows:
        et  = row["event_type"]
        vid = row["visitor_id"]

        if et in _ENTRY_TYPES:
            unique_visitors.add(vid)
            total_entries += 1

        elif et in _EXIT_TYPES:
            total_exits += 1

        elif et in _REENTRY_TYPES:
            unique_visitors.add(vid)   # still counts as a unique visitor
            total_reentries += 1

        elif et in _DWELL_TYPES and row["zone_id"] and row["dwell_ms"]:
            dwell_by_zone[row["zone_id"]].append(float(row["dwell_ms"]))

        elif et == _BILLING_JOIN:
            unique_visitors.add(vid)
            billing_joiners.add(vid)
            # Track latest queue depth
            qd = row["queue_depth"]
            if qd is not None:
                latest_queue_depth = int(qd)

        elif et == _BILLING_ABANDON:
            billing_abandons.add(vid)

    # ── Derived metrics ───────────────────────────────────────────────
    n_visitors = len(unique_visitors)

    # Conversion: billing joiner who did NOT abandon = converted
    converted = billing_joiners - billing_abandons
    conversion_rate = (len(converted) / n_visitors) if n_visitors > 0 else 0.0

    # Average dwell per zone
    avg_dwell_per_zone = {
        zone: round(sum(dwells) / len(dwells), 1)
        for zone, dwells in dwell_by_zone.items()
        if dwells
    }

    # Abandonment rate
    abandonment_rate = (
        len(billing_abandons) / len(billing_joiners)
        if billing_joiners else 0.0
    )

    logger.debug(
        "Metrics computed  store=%s  visitors=%d  conversion=%.3f",
        store_id, n_visitors, conversion_rate,
    )

    return StoreMetrics(
        store_id=store_id,
        window="today",
        unique_visitors=n_visitors,
        conversion_rate=round(conversion_rate, 4),
        avg_dwell_per_zone=avg_dwell_per_zone,
        queue_depth=latest_queue_depth,
        abandonment_rate=round(abandonment_rate, 4),
        total_entries=total_entries,
        total_exits=total_exits,
        total_reentries=total_reentries,
    )
