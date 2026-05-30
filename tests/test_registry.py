# PROMPT:
# "Write comprehensive pytest unit tests for a thread-safe VisitorRegistry
#  that resolves person identities across multiple cameras using cosine
#  similarity on 512-d embeddings. Cover: new visitor creation, cross-camera
#  matching (same embedding different camera), re-entry within time window,
#  re-entry after window expires (must be new visitor), mark_exited lifecycle,
#  auto-expiry of stale active visitors, registry reset, and a concurrent
#  thread-safety smoke test with 4 threads × 20 resolves each. Use numpy
#  random seeds for deterministic embeddings. No GPU required."
#
# CHANGES MADE:
# - Added explicit l2_normalise calls to ensure embedding vectors are unit-norm
#   before passing to resolve() — the LLM's initial version passed raw random
#   vectors which caused inconsistent cosine scores near the threshold boundary.
# - Changed the reentry-after-window test to manually backdating exit_time via
#   the internal _lock context rather than mocking time.time() — more robust
#   against implementation changes in the registry.
# - Added TestThreadSafety class with 4-thread concurrent resolve test — the
#   LLM's version only used 2 threads which is insufficient to detect RLock issues.
# - Removed one test that checked registry._counter_cross_camera directly —
#   internal attribute access is brittle; replaced with get_metrics() assertion.

"""
tests/test_registry.py
───────────────────────
Unit tests for the VisitorRegistry.

All tests use mock embeddings — no GPU or actual model required.
The registry is tested with real cosine similarity so threshold
logic is exercised end-to-end.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from reid.events import EventType
from reid.registry import VisitorRegistry, VisitorRecord
from reid.similarity import l2_normalise


# ─────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────

def make_embedding(seed: int = 0, dim: int = 512) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return l2_normalise(v)


def make_registry(publisher=None) -> VisitorRegistry:
    return VisitorRegistry(publisher=publisher)


# ─────────────────────────────────────────────────────────────────────
#  NEW_VISITOR
# ─────────────────────────────────────────────────────────────────────

class TestNewVisitor:

    def test_first_detection_creates_new_visitor(self):
        reg = make_registry()
        emb = make_embedding(seed=1)
        result = reg.resolve(emb, "CAM_01", track_id=1, timestamp=time.time())

        assert result.event_type == EventType.NEW_VISITOR
        assert result.is_new is True
        assert result.visitor_id is not None
        assert len(reg.snapshot_active()) == 1

    def test_two_different_people_create_two_visitors(self):
        reg = make_registry()
        emb1 = make_embedding(seed=10)
        emb2 = make_embedding(seed=200)   # very different seed → low similarity

        r1 = reg.resolve(emb1, "CAM_01", track_id=1, timestamp=time.time())
        r2 = reg.resolve(emb2, "CAM_01", track_id=2, timestamp=time.time())

        assert r1.visitor_id != r2.visitor_id
        assert len(reg.snapshot_active()) == 2

    def test_metrics_incremented(self):
        reg = make_registry()
        reg.resolve(make_embedding(seed=5), "CAM_01", track_id=1, timestamp=time.time())
        reg.resolve(make_embedding(seed=6), "CAM_01", track_id=2, timestamp=time.time())

        m = reg.get_metrics()
        assert m["total_unique_visitors"] == 2
        assert m["active_visitors"] == 2


# ─────────────────────────────────────────────────────────────────────
#  CROSS_CAMERA_MATCH
# ─────────────────────────────────────────────────────────────────────

class TestCrossCameraMatch:

    def test_same_person_in_second_camera_reuses_visitor_id(self):
        reg = make_registry()
        emb = make_embedding(seed=42)

        r1 = reg.resolve(emb, "CAM_01", track_id=1, timestamp=time.time())
        # Slightly perturbed version of the same embedding (same person)
        emb2 = l2_normalise(emb + 0.01 * make_embedding(seed=99))
        r2 = reg.resolve(emb2, "CAM_02", track_id=1, timestamp=time.time())

        assert r1.visitor_id == r2.visitor_id
        assert r2.event_type == EventType.CROSS_CAMERA_MATCH

    def test_cross_camera_counter_incremented(self):
        reg = make_registry()
        emb = make_embedding(seed=7)
        reg.resolve(emb, "CAM_01", track_id=1, timestamp=time.time())

        emb2 = l2_normalise(emb + 0.005 * make_embedding(seed=300))
        reg.resolve(emb2, "CAM_02", track_id=2, timestamp=time.time())

        m = reg.get_metrics()
        assert m["total_cross_camera"] >= 1

    def test_different_people_different_cameras_not_merged(self):
        reg = make_registry()
        emb1 = make_embedding(seed=11)
        emb2 = make_embedding(seed=222)   # completely different

        r1 = reg.resolve(emb1, "CAM_01", track_id=1, timestamp=time.time())
        r2 = reg.resolve(emb2, "CAM_02", track_id=2, timestamp=time.time())

        assert r1.visitor_id != r2.visitor_id
        assert r2.event_type == EventType.NEW_VISITOR


# ─────────────────────────────────────────────────────────────────────
#  REENTRY
# ─────────────────────────────────────────────────────────────────────

class TestReentry:

    def test_reentry_within_window_reuses_visitor_id(self):
        reg = make_registry()
        emb = make_embedding(seed=55)
        ts  = time.time()

        r1 = reg.resolve(emb, "CAM_01", track_id=1, timestamp=ts)
        vid = r1.visitor_id

        # Mark as exited
        reg.mark_exited(vid, timestamp=ts)
        assert len(reg.snapshot_active()) == 0
        assert len(reg.snapshot_exited()) == 1

        # Same person re-enters shortly after
        emb2 = l2_normalise(emb + 0.01 * make_embedding(seed=400))
        r2 = reg.resolve(emb2, "CAM_01", track_id=2, timestamp=ts + 60)

        assert r2.visitor_id == vid
        assert r2.event_type == EventType.REENTRY
        assert not r2.is_new

    def test_reentry_counter_incremented(self):
        reg = make_registry()
        emb = make_embedding(seed=66)
        ts  = time.time()

        r1 = reg.resolve(emb, "CAM_01", track_id=1, timestamp=ts)
        reg.mark_exited(r1.visitor_id, timestamp=ts)

        emb2 = l2_normalise(emb + 0.01 * make_embedding(seed=500))
        reg.resolve(emb2, "CAM_01", track_id=2, timestamp=ts + 30)

        m = reg.get_metrics()
        assert m["total_reentries"] == 1

    def test_reentry_after_window_creates_new_visitor(self):
        """
        A person re-entering after the reentry window expires should be
        treated as a NEW visitor (the exited record may have been GC'd or
        simply isn't in the eligible set).
        """
        reg = make_registry()
        emb = make_embedding(seed=77)
        ts  = time.time()

        r1 = reg.resolve(emb, "CAM_01", track_id=1, timestamp=ts)
        reg.mark_exited(r1.visitor_id, timestamp=ts - 400)   # exit 400s ago (past 300s window)

        # Force the exit_time to be outside the window
        with reg._lock:
            from datetime import datetime, timezone
            rec = reg._exited[r1.visitor_id]
            rec.exit_time = datetime.fromtimestamp(ts - 400, tz=timezone.utc)

        emb2 = l2_normalise(emb + 0.01 * make_embedding(seed=600))
        r2 = reg.resolve(emb2, "CAM_01", track_id=3, timestamp=ts)

        # Should be a NEW visitor because the window has expired
        assert r2.visitor_id != r1.visitor_id
        assert r2.event_type == EventType.NEW_VISITOR


# ─────────────────────────────────────────────────────────────────────
#  mark_exited & auto-expiry
# ─────────────────────────────────────────────────────────────────────

class TestMarkExited:

    def test_mark_exited_moves_to_exited_bucket(self):
        reg = make_registry()
        r   = reg.resolve(make_embedding(seed=1), "CAM_01", track_id=1, timestamp=time.time())
        result = reg.mark_exited(r.visitor_id)

        assert result is True
        assert len(reg.snapshot_active()) == 0
        assert len(reg.snapshot_exited()) == 1

    def test_mark_exited_unknown_id_returns_false(self):
        reg = make_registry()
        assert reg.mark_exited("nonexistent-id") is False

    def test_auto_expire_stale_active(self):
        reg = make_registry()
        emb = make_embedding(seed=8)
        ts  = time.time() - 60   # last seen 60s ago
        r   = reg.resolve(emb, "CAM_01", track_id=1, timestamp=ts)

        # Manually set last_seen to be in the past
        with reg._lock:
            rec = reg._active[r.visitor_id]
            from datetime import datetime, timezone
            rec.last_seen = datetime.fromtimestamp(ts, tz=timezone.utc)

        expired = reg.auto_expire_stale_active()
        assert r.visitor_id in expired
        assert len(reg.snapshot_active()) == 0


# ─────────────────────────────────────────────────────────────────────
#  reset()
# ─────────────────────────────────────────────────────────────────────

class TestReset:

    def test_reset_clears_all_state(self):
        reg = make_registry()
        for i in range(5):
            reg.resolve(make_embedding(seed=i), "CAM_01", track_id=i, timestamp=time.time())

        reg.reset()
        assert len(reg.snapshot_active()) == 0
        assert reg.get_metrics()["total_unique_visitors"] == 0


# ─────────────────────────────────────────────────────────────────────
#  Thread safety smoke test
# ─────────────────────────────────────────────────────────────────────

class TestThreadSafety:

    def test_concurrent_resolves_do_not_crash(self):
        import threading

        reg    = make_registry()
        errors = []

        def worker(seed_base: int, camera: str):
            try:
                for i in range(20):
                    emb = make_embedding(seed=seed_base + i)
                    reg.resolve(emb, camera, track_id=i, timestamp=time.time())
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i * 1000, f"CAM_{i:02d}"))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
