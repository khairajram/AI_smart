"""
tracking/detector.py
────────────────────
YOLOv8 person detector wrapper.

Responsible for:
* Loading the YOLOv8 model (nano by default — fastest inference)
* Running inference on a single BGR frame
* Filtering detections to person class only
* Returning a list of Detection objects (bbox, confidence, class_id)

Model Selection Note
--------------------
yolov8n.pt  — 3.2 M params,  ~80 FPS on CPU, ~500 FPS on A100
yolov8s.pt  — 11 M params,   ~50 FPS on CPU, ~350 FPS on A100
yolov8m.pt  — 25 M params,   ~30 FPS on CPU, ~200 FPS on A100

For retail ReID, yolov8n is recommended as the tracking + ReID steps
are the bottleneck, not detection.  Switch to yolov8s for higher
accuracy when camera resolution > 1080p.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class Detection:
    """Single person detection result."""
    bbox_xyxy: Tuple[float, float, float, float]   # (x1, y1, x2, y2)
    confidence: float
    class_id: int = 0   # 0 = person in COCO


class PersonDetector:
    """
    YOLOv8-based person detector.

    Usage
    -----
        detector = PersonDetector()
        detector.load()
        detections = detector.detect(frame)
    """

    def __init__(self) -> None:
        self._model = None
        self._loaded = False

    def load(self) -> "PersonDetector":
        """Load YOLOv8 model weights (downloaded automatically on first run)."""
        if self._loaded:
            return self
        try:
            from ultralytics import YOLO
            logger.info("Loading YOLO model: %s", settings.YOLO_MODEL)
            self._model = YOLO(settings.YOLO_MODEL)
            self._loaded = True
            logger.info("YOLO model loaded")
        except ImportError as exc:
            raise RuntimeError(
                "ultralytics is not installed. Run: pip install ultralytics"
            ) from exc
        return self

    def detect(self, frame: np.ndarray) -> List[Detection]:
        """
        Run person detection on a single BGR frame.

        Parameters
        ----------
        frame : np.ndarray  — BGR image (H × W × 3)

        Returns
        -------
        List[Detection] — filtered to person class, above confidence threshold
        """
        if not self._loaded:
            raise RuntimeError("Call PersonDetector.load() first")

        results = self._model.predict(
            source=frame,
            conf=settings.YOLO_CONFIDENCE,
            iou=settings.YOLO_IOU_THRESHOLD,
            classes=[settings.YOLO_PERSON_CLASS_ID],
            verbose=False,
            stream=False,
        )

        detections: List[Detection] = []
        for result in results:
            if result.boxes is None:
                continue
            boxes = result.boxes
            for i in range(len(boxes)):
                xyxy = boxes.xyxy[i].cpu().numpy().tolist()
                conf = float(boxes.conf[i].cpu().numpy())
                cls  = int(boxes.cls[i].cpu().numpy())
                detections.append(Detection(
                    bbox_xyxy=(xyxy[0], xyxy[1], xyxy[2], xyxy[3]),
                    confidence=conf,
                    class_id=cls,
                ))

        logger.debug("Detected %d persons in frame", len(detections))
        return detections

    @property
    def is_loaded(self) -> bool:
        return self._loaded
