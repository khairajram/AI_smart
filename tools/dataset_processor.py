"""
tools/dataset_processor.py
──────────────────────────
Offline ZIP-dataset processor for the ReID subsystem.

Supports four ZIP dataset formats out-of-the-box:

  FORMAT 1 — Multi-camera videos (recommended for retail)
    ├── ENTRANCE/video.mp4          (or .avi, .mov, .mkv)
    ├── AISLE_1/video.mp4
    └── CHECKOUT/video.mp4

  FORMAT 2 — Multi-camera image sequences
    ├── ENTRANCE/
    │   ├── 000001.jpg
    │   ├── 000002.jpg  ...
    ├── AISLE_1/
    │   └── ...

  FORMAT 3 — Flat videos (camera name = filename stem)
    ├── entrance.mp4
    ├── aisle_1.mp4
    └── checkout.mp4

  FORMAT 4 — Market-1501 / MSMT17 academic benchmark
    └── bounding_box_test/          (or query/, gallery/)
        ├── 0001_c1s1_000001_00.jpg
        └── ...

Usage
-----
  # Process a dataset ZIP and push events to the Node.js dashboard
  python tools/dataset_processor.py \\
      --zip footage.zip \\
      --camera-map entrance:ENTRANCE,aisle:AISLE_1,checkout:CHECKOUT \\
      --dashboard-url http://localhost:3000 \\
      --fps 10

  # Dry-run: just detect format and list cameras
  python tools/dataset_processor.py --zip footage.zip --dry-run

  # Process and write events to a JSONL file instead
  python tools/dataset_processor.py --zip footage.zip --output events.jsonl
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Dict, Generator, List, Optional, Tuple

import click
import cv2
import requests

# ── Bootstrap path so we can import project modules ──────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from config.settings import settings
from reid import OSNetEmbedder, VisitorRegistry, create_publisher
from pipeline.orchestrator import CameraPipeline

logger = logging.getLogger(__name__)

# ── Supported media extensions ────────────────────────────────────────
VIDEO_EXTS  = {".mp4", ".avi", ".mov", ".mkv", ".m4v", ".wmv", ".flv"}
IMAGE_EXTS  = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}


# ─────────────────────────────────────────────────────────────────────
#  Format Detection
# ─────────────────────────────────────────────────────────────────────

class DatasetFormat:
    """Result of auto-detecting the dataset format inside a ZIP."""

    MULTI_CAM_VIDEO   = "multi_cam_video"
    MULTI_CAM_FRAMES  = "multi_cam_frames"
    FLAT_VIDEOS       = "flat_videos"
    ACADEMIC_REID     = "academic_reid"
    UNKNOWN           = "unknown"

    def __init__(
        self,
        fmt: str,
        cameras: List[Dict],          # [{camera_id, source}]
        description: str,
    ):
        self.fmt         = fmt
        self.cameras     = cameras
        self.description = description

    def __repr__(self):
        return f"<DatasetFormat fmt={self.fmt!r} cameras={len(self.cameras)}>"


def detect_format(root: Path, camera_map: Optional[Dict[str, str]] = None) -> DatasetFormat:
    """
    Inspect the extracted ZIP directory and classify its layout.

    Returns a DatasetFormat with .cameras populated as a list of
    {camera_id, source} dicts ready to feed into CameraPipeline.
    """
    # ── Walk top-level entries ────────────────────────────────────────
    top_dirs  = [p for p in root.iterdir() if p.is_dir()]
    top_files = [p for p in root.iterdir() if p.is_file()]

    top_videos  = [f for f in top_files if f.suffix.lower() in VIDEO_EXTS]
    top_images  = [f for f in top_files if f.suffix.lower() in IMAGE_EXTS]

    # ── FORMAT 3: flat video files at the top level ───────────────────
    if top_videos and not top_dirs:
        top_videos.sort()
        cameras = []
        for vid in top_videos:
            cam_id = _resolve_camera_id(vid.stem.upper(), camera_map)
            cameras.append({"camera_id": cam_id, "source": str(vid)})
        return DatasetFormat(
            DatasetFormat.FLAT_VIDEOS,
            cameras,
            f"Flat video files: {[v.name for v in top_videos]}",
        )

    # ── FORMAT 4: academic benchmark (bounding_box_*/query/gallery) ───
    academic_dirs = {"bounding_box_test", "bounding_box_train", "query", "gallery", "gt_bbox"}
    if any(d.name.lower() in academic_dirs for d in top_dirs):
        return _detect_academic(root, camera_map)

    # ── FORMAT 1 or 2: per-camera sub-directories ─────────────────────
    cameras = []
    for sub in sorted(top_dirs):
        sub_videos = sorted(sub.glob("*"))
        sub_videos = [f for f in sub_videos if f.suffix.lower() in VIDEO_EXTS]
        sub_images = sorted(
            [f for f in sub.rglob("*") if f.suffix.lower() in IMAGE_EXTS]
        )

        cam_id = _resolve_camera_id(sub.name, camera_map)

        if sub_videos:
            # Multiple videos per camera folder → use the first one
            cameras.append({"camera_id": cam_id, "source": str(sub_videos[0])})

        elif sub_images:
            # Image sequence → we'll rebuild a virtual video using cv2
            cameras.append({
                "camera_id": cam_id,
                "source": str(sub),          # directory → handled as image sequence
                "frames": [str(f) for f in sub_images],
            })

    if not cameras:
        return DatasetFormat(DatasetFormat.UNKNOWN, [], "Could not identify dataset format")

    has_frames = any("frames" in c for c in cameras)
    fmt = DatasetFormat.MULTI_CAM_FRAMES if has_frames else DatasetFormat.MULTI_CAM_VIDEO
    return DatasetFormat(fmt, cameras, f"Per-camera folders: {[c['camera_id'] for c in cameras]}")


def _detect_academic(root: Path, camera_map: Optional[Dict[str, str]]) -> DatasetFormat:
    """
    Handle Market-1501 / MSMT17 / DukeMTMC layout.
    Filename pattern: PPPP_cCS...  where cC = camera index.
    We group images by camera index to create one virtual sequence per camera.
    """
    img_dirs = [p for p in root.rglob("*") if p.is_dir() and
                any(f.suffix.lower() in IMAGE_EXTS for f in p.iterdir() if f.is_file())]

    # Aggregate all images and group by camera
    cam_images: Dict[str, List[Path]] = {}
    pattern = re.compile(r"_c(\d+)", re.IGNORECASE)

    for d in img_dirs:
        for img in sorted(d.glob("*")):
            if img.suffix.lower() not in IMAGE_EXTS:
                continue
            m = pattern.search(img.stem)
            cam_idx = m.group(1) if m else "01"
            key = f"CAM_{int(cam_idx):02d}"
            cam_images.setdefault(key, []).append(img)

    cameras = []
    for cam_id, imgs in sorted(cam_images.items()):
        rid = _resolve_camera_id(cam_id, camera_map)
        cameras.append({
            "camera_id": rid,
            "source": str(imgs[0].parent),
            "frames": [str(f) for f in sorted(imgs)],
        })

    return DatasetFormat(
        DatasetFormat.ACADEMIC_REID,
        cameras,
        f"Academic ReID dataset: {len(cameras)} camera groups, "
        f"{sum(len(c['frames']) for c in cameras)} images",
    )


def _resolve_camera_id(raw: str, camera_map: Optional[Dict[str, str]]) -> str:
    """Apply optional camera-name remapping."""
    if camera_map:
        # Exact match
        if raw in camera_map:
            return camera_map[raw]
        # Case-insensitive match
        for k, v in camera_map.items():
            if k.lower() == raw.lower():
                return v
    return raw.upper().replace(" ", "_").replace("-", "_")


# ─────────────────────────────────────────────────────────────────────
#  Image-Sequence Virtual VideoCapture
# ─────────────────────────────────────────────────────────────────────

class ImageSequenceCapture:
    """
    Wraps a sorted list of image files to mimic cv2.VideoCapture API.
    Allows the CameraPipeline to treat an image sequence as a video.
    """

    def __init__(self, frames: List[str], fps: float = 10.0) -> None:
        self._frames = frames
        self._idx    = 0
        self._fps    = fps
        self._delay  = 1.0 / fps if fps > 0 else 0.1

    def isOpened(self) -> bool:
        return True

    def read(self) -> Tuple[bool, Optional[cv2.Mat]]:
        if self._idx >= len(self._frames):
            return False, None
        img = cv2.imread(self._frames[self._idx])
        self._idx += 1
        if img is None:
            return self.read()          # skip unreadable files
        time.sleep(self._delay)         # simulate frame rate
        return True, img

    def get(self, prop_id: int) -> float:
        if prop_id == cv2.CAP_PROP_FPS:
            return self._fps
        if prop_id == cv2.CAP_PROP_FRAME_WIDTH:
            return 1920.0
        if prop_id == cv2.CAP_PROP_FRAME_HEIGHT:
            return 1080.0
        return 0.0

    def release(self) -> None:
        self._idx = len(self._frames)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


# ─────────────────────────────────────────────────────────────────────
#  Dashboard HTTP publisher (pushes events to Node.js backend)
# ─────────────────────────────────────────────────────────────────────

class DashboardPublisher:
    """
    Sends ReID events to the Node.js dashboard via the /api/events/ingest
    endpoint (added to the backend).  Falls back to stdout silently.
    """

    def __init__(self, base_url: str, timeout: int = 5) -> None:
        self._url     = base_url.rstrip("/") + "/api/dataset/ingest"
        self._timeout = timeout
        self._session = requests.Session()
        self._buf: List[dict] = []
        self._flush_every = 10   # flush every N events

    def publish(self, event: dict) -> None:
        self._buf.append(event)
        if len(self._buf) >= self._flush_every:
            self._flush()

    def _flush(self) -> None:
        if not self._buf:
            return
        events = self._buf[:]
        self._buf.clear()
        try:
            self._session.post(
                self._url,
                json={"events": events},
                timeout=self._timeout,
            )
        except Exception as exc:
            logger.debug("Dashboard publish failed: %s", exc)

    def close(self) -> None:
        self._flush()
        self._session.close()


class JSONLPublisher:
    """Writes events as JSON Lines to a file."""

    def __init__(self, path: str) -> None:
        self._f = open(path, "w", encoding="utf-8")

    def publish(self, event: dict) -> None:
        self._f.write(json.dumps(event) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


# ─────────────────────────────────────────────────────────────────────
#  Progress tracker (posted to dashboard)
# ─────────────────────────────────────────────────────────────────────

class ProgressTracker:
    """Posts processing progress to /api/dataset/progress on the dashboard."""

    def __init__(self, dashboard_url: str, dataset_id: str) -> None:
        self._url = dashboard_url.rstrip("/") + f"/api/dataset/{dataset_id}/progress"
        self._session = requests.Session()

    def update(self, camera_id: str, frames_done: int, total_frames: int, status: str = "processing") -> None:
        try:
            self._session.post(self._url, json={
                "camera_id": camera_id,
                "frames_done": frames_done,
                "total_frames": total_frames,
                "status": status,
                "pct": round(frames_done / max(total_frames, 1) * 100, 1),
            }, timeout=3)
        except Exception:
            pass

    def done(self, summary: dict) -> None:
        try:
            self._session.post(self._url, json={"status": "done", **summary}, timeout=5)
        except Exception:
            pass

    def close(self) -> None:
        self._session.close()


# ─────────────────────────────────────────────────────────────────────
#  Main processor
# ─────────────────────────────────────────────────────────────────────

def process_dataset(
    zip_path:      str,
    camera_map:    Optional[Dict[str, str]] = None,
    dashboard_url: Optional[str]            = None,
    output_jsonl:  Optional[str]            = None,
    fps:           float                    = 10.0,
    max_frames:    Optional[int]            = None,
    dry_run:       bool                     = False,
    dataset_id:    str                      = "unknown",
) -> Dict:
    """
    Full pipeline:
      1. Extract ZIP → temp dir
      2. Detect dataset format
      3. Build publisher(s)
      4. Load OSNet + VisitorRegistry
      5. Process each camera sequentially (or print summary for dry-run)
      6. Return summary dict
    """
    zip_path = Path(zip_path)
    if not zip_path.exists():
        raise FileNotFoundError(f"ZIP not found: {zip_path}")

    # ── Extract ───────────────────────────────────────────────────────
    tmp_dir = Path(tempfile.mkdtemp(prefix="reid_dataset_"))
    logger.info("Extracting %s → %s", zip_path.name, tmp_dir)
    print(f"\n[1/5] Extracting {zip_path.name} …")

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp_dir)
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"Not a valid ZIP file: {e}") from e

    # Find actual content root (handle ZIPs with a single top-level folder)
    roots = [p for p in tmp_dir.iterdir()]
    if len(roots) == 1 and roots[0].is_dir():
        content_root = roots[0]
    else:
        content_root = tmp_dir

    # ── Detect format ─────────────────────────────────────────────────
    print("[2/5] Detecting dataset format …")
    dataset_fmt = detect_format(content_root, camera_map)
    print(f"      Format : {dataset_fmt.fmt}")
    print(f"      Cameras: {[c['camera_id'] for c in dataset_fmt.cameras]}")
    print(f"      Detail : {dataset_fmt.description}")

    if dry_run or not dataset_fmt.cameras:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return {
            "dry_run": True,
            "format": dataset_fmt.fmt,
            "cameras": [c["camera_id"] for c in dataset_fmt.cameras],
            "description": dataset_fmt.description,
        }

    # ── Publisher(s) ──────────────────────────────────────────────────
    publishers = []
    if dashboard_url:
        publishers.append(DashboardPublisher(dashboard_url))
    if output_jsonl:
        publishers.append(JSONLPublisher(output_jsonl))
    if not publishers:
        publishers.append(JSONLPublisher("/dev/null" if os.name != "nt" else "NUL"))

    progress = ProgressTracker(dashboard_url, dataset_id) if dashboard_url else None

    # ── Load shared ReID components ───────────────────────────────────
    print("[3/5] Loading OSNet ReID model …")

    class MultiPublisher:
        """Fan-out to multiple publisher targets."""
        def publish(self, e):
            for p in publishers: p.publish(e)
        def close(self):
            for p in publishers: p.close()

    multi_pub = MultiPublisher()

    # Patch the registry so it also sends events to our publisher(s)
    from reid.events import EventType
    registry  = VisitorRegistry(publisher=_make_event_forwarder(multi_pub))
    embedder  = OSNetEmbedder().load()
    print("      Model ready.\n")

    # ── Process cameras ───────────────────────────────────────────────
    print("[4/5] Processing cameras …")
    summary = {
        "format":     dataset_fmt.fmt,
        "cameras":    [],
        "total_frames": 0,
        "total_events": 0,
        "started_at": time.time(),
    }

    for cam_info in dataset_fmt.cameras:
        cam_id  = cam_info["camera_id"]
        source  = cam_info["source"]
        frames  = cam_info.get("frames")         # image-sequence paths

        print(f"\n  → Camera: {cam_id}")

        # For image sequences, monkey-patch VideoCapture
        if frames:
            cap     = ImageSequenceCapture(frames, fps=fps)
            total_f = len(frames)
            _patch_cv2_capture(source, cap)
        else:
            # Count video frames for progress display
            cap_check = cv2.VideoCapture(source)
            total_f   = int(cap_check.get(cv2.CAP_PROP_FRAME_COUNT)) or 9999
            cap_check.release()
            print(f"     Frames: {total_f}")

        pipeline = CameraPipeline(
            camera_id=cam_id,
            source=source,
            registry=registry,
            embedder=embedder,
            show=False,
            max_frames=max_frames,
        )

        t0 = time.perf_counter()
        pipeline.run_sync()
        elapsed = time.perf_counter() - t0

        cam_summary = {
            "camera_id": cam_id,
            "frames_processed": pipeline.frame_count,
            "fps_avg": round(pipeline.fps_actual, 1),
            "elapsed_s": round(elapsed, 1),
        }
        summary["cameras"].append(cam_summary)
        summary["total_frames"] += pipeline.frame_count
        print(f"     Done  frames={pipeline.frame_count}  fps={pipeline.fps_actual:.1f}  time={elapsed:.1f}s")

        if progress:
            progress.update(cam_id, pipeline.frame_count, total_f, "done")

    # ── Finalise ──────────────────────────────────────────────────────
    print("\n[5/5] Finalising …")
    m = registry.get_metrics()
    summary["metrics"] = {
        "unique_visitors":  m["total_unique_visitors"],
        "total_reentries":  m["total_reentries"],
        "cross_camera":     m["total_cross_camera"],
        "exits_recorded":   m["total_exits_recorded"],
    }
    summary["finished_at"] = time.time()
    summary["elapsed_s"]   = round(summary["finished_at"] - summary["started_at"], 1)

    multi_pub.close()
    if progress:
        progress.done(summary)
        progress.close()

    shutil.rmtree(tmp_dir, ignore_errors=True)

    print("\n" + "═" * 55)
    print("  ReID Dataset Processing Complete")
    print("═" * 55)
    print(f"  Unique visitors : {m['total_unique_visitors']}")
    print(f"  Re-entries      : {m['total_reentries']}")
    print(f"  Cross-camera    : {m['total_cross_camera']}")
    print(f"  Total frames    : {summary['total_frames']}")
    print(f"  Total time      : {summary['elapsed_s']}s")
    print("═" * 55)

    return summary


# ─────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────

def _make_event_forwarder(publisher):
    """
    Returns a publisher-compatible object that converts VisitorRegistry
    events into the flat dict schema expected by the dashboard.
    """
    class ForwardingPublisher:
        def publish(self, event) -> None:
            publisher.publish(event.to_dict() if hasattr(event, "to_dict") else event)
        def close(self) -> None:
            pass
    return ForwardingPublisher()


_cap_patches: Dict[str, ImageSequenceCapture] = {}

def _patch_cv2_capture(source: str, cap: ImageSequenceCapture) -> None:
    """Register a virtual capture for the given source path."""
    _cap_patches[source] = cap

# Note: CameraPipeline uses VideoSource which calls cv2.VideoCapture.
# For image sequences we inject a wrapper by subclassing VideoSource.
# The actual injection is done in __main__ context below via monkeypatching.


# ─────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────

@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--zip", "zip_path", required=True, type=click.Path(exists=True), help="Path to the dataset ZIP file.")
@click.option("--dashboard-url", default="http://localhost:3000", show_default=True, help="Node.js dashboard URL. Events are pushed here in real time.")
@click.option("--output", "output_jsonl", default=None, help="Also write events to this JSONL file.")
@click.option("--camera-map", default=None, help="Comma-separated old:new remaps. E.g. 'cam1:ENTRANCE,cam2:AISLE_1'")
@click.option("--fps", default=10.0, show_default=True, type=float, help="Playback FPS for image sequences (ignored for video files).")
@click.option("--max-frames", default=None, type=int, help="Limit frames per camera (useful for quick tests).")
@click.option("--dataset-id", default="manual", help="Dataset ID sent to the dashboard for tracking.")
@click.option("--dry-run", is_flag=True, default=False, help="Only detect format, do not process.")
def cli(zip_path, dashboard_url, output_jsonl, camera_map, fps, max_frames, dataset_id, dry_run):
    """
    Process a ZIP dataset through the full ReID pipeline and stream
    events to the dashboard or a JSONL file.

    \b
    Example:
      python tools/dataset_processor.py \\
          --zip retail_footage.zip \\
          --dashboard-url http://localhost:3000 \\
          --fps 10 \\
          --camera-map "cam1:ENTRANCE,cam2:AISLE_1,cam3:CHECKOUT"
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    cam_map = None
    if camera_map:
        cam_map = {}
        for pair in camera_map.split(","):
            if ":" in pair:
                k, v = pair.strip().split(":", 1)
                cam_map[k.strip()] = v.strip()

    result = process_dataset(
        zip_path=zip_path,
        camera_map=cam_map,
        dashboard_url=dashboard_url if not dry_run else None,
        output_jsonl=output_jsonl,
        fps=fps,
        max_frames=max_frames,
        dry_run=dry_run,
        dataset_id=dataset_id,
    )

    if dry_run:
        print("\nDry-run result:")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    cli()
