from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI

from server.battery_safety import BatterySafetyMonitor
from server.patrol_route_api import PatrolRouteApi


class _NavigationStub:
    def __init__(self):
        self.state = {
            "navigation_mode": "DRIVING",
            "robot_mode": "auto",
            "active_map": {"name": "floor1"},
            "emergency_stop": False,
            "navigation_state": "READY",
            "ros_navigation": {"state": "READY"},
        }

    def state_response(self):
        return dict(self.state)


def test_patrol_route_normalization_and_persistence(tmp_path):
    app = FastAPI()
    api = PatrolRouteApi(
        app,
        _NavigationStub(),
        lambda _request: None,
        tmp_path / "patrol.json",
        max_retries=2,
    )

    route = api._normalize_route(
        {
            "name": "night",
            "map_name": "floor1",
            "loop": True,
            "waypoints": [
                {"x": 1, "y": 2, "yaw": 0, "wait_sec": 3},
                {"x": 3, "y": 4, "yaw": 1.57, "wait_sec": 2},
            ],
        }
    )
    api._routes[route["route_id"]] = route
    api._save()

    restored = PatrolRouteApi(
        FastAPI(),
        _NavigationStub(),
        lambda _request: None,
        tmp_path / "patrol.json",
        max_retries=2,
    )
    assert restored._routes[route["route_id"]]["name"] == "night"
    assert len(restored._routes[route["route_id"]]["waypoints"]) == 2


def test_low_battery_monitor_triggers_on_pi_undervoltage():
    calls = []

    async def stop(reason, automatic=False):
        calls.append((reason, automatic))
        return {"ok": True}

    async def scenario():
        monitor = BatterySafetyMonitor(stop, low_samples=3)
        state = monitor.note_status({"power_undervoltage": True})
        await asyncio.sleep(0)
        assert state["battery_low"] is True
        assert state["power_undervoltage"] is True

    asyncio.run(scenario())
    assert calls == [("LOW_BATTERY", True)]


def test_low_battery_unknown_percent_is_not_fabricated():
    async def stop(_reason, _automatic=False):
        return {"ok": True}

    monitor = BatterySafetyMonitor(stop)
    state = monitor.note_status({"battery_percent": None, "power_undervoltage": False})
    assert state["battery_percent"] is None
    assert state["battery_low"] is False


def test_final_dashboard_components_are_mounted():
    root = Path(__file__).resolve().parents[1]
    html = (root / "frontend/templates/index.html").read_text(encoding="utf-8")
    patrol = (root / "frontend/components/navigation/patrol_control.js").read_text(encoding="utf-8")
    alerts = (root / "frontend/components/alerts/realtime_alerts.js").read_text(encoding="utf-8")

    assert "patrol_control.js" in html
    assert "realtime_alerts.js" in html
    assert "WAYPOINT" in patrol
    assert "/api/navigation/patrol/start" in patrol
    assert "dabom:patrol-waypoints-changed" in patrol
    assert "/api/latest_result" in alerts
    assert "AI 위험 감지" in alerts


def test_final_hardware_contracts_are_real_not_stubs():
    root = Path(__file__).resolve().parents[1]
    speaker = (root / "raspberry/controllers/speaker_controller.py").read_text(encoding="utf-8")
    pico = (root / "raspberry/pico_w_sdk/main.c").read_text(encoding="utf-8")
    motor = (root / "raspberry/controllers/motor_controller.py").read_text(encoding="utf-8")

    assert "espeak-ng" in speaker
    assert "aplay" in speaker
    assert 'print(f"[speaker]' not in speaker
    assert "#define SPEAKER_PIN 16" in pico
    assert "#define MOSFET_PIN 20" in pico
    assert '"LED,{1 if enabled else 0}"' in motor
