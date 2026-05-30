"""
tracking/tracker.py
───────────────────
ByteTrack integration via the supervision library.

Why ByteTrack?
--------------
ByteTrack uses BOTH high-confidence AND low-confidence detections in
its association step.  This dramatically reduces ID switches when a
person is temporarily occluded or partially obscured (common in retail).
This means the ByteTrack track_id is more stable than SORT or DeepSORT
within a single camera, reducing the frequency with which the ReID
system needs to re-resolve the same person.

Track Lifecycle
---------------
  TENTATIVE → CONFIRMED → LOST → (re-matched or deleted)

A track enters CONFIRMED after being matched in consecutive frames.
A LOST track is held for BYTETRACK_TRACK_BUFFER frames before deletion.
This buffer is exploited by the per-camera track lifecycle manager in
the orchestrator to avoid premature mark_exited() calls.

Output
------
Each frame produces a list of TrackedPerson objects, one per active track.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from config.settings import settings
from tracking.detector import Detection

logger = logging.getLogger(__name__)


@dataclass
class TrackedPerson:
    """Single active ByteTrack track for the current frame."""
    track_id: int
    bbox_xyxy: Tuple[float, float, float, float]
    confidence: float
    is_confirmed: bool   # True once the track has been matched in N consecutive frames


class ByteTracker:
    """
    Thin wrapper around supervision's ByteTrack implementation.

    Usage
    -----
        tracker = ByteTracker()
        tracker.init()

        # Per frame:
        tracked = tracker.update(detections, frame_index)
    """

    def __init__(self) -> None:
        self._tracker = None
        self._initialised = False

    def init(self) -> "ByteTracker":
        """Initialise ByteTrack with configured parameters."""
        if self._initialised:
            return self
        try:
            import supervision as sv

            self._tracker = sv.ByteTrack(
                track_activation_threshold=settings.BYTETRACK_TRACK_THRESH,
                lost_track_buffer=settings.BYTETRACK_TRACK_BUFFER,
                minimum_matching_threshold=settings.BYTETRACK_MATCH_THRESH,
                frame_rate=settings.BYTETRACK_FRAME_RATE,
            )
            self._initialised = True
            logger.info(
                "ByteTrack initialised — thresh=%.2f  buffer=%d  match=%.2f  fps=%d",
                settings.BYTETRACK_TRACK_THRESH,
                settings.BYTETRACK_TRACK_BUFFER,
                settings.BYTETRACK_MATCH_THRESH,
                settings.BYTETRACK_FRAME_RATE,
            )
        except ImportError as exc:
            raise RuntimeError(
                "supervision is not installed. Run: pip install supervision"
            ) from exc
        return self

    def update(
        self,
        detections: List[Detection],
        frame: Optional[np.ndarray] = None,
    ) -> List[TrackedPerson]:
        """
        Update tracker with current frame detections.

        Parameters
        ----------
        detections : List[Detection]  — output of PersonDetector.detect()
        frame      : Optional frame array (used by supervision for Detections obj)

        Returns
        -------
        List[TrackedPerson] — one entry per active confirmed/tentative track
        """
        if not self._initialised:
            raise RuntimeError("Call ByteTracker.init() first")

        import supervision as sv

        if not detections:
            # Update tracker with empty detections to advance internal state
            empty = sv.Detections.empty()
            tracked = self._tracker.update_with_detections(empty)
            return []

        # Convert Detection list → supervision Detections
        xyxy  = np.array([list(d.bbox_xyxy) for d in detections], dtype=np.float32)
        conf  = np.array([d.confidence for d in detections], dtype=np.float32)
        cls   = np.array([d.class_id for d in detections], dtype=int)

        sv_detections = sv.Detections(xyxy=xyxy, confidence=conf, class_id=cls)

        # ByteTrack update
        tracked_sv = self._tracker.update_with_detections(sv_detections)

        results: List[TrackedPerson] = []
        for i in range(len(tracked_sv)):
            bbox  = tuple(tracked_sv.xyxy[i].tolist())
            tid   = int(tracked_sv.tracker_id[i])
            conf_ = float(tracked_sv.confidence[i]) if tracked_sv.confidence is not None else 0.5
            results.append(TrackedPerson(
                track_id=tid,
                bbox_xyxy=bbox,
                confidence=conf_,
                is_confirmed=True,   # supervision ByteTrack only returns confirmed
            ))

        logger.debug("ByteTrack: %d active tracks this frame", len(results))
        return results

    def reset(self) -> None:
        """Reset tracker state (e.g., on video cut or camera restart)."""
        if self._tracker is not None:
            self._tracker.reset()
        logger.debug("ByteTracker state reset")

    @property
    def is_initialised(self) -> bool:
        return self._initialised
