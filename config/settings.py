"""
config/settings.py
──────────────────
Centralised, environment-variable-backed configuration for the
entire ReID subsystem.  All values can be overridden via a .env
file or shell environment variables with the same names.

Usage:
    from config.settings import settings
    threshold = settings.REID_SIMILARITY_THRESHOLD
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Single source of truth for every tuneable parameter.
    Environment variables take priority over defaults.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── ReID model ───────────────────────────────────────────────────
    OSNET_MODEL_NAME: str = Field(
        default="osnet_x1_0",
        description="torchreid model name. Options: osnet_x1_0, osnet_x0_75, "
                    "osnet_x0_5, osnet_ain_x1_0",
    )
    OSNET_PRETRAINED_DATASET: str = Field(
        default="msmt17",
        description="Dataset the pretrained weights were trained on. "
                    "Options: market1501, msmt17, dukemtmcreid",
    )
    EMBEDDING_DIM: int = Field(
        default=512,
        description="Dimension of the OSNet output embedding vector.",
    )
    REID_DEVICE: Literal["cpu", "cuda", "mps", "auto"] = Field(
        default="auto",
        description="Inference device. 'auto' selects CUDA > MPS > CPU.",
    )

    # ── Similarity thresholds ────────────────────────────────────────
    REID_SIMILARITY_THRESHOLD: float = Field(
        default=0.82,
        ge=0.0,
        le=1.0,
        description=(
            "Cosine similarity threshold for CROSS-CAMERA matching. "
            "Visitors above this threshold are treated as the same person. "
            "Range: 0.75–0.90.  Increase to reduce false merges; "
            "decrease to catch more same-person matches across cameras."
        ),
    )
    REENTRY_SIMILARITY_THRESHOLD: float = Field(
        default=0.80,
        ge=0.0,
        le=1.0,
        description=(
            "Cosine similarity threshold for RE-ENTRY detection. "
            "Slightly lower than cross-camera threshold because appearance "
            "drift is expected after a store exit (lighting, angle changes). "
            "Range: 0.75–0.88."
        ),
    )
    LOW_CONFIDENCE_THRESHOLD: float = Field(
        default=0.65,
        ge=0.0,
        le=1.0,
        description=(
            "Matches below this score are NOT merged — visitors remain separate. "
            "Prevents silent identity collisions."
        ),
    )

    # ── Re-entry detection ───────────────────────────────────────────
    REENTRY_WINDOW_SECONDS: int = Field(
        default=300,
        ge=30,
        description=(
            "How long after exit a person is eligible for re-entry detection "
            "(seconds).  Default: 5 minutes.  Increase for large shopping malls."
        ),
    )

    # ── Registry management ──────────────────────────────────────────
    MAX_EXITED_REGISTRY_SIZE: int = Field(
        default=5000,
        description="Maximum number of exited visitor records retained in memory.",
    )
    REGISTRY_GC_INTERVAL_SECONDS: int = Field(
        default=60,
        description="How often to run garbage-collection on expired exited records.",
    )
    ACTIVE_VISITOR_TIMEOUT_SECONDS: int = Field(
        default=30,
        description=(
            "Seconds since last_seen before an active visitor is auto-moved "
            "to exited status (handles lost tracks)."
        ),
    )

    # ── Detection ────────────────────────────────────────────────────
    YOLO_MODEL: str = Field(
        default="yolov8n.pt",
        description="YOLOv8 model weights. Use yolov8n.pt (fast) or yolov8m.pt (accurate).",
    )
    YOLO_CONFIDENCE: float = Field(
        default=0.40,
        ge=0.1,
        le=1.0,
        description="Minimum detector confidence for a person detection.",
    )
    YOLO_IOU_THRESHOLD: float = Field(
        default=0.45,
        description="NMS IOU threshold for YOLO detector.",
    )
    YOLO_PERSON_CLASS_ID: int = Field(
        default=0,
        description="COCO class ID for 'person'. Do not change unless using custom model.",
    )

    # ── Tracking (ByteTrack) ─────────────────────────────────────────
    BYTETRACK_TRACK_THRESH: float = Field(default=0.5)
    BYTETRACK_TRACK_BUFFER: int = Field(default=30)
    BYTETRACK_MATCH_THRESH: float = Field(default=0.8)
    BYTETRACK_FRAME_RATE: int = Field(default=25)

    # ── Staff detection ──────────────────────────────────────────
    STAFF_ZONE_TOP_PCT: float = Field(
        default=0.15,
        ge=0.0,
        le=0.5,
        description=(
            "Tier 1 staff detection: fraction of frame height (from top) "
            "treated as a staff-only zone. 0.15 = top 15 %% of frame. "
            "Calibrate per store/camera layout."
        ),
    )
    STAFF_STATIC_STD_PX: float = Field(
        default=8.0,
        ge=1.0,
        description=(
            "Tier 2 staff detection: centroid std-dev (pixels) below which "
            "a track is considered stationary (likely staff). Lower = stricter."
        ),
    )
    STAFF_DWELL_FRAMES: int = Field(
        default=150,
        ge=30,
        description=(
            "Tier 2 staff detection: number of consecutive frames with low "
            "centroid movement required to classify a track as staff. "
            "At 25 FPS this is 6 seconds."
        ),
    )
    STAFF_UNIFORM_HUE_MIN: int = Field(
        default=-1,
        description=(
            "Tier 3 uniform detection: minimum OpenCV HSV hue (0–179). "
            "Set to -1 (default) to disable colour-based staff detection."
        ),
    )
    STAFF_UNIFORM_HUE_MAX: int = Field(
        default=-1,
        description=(
            "Tier 3 uniform detection: maximum OpenCV HSV hue (0–179). "
            "Set to -1 (default) to disable colour-based staff detection."
        ),
    )

    # ── Crop preprocessing ───────────────────────────────────────────
    CROP_HEIGHT: int = Field(
        default=256,
        description="Person crop height fed to OSNet. OSNet expects 256×128.",
    )
    CROP_WIDTH: int = Field(
        default=128,
        description="Person crop width fed to OSNet.",
    )
    MIN_CROP_AREA: int = Field(
        default=1600,
        description="Minimum bounding-box pixel area (w×h) to attempt ReID. "
                    "Very small crops degrade embedding quality.",
    )

    # ── API ───────────────────────────────────────────────────────────
    API_HOST: str = Field(default="0.0.0.0")
    API_PORT: int = Field(default=8000)
    API_RELOAD: bool = Field(default=False)

    # ── Logging ───────────────────────────────────────────────────────
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    LOG_FORMAT: Literal["json", "console"] = Field(default="console")

    # ── Event publishing ─────────────────────────────────────────────
    EVENT_PUBLISHER: Literal["stdout", "redis", "http"] = Field(
        default="stdout",
        description="Where to publish ReID events. 'redis' requires REDIS_URL.",
    )
    REDIS_URL: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL (used when EVENT_PUBLISHER=redis).",
    )
    REDIS_CHANNEL: str = Field(default="reid_events")

    # ── Validators ───────────────────────────────────────────────────
    @field_validator("REENTRY_SIMILARITY_THRESHOLD")
    @classmethod
    def reentry_below_cross_camera(cls, v: float, info) -> float:  # noqa: ANN001
        cross = info.data.get("REID_SIMILARITY_THRESHOLD", 0.82)
        if v > cross:
            raise ValueError(
                f"REENTRY_SIMILARITY_THRESHOLD ({v}) must be ≤ "
                f"REID_SIMILARITY_THRESHOLD ({cross})"
            )
        return v


# Module-level singleton — import this everywhere
settings = Settings()
