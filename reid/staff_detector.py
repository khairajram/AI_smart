"""
reid/staff_detector.py
──────────────────────
Staff member detection for retail CCTV analytics.

Strategy
--------
The system uses a THREE-TIER approach, applied in order of reliability:

TIER 1 — Spatial heuristic (always on, zero overhead)
    Staff members spend significant time in staff-only areas: behind counters,
    in stockrooms, or in fixed "stay zones" near checkout.
    
    Implementation: If a track's average vertical position (centroid Y)
    is in the TOP portion of the frame (configurable: default top 15 %),
    the person is in a "staff zone" and is flagged is_staff=True.
    
    Why this works in retail: Checkout counters place staff at a fixed
    depth/distance from the camera, meaning their head appears near the
    top of the frame in a wide-angle camera view.

TIER 2 — Dwell pattern (activates after N frames of tracking)
    Staff members exhibit stationary dwell behaviour: they stay in one
    place for long periods (e.g., standing at a counter).
    
    Implementation: Track the std-dev of centroid positions over a sliding
    window. Low std-dev (<STATIC_STD_THRESHOLD px) for >STAFF_DWELL_FRAMES
    frames = static person = likely staff.
    
    A customer who stops to browse a shelf will also be static, but only
    briefly. Staff remain static for minutes, not seconds.

TIER 3 — Colour / uniform detection (optional, requires configuration)
    Staff in many retail chains wear uniforms of a specific colour.
    A dominant HSV hue analysis on the torso region of the crop can
    classify uniforms if a target colour range is provided in the env.
    
    This tier is disabled by default (requires calibration per store).

Limitations
-----------
See LIMITATIONS.md:
- The spatial heuristic is camera-position dependent. The default (top 15 %)
  works for cameras mounted high and angled down. Calibrate STAFF_ZONE_TOP_PCT
  per camera if your setup differs.
- Dwell detection cannot distinguish staff from a customer who stands still
  for >5 minutes (rare but possible). Use spatial heuristic jointly.
- Colour-based detection requires per-store colour calibration.

Usage
-----
    detector = StaffDetector()
    # Per track, call with bbox and centroid history:
    is_staff = detector.classify(
        centroid_x=320, centroid_y=50,
        centroid_history=[(318, 52), (321, 49), ...],
        frame_height=480,
        crop_bgr=crop_image,   # optional — enables Tier 3
    )
"""

from __future__ import annotations

import logging
import os
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
#  Configuration (all overridable via environment variables)
# ─────────────────────────────────────────────────────────────────────

# Tier 1 — Spatial threshold
# Fraction of frame height (from top) considered a "staff zone"
STAFF_ZONE_TOP_PCT: float = float(os.environ.get("STAFF_ZONE_TOP_PCT", "0.15"))

# Tier 2 — Dwell-based detection
# Centroid position std-dev (pixels) below which the person is considered static
STATIC_STD_THRESHOLD: float = float(os.environ.get("STAFF_STATIC_STD_PX", "8.0"))
# Number of consecutive frames with low movement required to flag as staff
STAFF_DWELL_FRAMES: int = int(os.environ.get("STAFF_DWELL_FRAMES", "150"))

# Tier 3 — Uniform colour detection
# Set STAFF_UNIFORM_HUE_MIN and STAFF_UNIFORM_HUE_MAX (0-179 in OpenCV HSV)
# to enable. Leave unset (empty) to disable.
UNIFORM_HUE_MIN: Optional[int] = (
    int(os.environ["STAFF_UNIFORM_HUE_MIN"])
    if "STAFF_UNIFORM_HUE_MIN" in os.environ else None
)
UNIFORM_HUE_MAX: Optional[int] = (
    int(os.environ["STAFF_UNIFORM_HUE_MAX"])
    if "STAFF_UNIFORM_HUE_MAX" in os.environ else None
)
# Fraction of torso pixels matching the hue range to classify as uniform
UNIFORM_COVERAGE_THRESHOLD: float = float(
    os.environ.get("STAFF_UNIFORM_COVERAGE_THRESHOLD", "0.40")
)


# ─────────────────────────────────────────────────────────────────────
#  Per-track state
# ─────────────────────────────────────────────────────────────────────

class TrackState:
    """Sliding window of centroid positions for one track."""

    def __init__(self, window_size: int = STAFF_DWELL_FRAMES) -> None:
        self.history: Deque[Tuple[float, float]] = deque(maxlen=window_size)
        self.is_staff_flag:  bool = False
        self.spatial_hits:   int  = 0   # frames classified as staff zone
        self.total_frames:   int  = 0

    def update(self, cx: float, cy: float) -> None:
        self.history.append((cx, cy))
        self.total_frames += 1

    def centroid_std(self) -> float:
        """XY standard deviation of centroid history (pixels)."""
        if len(self.history) < 10:
            return float("inf")
        pts = np.array(self.history, dtype=np.float32)
        return float(pts.std())


# ─────────────────────────────────────────────────────────────────────
#  Staff detector
# ─────────────────────────────────────────────────────────────────────

class StaffDetector:
    """
    Stateful staff detector — maintains per-track history.

    One instance should be created per CameraPipeline.  Track state is
    created lazily and cleaned up when tracks disappear.

    Call `classify()` once per track per frame.
    Call `remove_track()` when a track is lost.
    """

    def __init__(self) -> None:
        self._tracks: Dict[int, TrackState] = {}

    # ── Public API ────────────────────────────────────────────────────

    def classify(
        self,
        track_id:        int,
        centroid_x:      float,
        centroid_y:      float,
        frame_height:    int,
        crop_bgr:        Optional[np.ndarray] = None,
    ) -> bool:
        """
        Classify whether a tracked person is a staff member.

        Parameters
        ----------
        track_id     : ByteTrack track ID for this camera
        centroid_x   : Bounding-box centroid X (pixels)
        centroid_y   : Bounding-box centroid Y (pixels)
        frame_height : Frame height in pixels (for Tier 1 threshold)
        crop_bgr     : Optional person crop image (enables Tier 3)

        Returns
        -------
        True if the person is classified as staff.
        """
        state = self._get_or_create(track_id)
        state.update(centroid_x, centroid_y)

        # ── Tier 1: Spatial zone heuristic ───────────────────────────
        staff_zone_boundary = frame_height * STAFF_ZONE_TOP_PCT
        in_staff_zone = centroid_y < staff_zone_boundary

        if in_staff_zone:
            state.spatial_hits += 1

        # Require that the person has been in the staff zone for at least
        # 30 % of their observed frames (avoids flagging customers who
        # briefly walk through the back area).
        zone_fraction = state.spatial_hits / max(1, state.total_frames)
        tier1_positive = zone_fraction >= 0.30 and state.total_frames >= 30

        if tier1_positive:
            logger.debug(
                "Staff  Tier1 spatial  track=%d  cy=%.1f  zone_boundary=%.1f  frac=%.2f",
                track_id, centroid_y, staff_zone_boundary, zone_fraction,
            )
            state.is_staff_flag = True
            return True

        # ── Tier 2: Static dwell pattern ─────────────────────────────
        if len(state.history) >= STAFF_DWELL_FRAMES:
            std = state.centroid_std()
            if std < STATIC_STD_THRESHOLD:
                logger.debug(
                    "Staff  Tier2 static dwell  track=%d  centroid_std=%.2fpx",
                    track_id, std,
                )
                state.is_staff_flag = True
                return True

        # ── Tier 3: Uniform colour detection (if configured) ─────────
        if crop_bgr is not None and UNIFORM_HUE_MIN is not None and UNIFORM_HUE_MAX is not None:
            if self._has_uniform_colour(crop_bgr):
                logger.debug(
                    "Staff  Tier3 uniform colour  track=%d", track_id
                )
                state.is_staff_flag = True
                return True

        # Once flagged as staff, keep the flag (sticky — avoids flip-flopping)
        return state.is_staff_flag

    def remove_track(self, track_id: int) -> None:
        """Remove per-track state when a track is lost."""
        self._tracks.pop(track_id, None)

    def reset(self) -> None:
        """Clear all track state (call when pipeline restarts)."""
        self._tracks.clear()

    # ── Private helpers ───────────────────────────────────────────────

    def _get_or_create(self, track_id: int) -> TrackState:
        if track_id not in self._tracks:
            self._tracks[track_id] = TrackState()
        return self._tracks[track_id]

    @staticmethod
    def _has_uniform_colour(crop_bgr: np.ndarray) -> bool:
        """
        Tier 3: Check if the torso region of the crop matches the configured
        staff uniform colour (HSV hue range).

        Analyses only the middle 40 % of the crop height (torso area).
        """
        try:
            import cv2
            h, w = crop_bgr.shape[:2]
            # Torso = middle 40 % of height
            torso_top    = int(h * 0.30)
            torso_bottom = int(h * 0.70)
            torso = crop_bgr[torso_top:torso_bottom, :]

            hsv   = cv2.cvtColor(torso, cv2.COLOR_BGR2HSV)
            mask  = cv2.inRange(
                hsv,
                (UNIFORM_HUE_MIN, 50, 50),    # type: ignore[arg-type]
                (UNIFORM_HUE_MAX, 255, 255),   # type: ignore[arg-type]
            )
            coverage = mask.sum() / (255.0 * mask.size)
            return coverage >= UNIFORM_COVERAGE_THRESHOLD
        except Exception as exc:
            logger.debug("Tier3 colour check failed: %s", exc)
            return False
