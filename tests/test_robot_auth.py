from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from unittest.mock import AsyncMock, Mock, patch

import server.app as server
from server.app import app


client = TestClient(app)
STATUS_PAYLOAD = {
    "robot_id": "pi-01",
    "battery": "88",
    "battery_level": 87,
    "mode": "manual",
    "navigation_mode": "mapping",
}


def test_status_ingest_requires_robot_control_header():
    assert client.post("/status", json=STATUS_PAYLOAD).status_code == 401
    assert (
        client.post(
            "/status",
            json=STATUS_PAYLOAD,
            headers={"X-Robot-Control-Token": "wrong-token"},
        ).status_code
        == 401
    )

    response = client.post(
        "/status",
        json=STATUS_PAYLOAD,
        headers={"X-Robot-Control-Token": "test-robot-token"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    exposed = client.get("/get_status").json()
    assert "battery" not in exposed
    assert "battery_level" not in exposed


def test_robot_websocket_requires_matching_header_token():
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect("/ws/robot/pi-01"):
            pass
    assert denied.value.code == 1008

    with client.websocket_connect(
        "/ws/robot/pi-01",
        headers={"X-Robot-Control-Token": "test-robot-token"},
    ) as websocket:
        websocket.send_json({"type": "status", "data": STATUS_PAYLOAD})
    exposed = client.get("/get_status").json()
    assert "battery" not in exposed
    assert "battery_level" not in exposed


def test_robot_command_requires_robot_header_or_user_csrf():
    payload = {"type": "stop"}
    assert client.post("/api/robots/pi-01/command", json=payload).status_code == 401
    assert (
        client.post(
            "/api/robots/pi-01/command",
            json=payload,
            headers={"X-Robot-Control-Token": "wrong-token"},
        ).status_code
        == 401
    )

    with patch.object(server.connections, "send_command", AsyncMock(return_value=True)):
        response = client.post(
            "/api/robots/pi-01/command",
            json=payload,
            headers={"X-Robot-Control-Token": "test-robot-token"},
        )
    assert response.status_code == 200
    assert response.json()["delivered"] is True


def test_robot_command_allows_authenticated_user_with_csrf():
    browser = TestClient(app)
    csrf = browser.get("/api/auth/csrf").json()["csrf_token"]
    user = {"user_id": 1, "email": "browser@example.com", "name": "Browser"}
    with patch.object(server, "call_auth", AsyncMock(return_value=(user, None))):
        login = browser.post(
            "/api/auth/login",
            headers={"X-CSRF-Token": csrf},
            json={"login_id": "browser@example.com", "password": "test-password"},
        )
    assert login.status_code == 200

    csrf = browser.get("/api/auth/csrf").json()["csrf_token"]
    with patch.object(server.connections, "send_command", AsyncMock(return_value=True)):
        response = browser.post(
            "/api/robots/pi-01/command",
            headers={"X-CSRF-Token": csrf},
            json={"type": "stop"},
        )
    assert response.status_code == 200
    assert response.json()["delivered"] is True


def test_lidar_websocket_requires_matching_header_token(monkeypatch):
    bridge = Mock()
    monkeypatch.setattr(server, "lidar_ros_bridge", bridge)

    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect("/ws/sensors/pi-01/lidar"):
            pass
    assert denied.value.code == 1008

    with pytest.raises(WebSocketDisconnect) as wrong:
        with client.websocket_connect(
            "/ws/sensors/pi-01/lidar",
            headers={"X-Robot-Control-Token": "wrong-token"},
        ):
            pass
    assert wrong.value.code == 1008

    with client.websocket_connect(
        "/ws/sensors/pi-01/lidar",
        headers={"X-Robot-Control-Token": "test-robot-token"},
    ) as websocket:
        websocket.send_json(
            {"type": "sensor_hello", "sensor": "lidar", "robot_id": "pi-01"}
        )

    bridge.mark_connected.assert_called_once_with("pi-01")
    bridge.mark_disconnected.assert_called_once_with("pi-01")


def test_lidar_sender_does_not_log_token_bearing_exception_text():
    source = (
        Path(__file__).resolve().parents[1] / "raspberry" / "lidar_scan_sender.py"
    ).read_text(encoding="utf-8")
    assert "WebSocket thread terminated: {exc}" not in source
    assert "LiDAR WebSocket disconnected: {exc}" not in source
    assert source.count("{type(exc).__name__}") >= 2
