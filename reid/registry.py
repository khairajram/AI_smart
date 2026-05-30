"""
reid/registry.py
────────────────
Global Visitor Registry — the authoritative store of all known visitors.

Responsibilities
----------------
* Maintain ACTIVE visitors (currently tracked in at least one camera)
* Maintain recently EXITED visitors (within the re-entry window)
* Resolve incoming embeddings to existing or new visitor identities
* Detect REENTRY events (same person returns within the time window)
* Detect CROSS-CAMERA matches (same person seen in a different camera)
* Perform periodic garbage collection of expired exited records
* Expose Prometheus-compatible counters for observability

Thread Safety
-------------
All public methods acquire a reentrant lock (threading.RLock) so the
registry can safely be shared across multiple camera pipeline threads.

Visitor Record Schema
---------------------
{
    "visitor_id":      str,          # UUID, stable across cameras & re-entries
    "embedding":       np.ndarray,   # 512-d L2-normalised float32
    "first_seen":      datetime,     # UTC, when this visitor was first detected
    "last_seen":       datetime,     # UTC, most recent detection
    "camera_id":       str,          # Most recent camera
    "status":          str,          # "active" | "exited"
    "exit_time":       datetime|None,# Set when status → "exited"
    "reid_confidence": float,        # Confidence of the last ReID match
    "reentry_count":   int,          # How many times this person re-entered
    "track_history":   list[dict],   # Lightweight track event log
}
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional, Tuple
from uuid import uuid4

import numpy as np

from config.settings import settings
from reid.events import EventPublisher, EventType, build_event
from reid.similarity import MatchResult, batch_match

logger = logging.getLogger(__name__)

VisitorStatus = Literal["active", "exited"]


# ─────────────────────────────────────────────────────────────────────
#  Visitor record
# ─────────────────────────────────────────────────────────────────────

@dataclass
class VisitorRecord:
    visitor_id: str
    embedding: np.ndarray
    first_seen: datetime
    last_seen: datetime
    camera_id: str
    status: VisitorStatus = "active"
    exit_time: Optional[datetime] = None
    reid_confidence: float = 1.0
    reentry_count: int = 0
    is_staff: bool = False
    track_history: List[Dict] = field(default_factory=list)

    def update_embedding(self, new_embedding: np.ndarray, alpha: float = 0.3) -> None:
        """
        Exponential moving average update to smooth embedding drift.
        alpha controls how quickly the stored embedding adapts to the
        new observation (lower = more stable, higher = more adaptive).
        """
        self.embedding = (1.0 - alpha) * self.embedding + alpha * new_embedding
        # Re-normalise after blending
        norm = np.linalg.norm(self.embedding)
        if norm > 1e-8:
            self.embedding /= norm

    def to_dict(self) -> Dict:
        """Serialise to a JSON-safe dictionary (omits the raw embedding array)."""
        return {
            "visitor_id":      self.visitor_id,
            "first_seen":      self.first_seen.isoformat(),
            "last_seen":       self.last_seen.isoformat(),
            "camera_id":       self.camera_id,
            "status":          self.status,
            "is_staff":        self.is_staff,
            "exit_time":       self.exit_time.isoformat() if self.exit_time else None,
            "reid_confidence": round(self.reid_confidence, 4),
            "reentry_count":   self.reentry_count,
            "track_history":   self.track_history[-10:],  # last 10 events only
        }


# ─────────────────────────────────────────────────────────────────────
#  Resolution outcome
# ─────────────────────────────────────────────────────────────────────

@dataclass
class ResolveResult:
    visitor_id: str
    event_type: EventType
    reid_confidence: float
    is_new: bool
    is_staff: bool = False


# ─────────────────────────────────────────────────────────────────────
#  Global Visitor Registry
# ─────────────────────────────────────────────────────────────────────

class VisitorRegistry:
    """
    Thread-safe global visitor registry.

    All identity resolution decisions flow through this single object.
    One instance should be created per deployment and shared across all
    camera pipeline threads.
    """

    def __init__(self, publisher: Optional[EventPublisher] = None, store_id: str = "UNKNOWN_STORE") -> None:
        self._lock = threading.RLock()
        self._store_id = store_id          # passed to all published events
        self._active:  Dict[str, VisitorRecord] = {}   # visitor_id → record
        self._exited:  Dict[str, VisitorRecord] = {}   # visitor_id → record
        self._publisher = publisher

        # Prometheus-style counters (simple Python ints, thread-safe under lock)
        self._counter_new_visitors    = 0
        self._counter_cross_camera    = 0
        self._counter_reentries       = 0
        self._counter_exited          = 0
        self._counter_staff_detected  = 0   # distinct staff visitor_ids seen

        # GC scheduling
        self._last_gc_time = time.monotonic()

    # ── Public API ───────────────────────────────────────────────────

    def resolve(
        self,
        embedding: np.ndarray,
        camera_id: str,
        track_id: int,
        timestamp: float,
        bbox: Optional[Tuple] = None,
        is_staff: bool = False,
    ) -> ResolveResult:
        """
        Core identity resolution method.

        Given a fresh embedding from the current frame, determine whether
        this person is:
        1. An ACTIVE visitor (same or different camera) → CROSS_CAMERA_MATCH
        2. A recently EXITED visitor returning → REENTRY
        3. A brand-new visitor → NEW_VISITOR

        Parameters
        ----------
        embedding  : 512-d L2-normalised float32 array
        camera_id  : Source camera identifier
        track_id   : ByteTrack track ID for this camera
        timestamp  : Frame Unix timestamp
        bbox       : Optional (x1,y1,x2,y2) for event payload
        is_staff   : True if StaffDetector has classified this track as staff

        Returns
        -------
        ResolveResult with visitor_id, event_type, reid_confidence, is_staff
        """
        with self._lock:
            self._maybe_gc()

            # ── 1. Check active visitors ─────────────────────────────
            active_embeddings = {
                vid: rec.embedding for vid, rec in self._active.items()
            }
            active_match = batch_match(
                query_embedding=embedding,
                registry=active_embeddings,
                threshold=settings.REID_SIMILARITY_THRESHOLD,
                low_confidence_threshold=settings.LOW_CONFIDENCE_THRESHOLD,
            )

            if active_match.matched:
                vid = active_match.visitor_id
                rec = self._active[vid]

                # Update staff flag (once staff, always staff — sticky classification)
                if is_staff and not rec.is_staff:
                    rec.is_staff = True
                    logger.debug("Track %d promoted to staff  visitor=%s", track_id, vid)

                # Determine event type: same camera = normal update, diff camera = cross-camera
                if rec.camera_id != camera_id:
                    event_type = EventType.CROSS_CAMERA_MATCH
                    self._counter_cross_camera += 1
                    logger.info(
                        "CROSS_CAMERA  visitor=%s  %s→%s  confidence=%.3f",
                        vid, rec.camera_id, camera_id, active_match.similarity,
                    )
                else:
                    event_type = EventType.NEW_VISITOR   # re-use label for same-cam update
                    # (Not published as a new event — just an update)

                self._update_active(rec, embedding, camera_id, track_id, timestamp, bbox)

                result = ResolveResult(
                    visitor_id=vid,
                    event_type=event_type,
                    reid_confidence=active_match.similarity,
                    is_new=False,
                    is_staff=rec.is_staff,
                )
                if event_type == EventType.CROSS_CAMERA_MATCH:
                    self._publish_event(result, track_id, bbox)
                return result

            # ── 2. Check exited visitors (re-entry detection) ────────
            now = time.time()
            window = settings.REENTRY_WINDOW_SECONDS
            eligible_exited = {
                vid: rec.embedding
                for vid, rec in self._exited.items()
                if rec.exit_time is not None
                and (now - rec.exit_time.timestamp()) <= window
            }

            exited_match = batch_match(
                query_embedding=embedding,
                registry=eligible_exited,
                threshold=settings.REENTRY_SIMILARITY_THRESHOLD,
                low_confidence_threshold=settings.LOW_CONFIDENCE_THRESHOLD,
            )

            if exited_match.matched:
                vid = exited_match.visitor_id
                rec = self._exited.pop(vid)

                # Promote back to active, preserve staff flag
                rec.status = "active"
                rec.exit_time = None
                rec.reentry_count += 1
                rec.reid_confidence = exited_match.similarity
                if is_staff:
                    rec.is_staff = True
                self._update_active(rec, embedding, camera_id, track_id, timestamp, bbox)
                self._active[vid] = rec
                self._counter_reentries += 1

                logger.info(
                    "REENTRY  visitor=%s  camera=%s  confidence=%.3f  reentry_count=%d",
                    vid, camera_id, exited_match.similarity, rec.reentry_count,
                )

                result = ResolveResult(
                    visitor_id=vid,
                    event_type=EventType.REENTRY,
                    reid_confidence=exited_match.similarity,
                    is_new=False,
                    is_staff=rec.is_staff,
                )
                self._publish_event(result, track_id, bbox)
                return result

            # ── 3. New visitor ───────────────────────────────────────
            vid = str(uuid4())
            now_dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            rec = VisitorRecord(
                visitor_id=vid,
                embedding=embedding.copy(),
                first_seen=now_dt,
                last_seen=now_dt,
                camera_id=camera_id,
                status="active",
                reid_confidence=1.0,
                is_staff=is_staff,
                track_history=[self._make_track_event(track_id, camera_id, timestamp, bbox)],
            )
            self._active[vid] = rec
            self._counter_new_visitors += 1

            staff_label = " [STAFF]" if is_staff else ""
            logger.info("NEW_VISITOR%s  visitor=%s  camera=%s", staff_label, vid, camera_id)

            result = ResolveResult(
                visitor_id=vid,
                event_type=EventType.NEW_VISITOR,
                reid_confidence=1.0,
                is_new=True,
                is_staff=is_staff,
            )
            self._publish_event(result, track_id, bbox)
            return result

    def mark_exited(self, visitor_id: str, timestamp: Optional[float] = None) -> bool:
        """
        Transition an active visitor to 'exited' status.

        Called when the tracker reports that a track has been lost for
        ACTIVE_VISITOR_TIMEOUT_SECONDS without reappearing.

        Returns True if the visitor was found and transitioned.
        """
        with self._lock:
            if visitor_id not in self._active:
                return False

            rec = self._active.pop(visitor_id)
            exit_ts = timestamp or time.time()
            rec.status = "active"   # keep status for GC logic clarity
            rec.exit_time = datetime.fromtimestamp(exit_ts, tz=timezone.utc)
            rec.status = "exited"
            self._exited[visitor_id] = rec
            self._counter_exited += 1
            self._enforce_exited_limit()

            logger.debug("EXITED  visitor=%s  at=%s", visitor_id, rec.exit_time)

            if self._publisher:
                is_staff_flag = rec.is_staff if rec else False
                event = build_event(
                    event_type=EventType.VISITOR_EXITED,
                    visitor_id=visitor_id,
                    camera_id=rec.camera_id,
                    track_id=-1,
                    reid_confidence=rec.reid_confidence,
                    store_id=self._store_id,
                    is_staff=is_staff_flag,
                    timestamp=exit_ts,
                )
                self._publisher.publish(event)
            return True

    def auto_expire_stale_active(self) -> List[str]:
        """
        Move active visitors to exited if they haven't been seen recently.

        Call this periodically from the pipeline GC routine.
        Returns list of expired visitor_ids.
        """
        with self._lock:
            now = time.time()
            timeout = settings.ACTIVE_VISITOR_TIMEOUT_SECONDS
            to_expire = [
                vid for vid, rec in self._active.items()
                if (now - rec.last_seen.timestamp()) > timeout
            ]
            for vid in to_expire:
                self.mark_exited(vid, timestamp=now)
            if to_expire:
                logger.debug("Auto-expired %d stale active visitors", len(to_expire))
            return to_expire

    # ── Snapshot / reporting ─────────────────────────────────────────

    def snapshot_active(self) -> List[Dict]:
        with self._lock:
            return [rec.to_dict() for rec in self._active.values()]

    def snapshot_exited(self) -> List[Dict]:
        with self._lock:
            return [rec.to_dict() for rec in self._exited.values()]

    def get_metrics(self) -> Dict:
        with self._lock:
            staff_active = sum(1 for r in self._active.values() if r.is_staff)
            return {
                "active_visitors":         len(self._active),
                "active_staff":            staff_active,
                "active_customers":        len(self._active) - staff_active,
                "exited_visitors_cached":  len(self._exited),
                "total_unique_visitors":   self._counter_new_visitors,
                "total_reentries":         self._counter_reentries,
                "total_cross_camera":      self._counter_cross_camera,
                "total_exits_recorded":    self._counter_exited,
            }

    def reset(self) -> None:
        """Clear all state. Use with caution (intended for testing/admin)."""
        with self._lock:
            self._active.clear()
            self._exited.clear()
            self._counter_new_visitors = 0
            self._counter_cross_camera = 0
            self._counter_reentries    = 0
            self._counter_exited       = 0
        logger.warning("VisitorRegistry RESET — all visitor state cleared")

    # ── Private helpers ──────────────────────────────────────────────

    def _update_active(
        self,
        rec: VisitorRecord,
        embedding: np.ndarray,
        camera_id: str,
        track_id: int,
        timestamp: float,
        bbox: Optional[Tuple],
    ) -> None:
        """Update mutable fields on an existing active record."""
        rec.update_embedding(embedding)
        rec.camera_id = camera_id
        rec.last_seen = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        rec.track_history.append(
            self._make_track_event(track_id, camera_id, timestamp, bbox)
        )
        # Keep track history bounded
        if len(rec.track_history) > 100:
            rec.track_history = rec.track_history[-100:]
        self._active[rec.visitor_id] = rec

    @staticmethod
    def _make_track_event(
        track_id: int,
        camera_id: str,
        timestamp: float,
        bbox: Optional[Tuple],
    ) -> Dict:
        entry: Dict = {
            "track_id":  track_id,
            "camera_id": camera_id,
            "ts":        round(timestamp, 3),
        }
        if bbox is not None:
            entry["bbox"] = list(bbox)
        return entry

    def _publish_event(
        self,
        result: ResolveResult,
        track_id: int,
        bbox: Optional[Tuple],
    ) -> None:
        if self._publisher is None:
            return
        rec = self._active.get(result.visitor_id) or self._exited.get(result.visitor_id)
        camera_id = rec.camera_id if rec else "unknown"
        seq = len(rec.track_history) if rec else None
        event = build_event(
            event_type=result.event_type,
            visitor_id=result.visitor_id,
            camera_id=camera_id,
            track_id=track_id,
            reid_confidence=result.reid_confidence,
            store_id=self._store_id,
            is_staff=result.is_staff,
            session_seq=seq,
            extra={"bbox": list(bbox)} if bbox else None,
        )
        self._publisher.publish(event)

    def _enforce_exited_limit(self) -> None:
        """Remove oldest exited records when the limit is exceeded (LRU eviction)."""
        max_size = settings.MAX_EXITED_REGISTRY_SIZE
        if len(self._exited) > max_size:
            # Sort by exit_time, remove oldest
            sorted_ids = sorted(
                self._exited.keys(),
                key=lambda vid: self._exited[vid].exit_time or datetime.min.replace(tzinfo=timezone.utc),
            )
            to_remove = len(self._exited) - max_size
            for vid in sorted_ids[:to_remove]:
                del self._exited[vid]
            logger.debug("Evicted %d old exited records (limit=%d)", to_remove, max_size)

    def _maybe_gc(self) -> None:
        """Run GC if the interval has elapsed (called inside lock)."""
        now = time.monotonic()
        if (now - self._last_gc_time) >= settings.REGISTRY_GC_INTERVAL_SECONDS:
            self._gc_expired_exits()
            self._last_gc_time = now

    def _gc_expired_exits(self) -> None:
        """Remove exited records whose re-entry window has expired."""
        now = time.time()
        window = settings.REENTRY_WINDOW_SECONDS
        expired = [
            vid for vid, rec in self._exited.items()
            if rec.exit_time is not None
            and (now - rec.exit_time.timestamp()) > window
        ]
        for vid in expired:
            del self._exited[vid]
        if expired:
            logger.debug("GC removed %d expired exited records", len(expired))
