"""
api/models.py
─────────────
Pydantic v2 models for the Store Intelligence API.

All models conform exactly to the challenge event schema so that
POST /events/ingest validation and /stores/{id}/* response shapes
are correct and testable by the automated scoring harness.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────────────────────────────────────────────
#  Event type catalogue (challenge-defined)
# ─────────────────────────────────────────────────────────────────────

class EventType(str, Enum):
    ENTRY                 = "ENTRY"
    EXIT                  = "EXIT"
    ZONE_ENTER            = "ZONE_ENTER"
    ZONE_EXIT             = "ZONE_EXIT"
    ZONE_DWELL            = "ZONE_DWELL"
    BILLING_QUEUE_JOIN    = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY               = "REENTRY"
    # Internal ReID events (emitted by pipeline, mapped on ingest)
    NEW_VISITOR           = "NEW_VISITOR"
    CROSS_CAMERA_MATCH    = "CROSS_CAMERA_MATCH"
    VISITOR_EXITED        = "VISITOR_EXITED"


# ─────────────────────────────────────────────────────────────────────
#  Event schema
# ─────────────────────────────────────────────────────────────────────

class EventMetadata(BaseModel):
    """Optional metadata block inside each event."""
    queue_depth: Optional[int] = Field(
        default=None, ge=0,
        description="Queue depth at billing zone (BILLING_QUEUE_JOIN only)"
    )
    sku_zone: Optional[str] = Field(
        default=None,
        description="Zone label from store_layout.json"
    )
    session_seq: Optional[int] = Field(
        default=None, ge=1,
        description="Ordinal position of this event in the visitor session"
    )

    model_config = {"extra": "allow"}   # forward-compatible


class StoreEvent(BaseModel):
    """
    A single behavioural event emitted by the detection pipeline.

    Matches the challenge schema exactly — all fields are required unless
    marked Optional.  The `event_id` must be a globally unique UUID v4.
    """
    event_id:   str       = Field(..., description="UUID v4 — globally unique")
    store_id:   str       = Field(..., description="Store identifier e.g. STORE_BLR_002")
    camera_id:  str       = Field(..., description="Camera identifier e.g. CAM_ENTRY_01")
    visitor_id: str       = Field(..., description="ReID token — unique per visit session")
    event_type: EventType
    timestamp:  str       = Field(..., description="ISO-8601 UTC timestamp")
    zone_id:    Optional[str] = Field(
        default=None,
        description="Zone name from store_layout.json; null for ENTRY/EXIT events"
    )
    dwell_ms:   int       = Field(default=0, ge=0, description="Duration in milliseconds")
    is_staff:   bool      = Field(default=False, description="True if detected as store staff")
    confidence: float     = Field(default=1.0, ge=0.0, le=1.0,
                                  description="Detection confidence — never suppressed")
    metadata:   EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v: str) -> str:
        """Ensure timestamp is parseable ISO-8601."""
        try:
            # Accept both Z and +00:00 suffixes
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"timestamp must be ISO-8601 UTC: {v!r}") from exc
        return v

    @model_validator(mode="after")
    def zone_required_for_zone_events(self) -> "StoreEvent":
        zone_events = {
            EventType.ZONE_ENTER, EventType.ZONE_EXIT,
            EventType.ZONE_DWELL, EventType.BILLING_QUEUE_JOIN,
            EventType.BILLING_QUEUE_ABANDON,
        }
        if self.event_type in zone_events and not self.zone_id:
            raise ValueError(f"zone_id is required for event_type={self.event_type}")
        return self


# ─────────────────────────────────────────────────────────────────────
#  Ingest request / response
# ─────────────────────────────────────────────────────────────────────

class IngestRequest(BaseModel):
    events: List[StoreEvent] = Field(
        ...,
        max_length=500,
        description="Up to 500 events per request"
    )


class IngestErrorDetail(BaseModel):
    event_id: Optional[str]
    error: str


class IngestResponse(BaseModel):
    accepted:  int
    rejected:  int
    duplicate: int
    errors:    List[IngestErrorDetail] = []


# ─────────────────────────────────────────────────────────────────────
#  Store metrics response
# ─────────────────────────────────────────────────────────────────────

class StoreMetrics(BaseModel):
    store_id:          str
    window:            str     = "today"
    unique_visitors:   int
    conversion_rate:   float   = Field(..., ge=0.0, le=1.0)
    avg_dwell_per_zone: Dict[str, float]   # zone_id → avg dwell ms
    queue_depth:       Optional[int]       # current depth; null if no billing events
    abandonment_rate:  float   = Field(..., ge=0.0, le=1.0)
    total_entries:     int
    total_exits:       int
    total_reentries:   int


# ─────────────────────────────────────────────────────────────────────
#  Funnel response
# ─────────────────────────────────────────────────────────────────────

class FunnelStage(BaseModel):
    stage:       str
    visitors:    int
    drop_off_pct: float   = Field(..., ge=0.0, le=100.0)


class FunnelResponse(BaseModel):
    store_id: str
    window:   str = "today"
    stages:   List[FunnelStage]


# ─────────────────────────────────────────────────────────────────────
#  Heatmap response
# ─────────────────────────────────────────────────────────────────────

class ZoneHeatmap(BaseModel):
    zone_id:      str
    visit_count:  int
    avg_dwell_ms: float
    score:        float   = Field(..., ge=0.0, le=100.0)   # normalised 0–100


class HeatmapResponse(BaseModel):
    store_id:         str
    window:           str  = "today"
    data_confidence:  bool  # False if < 20 sessions in window
    zones:            List[ZoneHeatmap]


# ─────────────────────────────────────────────────────────────────────
#  Anomaly response
# ─────────────────────────────────────────────────────────────────────

class AnomalySeverity(str, Enum):
    INFO     = "INFO"
    WARN     = "WARN"
    CRITICAL = "CRITICAL"


class Anomaly(BaseModel):
    anomaly_type:      str
    severity:          AnomalySeverity
    detail:            str
    suggested_action:  str
    detected_at:       str   # ISO-8601


class AnomalyResponse(BaseModel):
    store_id:  str
    anomalies: List[Anomaly]


# ─────────────────────────────────────────────────────────────────────
#  Health response
# ─────────────────────────────────────────────────────────────────────

class StoreHealth(BaseModel):
    last_event_timestamp: Optional[str]
    status:               str   # "ok" | "STALE_FEED" | "NO_DATA"


class HealthResponse(BaseModel):
    status:         str
    uptime_seconds: float
    version:        str = "2.0.0"
    stores:         Dict[str, StoreHealth] = {}
