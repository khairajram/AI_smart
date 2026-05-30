"""
reid/crop_utils.py
──────────────────
Bounding-box crop extraction and preprocessing pipeline for OSNet.

Responsibilities
----------------
* Safely clip bounding boxes to frame boundaries
* Reject crops that are too small to produce reliable embeddings
* Resize crops to the 256×128 (H×W) format expected by OSNet
* Apply ImageNet normalisation as a tensor

The preprocessing chain mirrors the torchvision transforms used
during OSNet training so inference statistics remain consistent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

import cv2
import numpy as np
import torch
import torchvision.transforms as T

from config.settings import settings

logger = logging.getLogger(__name__)

# ── ImageNet statistics used during OSNet training ──────────────────
_IMAGENET_MEAN = (0.485, 0.456, 0.406)
_IMAGENET_STD  = (0.229, 0.224, 0.225)

# ── Preprocessing transform (identical to torchreid eval transform) ──
REID_TRANSFORM = T.Compose([
    T.ToPILImage(),
    T.Resize((settings.CROP_HEIGHT, settings.CROP_WIDTH)),
    T.ToTensor(),
    T.Normalize(mean=_IMAGENET_MEAN, std=_IMAGENET_STD),
])


@dataclass
class PersonCrop:
    """Container returned for every successfully extracted person crop."""
    track_id: int
    camera_id: str
    timestamp: float           # Unix seconds
    bbox_xyxy: Tuple[int, int, int, int]   # (x1, y1, x2, y2) in frame coords
    bbox_wh: Tuple[int, int]               # (width, height)
    image_bgr: np.ndarray                  # Raw BGR crop (H×W×3)
    tensor: torch.Tensor                   # Preprocessed tensor (1×3×256×128)


def extract_crop(
    frame: np.ndarray,
    bbox_xyxy: Tuple[float, float, float, float],
    track_id: int,
    camera_id: str,
    timestamp: float,
) -> Optional[PersonCrop]:
    """
    Extract and preprocess a person crop from a video frame.

    Parameters
    ----------
    frame       : BGR numpy array (H × W × 3)
    bbox_xyxy   : Bounding box in (x1, y1, x2, y2) float format
    track_id    : ByteTrack track identifier
    camera_id   : Camera source identifier (e.g. "CAM_01")
    timestamp   : Frame capture time as Unix float

    Returns
    -------
    PersonCrop  : Fully populated crop container
    None        : If the crop is too small or otherwise invalid
    """
    frame_h, frame_w = frame.shape[:2]

    # ── Clip bbox to frame boundaries ─────────────────────────────────
    x1 = max(0, int(bbox_xyxy[0]))
    y1 = max(0, int(bbox_xyxy[1]))
    x2 = min(frame_w - 1, int(bbox_xyxy[2]))
    y2 = min(frame_h - 1, int(bbox_xyxy[3]))

    w = x2 - x1
    h = y2 - y1

    # ── Reject crops that are too small ───────────────────────────────
    if w <= 0 or h <= 0:
        logger.debug(
            "Rejected zero-size crop for track %s camera %s",
            track_id, camera_id,
        )
        return None

    area = w * h
    if area < settings.MIN_CROP_AREA:
        logger.debug(
            "Rejected undersized crop (area=%d < %d) for track %s camera %s",
            area, settings.MIN_CROP_AREA, track_id, camera_id,
        )
        return None

    # ── Slice and convert colour ───────────────────────────────────────
    crop_bgr = frame[y1:y2, x1:x2]
    crop_rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)

    # ── Apply OSNet preprocessing transform ───────────────────────────
    try:
        tensor = REID_TRANSFORM(crop_rgb)         # (3, H, W)
        tensor = tensor.unsqueeze(0)              # (1, 3, H, W)
    except Exception as exc:
        logger.warning(
            "Preprocessing failed for track %s camera %s: %s",
            track_id, camera_id, exc,
        )
        return None

    return PersonCrop(
        track_id=track_id,
        camera_id=camera_id,
        timestamp=timestamp,
        bbox_xyxy=(x1, y1, x2, y2),
        bbox_wh=(w, h),
        image_bgr=crop_bgr.copy(),
        tensor=tensor,
    )


def batch_crops_to_tensor(crops: list[PersonCrop]) -> torch.Tensor:
    """
    Stack a list of PersonCrop tensors into a single batched tensor.

    Returns
    -------
    torch.Tensor : shape (N, 3, H, W) suitable for OSNet forward pass
    """
    return torch.cat([c.tensor for c in crops], dim=0)
