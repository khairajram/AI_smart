"""
pipeline/orchestrator.py
─────────────────────────
Per-camera pipeline that runs the full ReID loop every frame.

Architecture per frame
----------------------
  1. Capture frame from video source
  2. Detect persons (YOLOv8)
  3. Update ByteTrack — get stable track IDs
  4. For each confirmed track:
       a. Extract + validate person crop
       b. Accumulate crops into a batch
  5. Batch-embed all crops via OSNet (single GPU forward pass)
  6. Resolve each embedding against the VisitorRegistry
  7. Mark tracks that have disappeared → auto-expire active visitors
  8. Repeat

Track Lifecycle Manager
-----------------------
The orchestrator maintains a per-camera dict mapping:
    track_id → last_seen_frame_index

If a track_id is absent from the tracker output for
ACTIVE_VISITOR_TIMEOUT_SECONDS worth of frames, the corresponding
visitor is marked as exited in the registry.

Multi-camera Usage
------------------
Create one CameraPipeline per camera.  All pipelines share a single
VisitorRegistry instance, which is thread-safe.

    registry  = VisitorRegistry(publisher=create_publisher())
    embedder  = OSNetEmbedder().load()          # one per GPU device
    pipelines = [
        CameraPipeline("CAM_01", source_01, registry, embedder),
        CameraPipeline("CAM_02", source_02, registry, embedder),
    ]
    for p in pipelines:
        p.start()          # launches a daemon thread

NOTE: When multiple cameras share one GPU, set REID_DEVICE=cuda and
      use a threading.Semaphore around the embedder call to prevent
      OOM.  The embedder wrapper handles this automatically when
      use_semaphore=True is passed.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional, Union

import cv2
import numpy as np

from config.settings import settings
from reid.crop_utils import extract_crop
from reid.embedder import OSNetEmbedder
from reid.registry import VisitorRegistry
from reid.staff_detector import StaffDetector
from tracking.detector import PersonDetector
from tracking.tracker import ByteTracker, TrackedPerson

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
#  Frame source abstraction
# ─────────────────────────────────────────────────────────────────────

class VideoSource:
    """
    Generic video source wrapper supporting:
    - Integer webcam index  (e.g. 0)
    - File paths            (e.g. "video.mp4")
    - RTSP streams          (e.g. "rtsp://user:pass@192.168.1.5/stream1")
    """

    def __init__(self, source: Union[int, str]) -> None:
        self.source = source
        self._cap: Optional[cv2.VideoCapture] = None

    def open(self) -> "VideoSource":
        self._cap = cv2.VideoCapture(self.source)
        if not self._cap.isOpened():
            raise RuntimeError(f"Cannot open video source: {self.source!r}")
        fps = self._cap.get(cv2.CAP_PROP_FPS) or 25
        logger.info(
            "Opened video source %r  fps=%.1f  resolution=%dx%d",
            self.source,
            fps,
            int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        )
        return self

    def read(self):
        """Returns (success, frame) like cv2.VideoCapture.read()"""
        if self._cap is None:
            raise RuntimeError("VideoSource not opened")
        return self._cap.read()

    def release(self) -> None:
        if self._cap:
            self._cap.release()
            self._cap = None

    @property
    def fps(self) -> float:
        if self._cap is None:
            return 25.0
        return self._cap.get(cv2.CAP_PROP_FPS) or 25.0

    def __enter__(self):
        return self.open()

    def __exit__(self, *_):
        self.release()


# ─────────────────────────────────────────────────────────────────────
#  Per-camera pipeline
# ─────────────────────────────────────────────────────────────────────

class CameraPipeline:
    """
    Manages the full detection → tracking → ReID loop for one camera.

    Parameters
    ----------
    camera_id  : Unique string identifier (e.g. "CAM_01")
    source     : Video source (int / path / RTSP URL)
    registry   : Shared VisitorRegistry instance (thread-safe)
    embedder   : Shared or dedicated OSNetEmbedder instance
    show       : Whether to display an annotated preview window
    max_frames : Optional limit for testing (None = run forever)
    """

    def __init__(
        self,
        camera_id: str,
        source: Union[int, str],
        registry: VisitorRegistry,
        embedder: OSNetEmbedder,
        show: bool = False,
        max_frames: Optional[int] = None,
    ) -> None:
        self.camera_id  = camera_id
        self.source     = source
        self.registry   = registry
        self.embedder   = embedder
        self.show       = show
        self.max_frames = max_frames

        self._detector     = PersonDetector()
        self._tracker      = ByteTracker()
        self._staff_det    = StaffDetector()   # one per camera
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Track lifecycle: track_id → last frame index seen
        self._track_last_seen: Dict[int, int] = {}
        # Track → visitor_id mapping (for efficient mark_exited lookup)
        self._track_to_visitor: Dict[int, str] = {}

        # Diagnostics
        self._frame_count   = 0
        self._start_time    = 0.0
        self._fps_actual    = 0.0

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> "CameraPipeline":
        """Launch the pipeline in a background daemon thread."""
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"pipeline-{self.camera_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info("Pipeline started for camera %s", self.camera_id)
        return self

    def stop(self) -> None:
        """Signal the pipeline thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("Pipeline stopped for camera %s", self.camera_id)

    def run_sync(self) -> None:
        """
        Run the pipeline loop synchronously in the calling thread.
        Useful for single-camera usage or testing.
        """
        self._run_loop()

    # ── Main loop ────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Inner pipeline loop — runs until stop() or source exhausted."""
        self._detector.load()
        self._tracker.init()
        self._start_time = time.perf_counter()

        video = VideoSource(self.source)
        try:
            video.open()
        except RuntimeError as exc:
            logger.error("Camera %s source error: %s", self.camera_id, exc)
            return

        timeout_frames = int(
            settings.ACTIVE_VISITOR_TIMEOUT_SECONDS * (video.fps or 25)
        )

        logger.info(
            "Camera %s pipeline loop started  source=%r  track_timeout=%d frames",
            self.camera_id, self.source, timeout_frames,
        )

        try:
            while not self._stop_event.is_set():
                ok, frame = video.read()
                if not ok:
                    logger.info("Camera %s source exhausted", self.camera_id)
                    break

                self._frame_count += 1
                if self.max_frames and self._frame_count > self.max_frames:
                    break

                timestamp = time.time()
                self._process_frame(frame, timestamp, timeout_frames)

                if self.show:
                    cv2.imshow(f"ReID — {self.camera_id}", frame)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break

        finally:
            video.release()
            if self.show:
                cv2.destroyWindow(f"ReID — {self.camera_id}")
            self._expire_all_active()
            elapsed = time.perf_counter() - self._start_time
            if elapsed > 0:
                self._fps_actual = self._frame_count / elapsed
            logger.info(
                "Camera %s finished  frames=%d  avg_fps=%.1f",
                self.camera_id, self._frame_count, self._fps_actual,
            )

    # ── Per-frame processing ─────────────────────────────────────────

    def _process_frame(
        self,
        frame: np.ndarray,
        timestamp: float,
        timeout_frames: int,
    ) -> None:
        fi = self._frame_count   # frame index

        # 1. Detect persons
        detections = self._detector.detect(frame)

        # 2. Update ByteTrack
        tracks: List[TrackedPerson] = self._tracker.update(detections, frame)

        # 3. Extract person crops
        crops = []
        valid_tracks = []
        frame_h = frame.shape[0]   # needed for staff zone threshold
        for track in tracks:
            crop = extract_crop(
                frame=frame,
                bbox_xyxy=track.bbox_xyxy,
                track_id=track.track_id,
                camera_id=self.camera_id,
                timestamp=timestamp,
            )
            if crop is not None:
                crops.append(crop)
                valid_tracks.append(track)
            self._track_last_seen[track.track_id] = fi

        # 4. Batch embed all crops
        if crops:
            embeddings = self.embedder.embed_crops(crops)

            # 5. Resolve each embedding in the registry
            for crop, embedding, track in zip(crops, embeddings, valid_tracks):
                # Compute bounding-box centroid for staff detection
                x1, y1, x2, y2 = crop.bbox_xyxy
                cx = (x1 + x2) / 2
                cy = (y1 + y2) / 2

                # Classify staff (three-tier: spatial / dwell / colour)
                is_staff = self._staff_det.classify(
                    track_id=track.track_id,
                    centroid_x=cx,
                    centroid_y=cy,
                    frame_height=frame_h,
                    crop_bgr=crop.image_bgr,   # enables Tier 3 if configured
                )

                result = self.registry.resolve(
                    embedding=embedding,
                    camera_id=self.camera_id,
                    track_id=track.track_id,
                    timestamp=timestamp,
                    bbox=crop.bbox_xyxy,
                    is_staff=is_staff,
                )
                self._track_to_visitor[track.track_id] = result.visitor_id

        # 6. Expire tracks that have vanished
        current_track_ids = {t.track_id for t in tracks}
        disappeared = [
            tid for tid, last_fi in self._track_last_seen.items()
            if tid not in current_track_ids and (fi - last_fi) > timeout_frames
        ]
        for tid in disappeared:
            vid = self._track_to_visitor.pop(tid, None)
            if vid:
                self.registry.mark_exited(vid, timestamp=timestamp)
            self._staff_det.remove_track(tid)   # clean up staff detector state
            del self._track_last_seen[tid]

        # 7. Log FPS every 100 frames
        if fi % 100 == 0 and fi > 0:
            elapsed = time.perf_counter() - self._start_time
            fps = fi / elapsed if elapsed > 0 else 0
            logger.info(
                "Camera %s  frame=%d  fps=%.1f  active_tracks=%d",
                self.camera_id, fi, fps, len(tracks),
            )

    def _expire_all_active(self) -> None:
        """Mark all tracked visitors as exited when the pipeline stops."""
        for tid, vid in self._track_to_visitor.items():
            self.registry.mark_exited(vid)
        self._track_to_visitor.clear()
        self._track_last_seen.clear()
        self._staff_det.reset()   # clear staff detector state too

    # ── Diagnostics ──────────────────────────────────────────────────

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def fps_actual(self) -> float:
        if self._frame_count > 0 and self._start_time > 0:
            return self._frame_count / max(1e-9, time.perf_counter() - self._start_time)
        return 0.0
