"""Regression contracts for Pi/GPU cross-host LiDAR time alignment."""
from pathlib import Path

import pytest

from raspberry.clock_sync import probe_gpu_clock

ROOT = Path(__file__).resolve().parents[1]


class StubResponse:
    def __init__(self, timestamp, status=None):
        self.timestamp = timestamp
        self.status = status

    def raise_for_status(self):
        if self.status:
            from requests import HTTPError
            raise HTTPError(self.status)

    def json(self):
        return {"server_time_sec": self.timestamp}


def stub_clock(times):
    it = iter(times)
    return lambda: next(it)


def test_clock_probe_estimates_gpu_minus_pi_without_mutating_host_clock():
    recorded = []

    def fake_get(url, *, headers, timeout):
        recorded.append((url, headers, timeout))
        return StubResponse(110.10)

    sample = probe_gpu_clock(
        "http://gpu.local:21063/",
        "test-token",
        attempts=1,
        request_get=fake_get,
        wall_clock=stub_clock([100.0, 100.2]),
        steady_clock=stub_clock([10.0, 10.2]),
    )
    assert sample.offset_sec == pytest.approx(10.0)
    assert sample.rtt_sec == pytest.approx(0.2)
    assert recorded == [
        ("http://gpu.local:21063/api/time/sync",
         {"X-Robot-Control-Token": "test-token"}, 0.65)
    ]


def test_clock_probe_rejects_network_jitter_and_clock_step():
    sequence = iter([StubResponse(100.10), StubResponse(100.10)])

    def fake_get(*_args, **_kwargs):
        return next(sequence)

    with pytest.raises(RuntimeError, match="probe unavailable"):
        probe_gpu_clock(
            "http://gpu.local", "secret", attempts=2, request_get=fake_get,
            wall_clock=stub_clock([100.0, 100.8, 100.9, 101.5]),
            steady_clock=stub_clock([10.0, 10.8, 11.0, 11.1]),
        )


def test_clock_probe_chooses_lowest_rtt():
    responses = iter([StubResponse(101.20), StubResponse(102.05)])

    def fake_get(*_args, **_kwargs):
        return next(responses)

    sample = probe_gpu_clock(
        "http://gpu", "token", attempts=2, request_get=fake_get,
        wall_clock=stub_clock([100.0, 100.4, 101.0, 101.1]),
        steady_clock=stub_clock([10.0, 10.4, 11.0, 11.1]),
    )
    # First sample RTT 0.4 is rejected; second gives 102.05 - 101.05 = +1s.
    assert sample.offset_sec == pytest.approx(1.0)


def test_clock_sync_integration_contracts():
    app = (ROOT / "server/app.py").read_text(encoding="utf-8")
    sender = (ROOT / "raspberry/lidar_scan_sender.py").read_text(encoding="utf-8")
    bridge = (ROOT / "server/lidar_ros_bridge.py").read_text(encoding="utf-8")
    assert '@app.get("/api/time/sync")' in app
    assert 'denied = robot_ingest_failure(request)' in app
    assert '"server_time_sec": time.time()' in app
    assert 'probe_gpu_clock(self.server_base_url, self.control_token)' in sender
    assert '"clock_sync_valid": valid_sync' in sender
    assert "offset_sec * 1_000_000_000" in sender
    assert 'header.get("clock_sync_valid") is True' in bridge
    assert "message.header.stamp = received_stamp" in bridge


def test_existing_pipeline_and_nav_safety_paths_remain():
    assert (ROOT / "perception/models/detector_rtmo.py").exists()
    assert (ROOT / "navigation/ros/patrol_navigation/config/nav2_params.yaml").exists()

def test_time_sync_endpoint_requires_robot_token():
    from fastapi.testclient import TestClient
    from server.app import app
    import time

    client = TestClient(app)
    assert client.get("/api/time/sync").status_code == 401
    assert client.get(
        "/api/time/sync",
        headers={"X-Robot-Control-Token": "invalid"},
    ).status_code == 401
    response = client.get(
        "/api/time/sync",
        headers={"X-Robot-Control-Token": "test-robot-token"},
    )
    assert response.status_code == 200
    assert abs(response.json()["server_time_sec"] - time.time()) < 3.0


def test_sender_invalidates_cached_offset_on_clock_jump():
    sender = (ROOT / "raspberry/lidar_scan_sender.py").read_text(encoding="utf-8")
    assert "self.clock_wall_minus_monotonic" in sender
    assert "wall_now - mono_now - self.clock_wall_minus_monotonic" in sender
    assert "and mono_now - self.clock_sample_monotonic <= 45.0" in sender
