"""
api/heatmap.py
──────────────
Zone heatmap computation for GET /stores/{id}/heatmap.

Output
------
For each zone that had visitor activity in the window:
  visit_count  : distinct visitors who entered the zone
  avg_dwell_ms : average time spent in the zone per visitor
  score        : normalised 0–100 (100 = most-visited zone)

Normalisation
-------------
score = (zone_visit_count / max_visit_count) * 100
Rounded to 1 decimal place.

Data confidence flag
--------------------
data_confidence = False when total unique sessions < 20.
This signals to the dashboard renderer to show a "low confidence"
indicator rather than rendering a misleading heatmap.

Zone visit sources
------------------
- ZONE_ENTER events:  count distinct visitor per zone
- ZONE_DWELL events:  accumulate dwell_ms per visitor per zone
- ZONE_EXIT events:   accumulate dwell_ms per visitor per zone

Dwell logic: prefer ZONE_DWELL (most accurate, emitted every 30s of
continued dwell) or ZONE_EXIT dwell_ms when available.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from api.db import query_events
from api.models import HeatmapResponse, ZoneHeatmap

logger = logging.getLogger(__name__)

_MIN_SESSIONS_FOR_CONFIDENCE = 20
_ZONE_EVENTS = {"ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL", "BILLING_QUEUE_JOIN"}


def _today_window() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    sod = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return sod.isoformat().replace("+00:00", "Z"), now.isoformat().replace("+00:00", "Z")


def compute_heatmap(
    store_id: str,
    since:    Optional[str] = None,
    until:    Optional[str] = None,
) -> HeatmapResponse:
    """
    Compute zone visit heatmap for a store.

    Parameters
    ----------
    store_id : Target store
    since    : ISO-8601 window start (default: today)
    until    : ISO-8601 window end   (default: now)

    Returns
    -------
    HeatmapResponse sorted by score descending
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
        event_types=list(_ZONE_EVENTS),
    )

    # zone → set of visitor_ids
    zone_visitors: Dict[str, Set[str]] = defaultdict(set)
    # zone → list of dwell_ms values
    zone_dwells:   Dict[str, List[float]] = defaultdict(list)

    for row in rows:
        zone = row["zone_id"]
        if not zone:
            continue
        vid     = row["visitor_id"]
        dwell   = row["dwell_ms"] or 0
        zone_visitors[zone].add(vid)
        if dwell > 0:
            zone_dwells[zone].append(float(dwell))

    if not zone_visitors:
        # No zone data yet — return empty heatmap with low confidence
        total_sessions = 0
        return HeatmapResponse(
            store_id=store_id,
            window="today",
            data_confidence=False,
            zones=[],
        )

    # Count total unique sessions (distinct visitors with any zone event)
    all_visitors: Set[str] = set()
    for visitors in zone_visitors.values():
        all_visitors |= visitors
    total_sessions = len(all_visitors)

    # Build raw zone data
    zone_data: List[Dict] = []
    for zone, visitors in zone_visitors.items():
        vc = len(visitors)
        dwells = zone_dwells.get(zone, [])
        avg_dwell = round(sum(dwells) / len(dwells), 1) if dwells else 0.0
        zone_data.append({
            "zone_id": zone,
            "visit_count": vc,
            "avg_dwell_ms": avg_dwell,
        })

    # Normalise scores to 0–100
    max_visits = max(zd["visit_count"] for zd in zone_data) if zone_data else 1
    zones = [
        ZoneHeatmap(
            zone_id=zd["zone_id"],
            visit_count=zd["visit_count"],
            avg_dwell_ms=zd["avg_dwell_ms"],
            score=round((zd["visit_count"] / max_visits) * 100, 1),
        )
        for zd in zone_data
    ]
    # Sort: highest score first
    zones.sort(key=lambda z: z.score, reverse=True)

    logger.debug(
        "Heatmap computed  store=%s  zones=%d  sessions=%d",
        store_id, len(zones), total_sessions,
    )

    return HeatmapResponse(
        store_id=store_id,
        window="today",
        data_confidence=total_sessions >= _MIN_SESSIONS_FOR_CONFIDENCE,
        zones=zones,
    )
