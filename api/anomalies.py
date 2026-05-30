"""
api/anomalies.py
────────────────
Real-time anomaly detection for GET /stores/{id}/anomalies.

Detected anomaly types
----------------------
BILLING_QUEUE_SPIKE
    Triggered when the latest queue_depth from BILLING_QUEUE_JOIN events
    exceeds the configured spike threshold (default: 5).
    Severity: WARN at threshold, CRITICAL at 2× threshold.

CONVERSION_DROP
    Triggered when today's conversion_rate falls below 80 % of the
    7-day average conversion_rate for this store.
    Severity: WARN at <80 %, CRITICAL at <60 %.
    Requires at least 7 days of event history to activate.

DEAD_ZONE
    Triggered when a zone that received traffic in the past 2 hours has
    had no ZONE_ENTER events in the last 30 minutes.
    Severity: INFO.
    Only fires if the store has had any traffic today.

STALE_FEED (reported by /health, not /anomalies)
    See api/server.py /health endpoint.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from api.db import query_events
from api.models import Anomaly, AnomalyResponse, AnomalySeverity
from api.store_metrics import compute_metrics

logger = logging.getLogger(__name__)

# ── Thresholds (could be env-var driven in a future iteration) ────────
QUEUE_SPIKE_WARN     = 5    # queue depth ≥ this → WARN
QUEUE_SPIKE_CRITICAL = 10   # queue depth ≥ this → CRITICAL
CONVERSION_DROP_WARN = 0.80  # today < 7-day avg × this → WARN
CONVERSION_DROP_CRIT = 0.60  # today < 7-day avg × this → CRITICAL
DEAD_ZONE_WINDOW_MIN = 30    # no visits in this many minutes → dead zone
DEAD_ZONE_LOOKBACK_H = 2     # only flag zones active in this lookback


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _today_window() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    sod = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return _iso(sod), _iso(now)


# ─────────────────────────────────────────────────────────────────────
#  Individual anomaly detectors
# ─────────────────────────────────────────────────────────────────────

def _detect_queue_spike(store_id: str, since: str, until: str) -> Optional[Anomaly]:
    """Detect billing queue spike from recent BILLING_QUEUE_JOIN events."""
    rows = query_events(
        store_id=store_id,
        since=since,
        until=until,
        event_types=["BILLING_QUEUE_JOIN"],
        exclude_staff=True,
    )
    if not rows:
        return None

    # Use the latest queue_depth value
    latest_depth: Optional[int] = None
    for row in reversed(rows):
        if row["queue_depth"] is not None:
            latest_depth = int(row["queue_depth"])
            break

    if latest_depth is None or latest_depth < QUEUE_SPIKE_WARN:
        return None

    if latest_depth >= QUEUE_SPIKE_CRITICAL:
        severity = AnomalySeverity.CRITICAL
        action   = (
            f"Queue depth is critically high ({latest_depth}). "
            "Open all available billing counters immediately and call supervisor."
        )
    else:
        severity = AnomalySeverity.WARN
        action   = (
            f"Queue depth is {latest_depth}. "
            "Consider opening an additional billing counter."
        )

    return Anomaly(
        anomaly_type="BILLING_QUEUE_SPIKE",
        severity=severity,
        detail=f"Current queue depth: {latest_depth} (warn threshold: {QUEUE_SPIKE_WARN})",
        suggested_action=action,
        detected_at=_now_iso(),
    )


def _detect_conversion_drop(store_id: str) -> Optional[Anomaly]:
    """
    Compare today's conversion rate against the 7-day trailing average.
    Only triggers when there's sufficient historical data (7+ days).
    """
    now = datetime.now(timezone.utc)

    # Today's conversion
    sod = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_metrics = compute_metrics(store_id, _iso(sod), _iso(now))
    today_rate    = today_metrics.conversion_rate

    # 7-day trailing window (excluding today)
    seven_days_ago = sod - timedelta(days=7)
    hist_metrics   = compute_metrics(store_id, _iso(seven_days_ago), _iso(sod))
    hist_rate      = hist_metrics.conversion_rate

    # Need meaningful historical data
    if hist_metrics.unique_visitors < 20 or hist_rate == 0:
        return None

    ratio = today_rate / hist_rate if hist_rate > 0 else 1.0

    if ratio >= CONVERSION_DROP_WARN:
        return None   # within normal range

    if ratio < CONVERSION_DROP_CRIT:
        severity = AnomalySeverity.CRITICAL
        action   = (
            f"Conversion rate has dropped {round((1-ratio)*100, 1)} % vs 7-day avg. "
            "Check for pricing issues, staff shortages, or stock problems."
        )
    else:
        severity = AnomalySeverity.WARN
        action   = (
            f"Conversion rate is {round((1-ratio)*100, 1)} % below 7-day average. "
            "Review zone heatmap for early exit patterns."
        )

    return Anomaly(
        anomaly_type="CONVERSION_DROP",
        severity=severity,
        detail=(
            f"Today: {round(today_rate*100, 1)} %  "
            f"7-day avg: {round(hist_rate*100, 1)} %  "
            f"ratio: {round(ratio, 2)}"
        ),
        suggested_action=action,
        detected_at=_now_iso(),
    )


def _detect_dead_zones(store_id: str, since: str, until: str) -> List[Anomaly]:
    """
    Flag zones that were active in the past 2 hours but silent in the
    last 30 minutes.
    """
    now      = datetime.now(timezone.utc)
    cutoff   = now - timedelta(minutes=DEAD_ZONE_WINDOW_MIN)
    lookback = now - timedelta(hours=DEAD_ZONE_LOOKBACK_H)

    # Zones active in the 2-hour lookback window
    hist_rows = query_events(
        store_id=store_id,
        since=_iso(lookback),
        until=_iso(now),
        event_types=["ZONE_ENTER"],
        exclude_staff=True,
    )
    if not hist_rows:
        return []   # store has no traffic — don't fire dead zone alerts

    # Zones with traffic in the last 30 minutes
    recent_rows = query_events(
        store_id=store_id,
        since=_iso(cutoff),
        until=_iso(now),
        event_types=["ZONE_ENTER"],
        exclude_staff=True,
    )
    recently_active = {row["zone_id"] for row in recent_rows if row["zone_id"]}
    historically_active = {row["zone_id"] for row in hist_rows if row["zone_id"]}

    dead_zones = historically_active - recently_active
    anomalies  = []
    for zone in sorted(dead_zones):
        anomalies.append(Anomaly(
            anomaly_type="DEAD_ZONE",
            severity=AnomalySeverity.INFO,
            detail=(
                f"Zone '{zone}' had no visitor entries in the last "
                f"{DEAD_ZONE_WINDOW_MIN} minutes despite earlier traffic."
            ),
            suggested_action=(
                f"Check camera feed for zone '{zone}'. "
                "Consider moving promotional displays or staff to the area."
            ),
            detected_at=_now_iso(),
        ))
    return anomalies


# ─────────────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────────────

def detect_anomalies(
    store_id: str,
    since:    Optional[str] = None,
    until:    Optional[str] = None,
) -> AnomalyResponse:
    """
    Run all anomaly detectors for a store and return active anomalies.

    Parameters
    ----------
    store_id : Target store identifier
    since    : ISO-8601 window start (default: today)
    until    : ISO-8601 window end   (default: now)

    Returns
    -------
    AnomalyResponse with list of active anomalies
    """
    if not since or not until:
        _since, _until = _today_window()
        since = since or _since
        until = until or _until

    anomalies: List[Anomaly] = []

    # Queue spike
    spike = _detect_queue_spike(store_id, since, until)
    if spike:
        anomalies.append(spike)

    # Conversion drop (compares 7-day historical — may be None if insufficient data)
    try:
        drop = _detect_conversion_drop(store_id)
        if drop:
            anomalies.append(drop)
    except Exception as exc:
        logger.warning("Conversion drop check failed  store=%s  err=%s", store_id, exc)

    # Dead zones
    try:
        dead = _detect_dead_zones(store_id, since, until)
        anomalies.extend(dead)
    except Exception as exc:
        logger.warning("Dead zone check failed  store=%s  err=%s", store_id, exc)

    # Sort: CRITICAL → WARN → INFO
    _order = {AnomalySeverity.CRITICAL: 0, AnomalySeverity.WARN: 1, AnomalySeverity.INFO: 2}
    anomalies.sort(key=lambda a: _order.get(a.severity, 9))

    logger.info(
        "Anomaly detection complete  store=%s  count=%d",
        store_id, len(anomalies),
    )

    return AnomalyResponse(store_id=store_id, anomalies=anomalies)
