# PROMPT:
# "Write pytest integration tests for a FastAPI Store Intelligence API.
#  Test these endpoints:
#  - POST /events/ingest: valid batch accepted, schema validation rejects bad events,
#    idempotency (same payload twice = accepted + duplicate), batch size >500 rejected.
#  - GET /stores/{id}/metrics: returns required fields (unique_visitors, conversion_rate,
#    avg_dwell_per_zone, queue_depth, abandonment_rate). Empty store returns zero metrics.
#  - GET /stores/{id}/funnel: stages list has entry/zone_visit/billing_queue/purchase,
#    drop_off_pct is non-negative, stages are monotonically decreasing.
#  - GET /stores/{id}/heatmap: zones list, data_confidence flag, score range 0-100.
#  - GET /stores/{id}/anomalies: returns anomalies list, each has anomaly_type/severity/
#    detail/suggested_action. Queue spike detected when queue_depth > threshold.
#  - GET /health: returns ok status, stores dict with per-store status.
#  Use TestClient (sync) against a test SQLite DB in /tmp, reset between tests."
#
# CHANGES MADE:
# - Used a tmp_path pytest fixture for the DB path instead of /tmp hardcode —
#   the LLM's initial version left test DBs on disk permanently.
# - Added monkeypatching of api.db._DB_PATH to redirect to the test DB.
# - The LLM missed testing idempotency — added explicit double-ingest test
#   verifying accepted + duplicate = original batch size.
# - Added edge case: GET /stores/{nonexistent}/metrics should return 0 values,
#   not 404 — the LLM raised HTTPException but the spec says return zeros.
# - Fixed event timestamp format — the LLM used a non-UTC string that failed
#   ISO-8601 validation.

"""
tests/test_api.py
─────────────────
Integration tests for the Store Intelligence API.

Uses FastAPI TestClient — no real server needed.
All tests use an isolated SQLite DB in a pytest tmp_path fixture.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient


# ─────────────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch):
    """Redirect the SQLite DB to a fresh temp file for each test."""
    import api.db as db_module
    db_path = tmp_path / "test_store.db"
    monkeypatch.setattr(db_module, "_DB_PATH", db_path)
    db_module.init_db(db_path)
    yield
    # Cleanup handled automatically by tmp_path


@pytest.fixture
def client():
    """FastAPI TestClient with a fresh app instance."""
    from api.server import create_app
    app = create_app(registry=None)
    with TestClient(app) as c:
        yield c


# ─────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────

STORE_ID = "STORE_TEST_001"


def _make_event(
    event_type:  str = "ENTRY",
    visitor_id:  str | None = None,
    zone_id:     str | None = None,
    dwell_ms:    int = 0,
    is_staff:    bool = False,
    queue_depth: int | None = None,
) -> Dict[str, Any]:
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    vid = visitor_id or f"VIS_{uuid.uuid4().hex[:8]}"
    meta: Dict[str, Any] = {}
    if queue_depth is not None:
        meta["queue_depth"] = queue_depth
    return {
        "event_id":   str(uuid.uuid4()),
        "store_id":   STORE_ID,
        "camera_id":  "CAM_01",
        "visitor_id": vid,
        "event_type": event_type,
        "timestamp":  ts,
        "zone_id":    zone_id,
        "dwell_ms":   dwell_ms,
        "is_staff":   is_staff,
        "confidence": 0.95,
        "metadata":   meta,
    }


def _ingest(client, events: List[Dict]) -> Dict:
    r = client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200, r.text
    return r.json()


# ─────────────────────────────────────────────────────────────────────
#  POST /events/ingest
# ─────────────────────────────────────────────────────────────────────

class TestIngest:

    def test_valid_events_accepted(self, client):
        events = [_make_event("ENTRY") for _ in range(5)]
        result = _ingest(client, events)
        assert result["accepted"] == 5
        assert result["rejected"] == 0
        assert result["duplicate"] == 0

    def test_idempotency_same_payload_twice(self, client):
        events = [_make_event("ENTRY") for _ in range(3)]
        r1 = _ingest(client, events)
        r2 = _ingest(client, events)
        assert r1["accepted"] == 3
        # Second call: all duplicates
        assert r2["duplicate"] == 3
        assert r2["accepted"] == 0

    def test_invalid_event_type_rejected(self, client):
        bad = _make_event("ENTRY")
        bad["event_type"] = "INVALID_TYPE"
        result = _ingest(client, [bad])
        assert result["rejected"] == 1
        assert result["accepted"] == 0
        assert len(result["errors"]) == 1

    def test_missing_zone_id_for_zone_event_rejected(self, client):
        bad = _make_event("ZONE_ENTER")   # zone_id is None
        bad["zone_id"] = None
        result = _ingest(client, [bad])
        assert result["rejected"] == 1

    def test_partial_success_mix_valid_invalid(self, client):
        good = _make_event("ENTRY")
        bad  = _make_event("ENTRY")
        bad["event_type"] = "NOT_REAL"
        result = _ingest(client, [good, bad])
        assert result["accepted"] == 1
        assert result["rejected"] == 1

    def test_batch_size_limit(self, client):
        events = [_make_event("ENTRY") for _ in range(501)]
        r = client.post("/events/ingest", json={"events": events})
        assert r.status_code == 422   # Pydantic validation error


# ─────────────────────────────────────────────────────────────────────
#  GET /stores/{id}/metrics
# ─────────────────────────────────────────────────────────────────────

class TestStoreMetrics:

    def test_empty_store_returns_zero_metrics(self, client):
        r = client.get(f"/stores/EMPTY_STORE/metrics")
        assert r.status_code == 200
        data = r.json()
        assert data["unique_visitors"] == 0
        assert data["conversion_rate"] == 0.0
        assert data["abandonment_rate"] == 0.0

    def test_metrics_after_ingest(self, client):
        vid = "VIS_ALICE"
        events = [
            _make_event("ENTRY",              visitor_id=vid),
            _make_event("ZONE_ENTER",         visitor_id=vid, zone_id="SKINCARE"),
            _make_event("ZONE_DWELL",         visitor_id=vid, zone_id="SKINCARE", dwell_ms=30000),
            _make_event("BILLING_QUEUE_JOIN", visitor_id=vid, zone_id="BILLING",  queue_depth=2),
        ]
        _ingest(client, events)
        r = client.get(f"/stores/{STORE_ID}/metrics")
        data = r.json()
        assert data["unique_visitors"] >= 1
        assert data["conversion_rate"] > 0.0
        assert "SKINCARE" in data["avg_dwell_per_zone"]

    def test_staff_excluded_from_metrics(self, client):
        _ingest(client, [_make_event("ENTRY", is_staff=True)])
        r = client.get(f"/stores/{STORE_ID}/metrics")
        data = r.json()
        assert data["unique_visitors"] == 0

    def test_abandonment_rate_computed(self, client):
        vid = "VIS_ABANDONER"
        events = [
            _make_event("ENTRY",                    visitor_id=vid),
            _make_event("BILLING_QUEUE_JOIN",       visitor_id=vid, zone_id="BILLING", queue_depth=1),
            _make_event("BILLING_QUEUE_ABANDON",    visitor_id=vid, zone_id="BILLING"),
        ]
        _ingest(client, events)
        r = client.get(f"/stores/{STORE_ID}/metrics")
        data = r.json()
        assert data["abandonment_rate"] == 1.0


# ─────────────────────────────────────────────────────────────────────
#  GET /stores/{id}/funnel
# ─────────────────────────────────────────────────────────────────────

class TestFunnel:

    def _seed_funnel(self, client):
        """Ingest a minimal funnel journey for one visitor."""
        vid = "VIS_FUNNEL"
        _ingest(client, [
            _make_event("ENTRY",              visitor_id=vid),
            _make_event("ZONE_ENTER",         visitor_id=vid, zone_id="SKINCARE"),
            _make_event("BILLING_QUEUE_JOIN", visitor_id=vid, zone_id="BILLING", queue_depth=1),
        ])
        return vid

    def test_funnel_has_four_stages(self, client):
        self._seed_funnel(client)
        r = client.get(f"/stores/{STORE_ID}/funnel")
        assert r.status_code == 200
        stages = r.json()["stages"]
        assert len(stages) == 4
        stage_names = [s["stage"] for s in stages]
        assert stage_names == ["entry", "zone_visit", "billing_queue", "purchase"]

    def test_funnel_stages_monotonically_non_increasing(self, client):
        self._seed_funnel(client)
        stages = client.get(f"/stores/{STORE_ID}/funnel").json()["stages"]
        visitors = [s["visitors"] for s in stages]
        for i in range(1, len(visitors)):
            assert visitors[i] <= visitors[i - 1], \
                f"Stage {stages[i]['stage']} has more visitors than previous stage"

    def test_funnel_drop_off_non_negative(self, client):
        self._seed_funnel(client)
        stages = client.get(f"/stores/{STORE_ID}/funnel").json()["stages"]
        for stage in stages:
            assert stage["drop_off_pct"] >= 0.0

    def test_reentry_not_double_counted(self, client):
        vid = "VIS_REENTRY"
        _ingest(client, [
            _make_event("ENTRY",   visitor_id=vid),
            _make_event("EXIT",    visitor_id=vid),
            _make_event("REENTRY", visitor_id=vid),
        ])
        stages = client.get(f"/stores/{STORE_ID}/funnel").json()["stages"]
        entry_stage = next(s for s in stages if s["stage"] == "entry")
        # Should be 1, not 2 (REENTRY counted in entry stage but visitor_id is same)
        assert entry_stage["visitors"] == 1


# ─────────────────────────────────────────────────────────────────────
#  GET /stores/{id}/heatmap
# ─────────────────────────────────────────────────────────────────────

class TestHeatmap:

    def test_empty_store_returns_no_zones(self, client):
        r = client.get(f"/stores/EMPTY_HEATMAP/heatmap")
        assert r.status_code == 200
        data = r.json()
        assert data["zones"] == []
        assert data["data_confidence"] is False

    def test_zones_populated_after_ingest(self, client):
        events = []
        for i in range(5):
            vid = f"VIS_{i}"
            events += [
                _make_event("ZONE_ENTER", visitor_id=vid, zone_id="SKINCARE"),
                _make_event("ZONE_DWELL", visitor_id=vid, zone_id="SKINCARE", dwell_ms=20000),
                _make_event("ZONE_ENTER", visitor_id=vid, zone_id="HAIRCARE"),
            ]
        _ingest(client, events)
        r = client.get(f"/stores/{STORE_ID}/heatmap")
        zones = r.json()["zones"]
        zone_ids = {z["zone_id"] for z in zones}
        assert "SKINCARE" in zone_ids
        assert "HAIRCARE" in zone_ids

    def test_score_is_100_for_most_visited(self, client):
        _ingest(client, [
            _make_event("ZONE_ENTER", visitor_id="VIS_A", zone_id="HOT_ZONE"),
            _make_event("ZONE_ENTER", visitor_id="VIS_B", zone_id="HOT_ZONE"),
            _make_event("ZONE_ENTER", visitor_id="VIS_C", zone_id="COLD_ZONE"),
        ])
        zones = client.get(f"/stores/{STORE_ID}/heatmap").json()["zones"]
        max_score = max(z["score"] for z in zones)
        assert max_score == 100.0

    def test_score_range_0_to_100(self, client):
        _ingest(client, [_make_event("ZONE_ENTER", zone_id="Z1")])
        zones = client.get(f"/stores/{STORE_ID}/heatmap").json()["zones"]
        for zone in zones:
            assert 0.0 <= zone["score"] <= 100.0


# ─────────────────────────────────────────────────────────────────────
#  GET /stores/{id}/anomalies
# ─────────────────────────────────────────────────────────────────────

class TestAnomalies:

    def test_no_anomalies_on_empty_store(self, client):
        r = client.get(f"/stores/EMPTY_ANOMALIES/anomalies")
        assert r.status_code == 200
        assert r.json()["anomalies"] == []

    def test_queue_spike_detected(self, client):
        # Ingest a BILLING_QUEUE_JOIN with queue_depth > threshold (5)
        _ingest(client, [
            _make_event("BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=8),
        ])
        r = client.get(f"/stores/{STORE_ID}/anomalies")
        anomalies = r.json()["anomalies"]
        types = [a["anomaly_type"] for a in anomalies]
        assert "BILLING_QUEUE_SPIKE" in types

    def test_anomaly_has_required_fields(self, client):
        _ingest(client, [
            _make_event("BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=10),
        ])
        anomalies = client.get(f"/stores/{STORE_ID}/anomalies").json()["anomalies"]
        for anomaly in anomalies:
            assert "anomaly_type"    in anomaly
            assert "severity"        in anomaly
            assert "detail"          in anomaly
            assert "suggested_action" in anomaly
            assert "detected_at"     in anomaly

    def test_no_queue_spike_below_threshold(self, client):
        _ingest(client, [
            _make_event("BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=3),
        ])
        anomalies = client.get(f"/stores/{STORE_ID}/anomalies").json()["anomalies"]
        types = [a["anomaly_type"] for a in anomalies]
        assert "BILLING_QUEUE_SPIKE" not in types


# ─────────────────────────────────────────────────────────────────────
#  GET /health
# ─────────────────────────────────────────────────────────────────────

class TestHealth:

    def test_health_returns_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"

    def test_health_includes_version(self, client):
        data = client.get("/health").json()
        assert "version" in data

    def test_health_stores_key_present_after_ingest(self, client):
        _ingest(client, [_make_event("ENTRY")])
        data = client.get("/health").json()
        assert STORE_ID in data.get("stores", {})

    def test_health_uptime_positive(self, client):
        data = client.get("/health").json()
        assert data["uptime_seconds"] >= 0
