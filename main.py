"""
main.py
───────
Entry point for the ReID subsystem.

Supports three usage modes:
  1. Multi-camera production run  (default)
  2. Single-camera with preview window (--show)
  3. Demo mode with synthetic webcam  (--demo)

Usage Examples
--------------
# Run on two RTSP cameras with API server
python main.py \\
    --cameras rtsp://192.168.1.10/stream1 rtsp://192.168.1.11/stream1 \\
    --camera-ids CAM_01 CAM_02 \\
    --serve

# Run on local video files
python main.py \\
    --cameras footage/entrance.mp4 footage/aisle.mp4 \\
    --camera-ids ENTRANCE AISLE

# Demo mode - webcam 0 only
python main.py --demo --show

# Tune thresholds via environment variables (no code change needed)
REID_SIMILARITY_THRESHOLD=0.85 REENTRY_WINDOW_SECONDS=600 python main.py --demo
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from typing import List, Optional

import click
import structlog

from config.settings import settings
from reid import OSNetEmbedder, VisitorRegistry, create_publisher
from pipeline.orchestrator import CameraPipeline
from api.server import run_server


# ─────────────────────────────────────────────────────────────────────
#  Logging setup
# ─────────────────────────────────────────────────────────────────────

def configure_logging() -> None:
    """Configure structlog for either JSON or console output."""
    log_level = getattr(logging, settings.LOG_LEVEL)

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if settings.LOG_FORMAT == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    # Also configure stdlib logging for third-party libs
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


# ─────────────────────────────────────────────────────────────────────
#  Graceful shutdown
# ─────────────────────────────────────────────────────────────────────

_shutdown_event = threading.Event()


def _handle_signal(sig, frame):  # noqa: ANN001
    logging.getLogger(__name__).info(
        "Signal %s received - initiating graceful shutdown", sig
    )
    _shutdown_event.set()


# ─────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────

@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--cameras",
    multiple=True,
    default=["0"],
    show_default=True,
    help="Video sources (webcam index, file path, or RTSP URL). "
         "Repeat for multiple cameras: --cameras CAM1 --cameras CAM2",
)
@click.option(
    "--camera-ids",
    multiple=True,
    default=None,
    help="Human-readable camera IDs (e.g. ENTRANCE AISLE). "
         "Must match --cameras count if provided.",
)
@click.option(
    "--serve",
    is_flag=True,
    default=False,
    help="Start the FastAPI REST API server alongside the pipelines.",
)
@click.option(
    "--show",
    is_flag=True,
    default=False,
    help="Display annotated preview window (requires display).",
)
@click.option(
    "--demo",
    is_flag=True,
    default=False,
    help="Demo mode: runs a single webcam (index 0) pipeline.",
)
@click.option(
    "--store-id",
    default="STORE_UNKNOWN",
    show_default=True,
    help="Store identifier from store_layout.json (e.g. STORE_BLR_002). "
         "Embedded in every emitted event.",
)
@click.option(
    "--max-frames",
    type=int,
    default=None,
    help="Stop after this many frames per camera (useful for testing).",
)
def main(
    cameras: tuple,
    camera_ids: tuple,
    serve: bool,
    show: bool,
    demo: bool,
    store_id: str,
    max_frames,
) -> None:
    """
    ReID Subsystem - Multi-camera person re-identification pipeline.

    \b
    Architecture:
        YOLO -> ByteTrack -> OSNet Embedder -> VisitorRegistry -> Events

    \b
    Key environment variables:
        REID_SIMILARITY_THRESHOLD   (default: 0.82)
        REENTRY_SIMILARITY_THRESHOLD (default: 0.80)
        REENTRY_WINDOW_SECONDS       (default: 300)
        REID_DEVICE                  (default: auto)
        LOG_LEVEL                    (default: INFO)
    """
    configure_logging()
    log = structlog.get_logger(__name__)

    # ── Resolve sources and IDs ───────────────────────────────────────
    if demo:
        sources     = [0]
        cam_ids     = ["CAM_DEMO"]
    else:
        sources = [int(s) if s.isdigit() else s for s in cameras]
        if camera_ids:
            if len(camera_ids) != len(sources):
                raise click.UsageError(
                    f"--camera-ids count ({len(camera_ids)}) must match "
                    f"--cameras count ({len(sources)})"
                )
            cam_ids = list(camera_ids)
        else:
            cam_ids = [f"CAM_{i+1:02d}" for i in range(len(sources))]

    log.info(
        "Starting ReID subsystem",
        cameras=cam_ids,
        reid_threshold=settings.REID_SIMILARITY_THRESHOLD,
        reentry_threshold=settings.REENTRY_SIMILARITY_THRESHOLD,
        reentry_window_s=settings.REENTRY_WINDOW_SECONDS,
        device=settings.REID_DEVICE,
        publisher=settings.EVENT_PUBLISHER,
    )

    # ── Signal handlers ───────────────────────────────────────────────
    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # ── Shared components ─────────────────────────────────────────────
    publisher = create_publisher()
    registry  = VisitorRegistry(publisher=publisher, store_id=store_id)

    log.info(
        "Shared registry ready — each camera will load its own embedder",
        store_id=store_id,
        osnet_model=settings.OSNET_MODEL_NAME,
        num_cameras=len(cam_ids),
    )

    # ── Camera pipelines ──────────────────────────────────────────────
    # Each camera gets its own OSNetEmbedder so camera threads never
    # share model state — eliminating race conditions on concurrent
    # GPU/CPU forward passes.  The VisitorRegistry stays shared so
    # cross-camera identity matching still works correctly.
    pipelines: List[CameraPipeline] = []
    for cam_id, source in zip(cam_ids, sources):
        embedder = OSNetEmbedder().load()   # one model instance per camera
        log.info(
            "Embedder loaded for camera",
            camera_id=cam_id,
            device=str(embedder.device),
        )
        p = CameraPipeline(
            camera_id=cam_id,
            source=source,
            registry=registry,
            embedder=embedder,
            show=show and len(sources) == 1,   # preview only in single-camera mode
            max_frames=max_frames,
        )
        pipelines.append(p)

    if len(pipelines) == 1 and not serve:
        # Single camera — run synchronously for simplicity
        log.info("Running single-camera pipeline synchronously")
        pipelines[0].run_sync()
    else:
        # Multi-camera or API server — run each pipeline in its own thread
        for p in pipelines:
            p.start()

        # ── Optional API server ───────────────────────────────────────
        if serve:
            api_thread = threading.Thread(
                target=run_server,
                args=(registry,),
                name="api-server",
                daemon=True,
            )
            api_thread.start()
            log.info(
                "API server started",
                url=f"http://{settings.API_HOST}:{settings.API_PORT}/docs",
            )

        # ── Wait for shutdown signal ──────────────────────────────────
        try:
            while not _shutdown_event.is_set():
                time.sleep(1.0)
                # Print periodic summary
                m = registry.get_metrics()
                log.info(
                    "Heartbeat",
                    active_visitors=m["active_visitors"],
                    total_unique=m["total_unique_visitors"],
                    reentries=m["total_reentries"],
                    cross_camera=m["total_cross_camera"],
                )
        except KeyboardInterrupt:
            pass
        finally:
            log.info("Stopping all camera pipelines…")
            for p in pipelines:
                p.stop()

    # ── Final summary ─────────────────────────────────────────────────
    m = registry.get_metrics()
    log.info(
        "ReID session complete",
        total_unique_visitors=m["total_unique_visitors"],
        total_reentries=m["total_reentries"],
        total_cross_camera=m["total_cross_camera"],
        total_exits=m["total_exits_recorded"],
    )
    publisher.close()
    sys.exit(0)


if __name__ == "__main__":
    main()
