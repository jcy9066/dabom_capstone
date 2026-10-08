from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import importlib.util
import importlib
import asyncio
import os
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "frontend/services/static/system_control.js").read_text(encoding="utf-8")
STYLE = (ROOT / "frontend/services/static/system_control.css").read_text(encoding="utf-8")
BACKEND = (ROOT / "frontend/system_control.py").read_text(encoding="utf-8")


def attach_system_control_routes(app):
    spec = importlib.util.spec_from_file_location("system_control_under_test", ROOT / "frontend/system_control.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.attach_system_control_routes(app)


def test_generic_dashboard_command_cannot_release_navigation_estop():
    app = FastAPI()

    @app.middleware("http")
    async def test_session(request, call_next):
        request.scope["session"] = {"user": {"user_id": "tester"}}
        return await call_next(request)

    sender = AsyncMock(return_value=True)
    server = SimpleNamespace(
        csrf_failure=lambda _request: None,
        connections=SimpleNamespace(send_command_wait_ack=sender),
        SERVER_ROBOT_ID="pi-01",
    )
    with patch.dict(sys.modules, {"server.app": server}):
        attach_system_control_routes(app)
        client = TestClient(app)
        for command_type in ("resume_safety_check", "resume_navigation"):
            response = client.post(
                "/api/dashboard-control/command",
                json={"type": command_type},
                headers={"X-Dashboard-Client-Id": "test-client"},
            )
            assert response.status_code == 409
            assert response.json()["error_code"] == "NAVIGATION_RESUME_REQUIRED"
    sender.assert_not_awaited()


def test_generic_robot_command_cannot_release_navigation_estop():
    with patch.dict(os.environ, {
        "DASHBOARD_ESTOP_COOLDOWN_SEC": "1",
        "DASHBOARD_GOAL_REACHED_TOLERANCE_M": "0.25",
    }):
        server_module = importlib.import_module("server.app")

    with patch.object(server_module, "robot_ingest_failure", return_value=None):
        client = TestClient(server_module.app)
        for command_type in ("resume_safety_check", "resume_navigation"):
            response = client.post(
                "/api/robots/pi-01/command",
                json={"type": command_type},
            )
            assert response.status_code == 409


def test_first_direct_motion_after_resume_requires_pi_ack():
    import time
    with patch.dict(os.environ, {
        "DASHBOARD_ESTOP_COOLDOWN_SEC": "1",
        "DASHBOARD_GOAL_REACHED_TOLERANCE_M": "0.25",
    }):
        server_module = importlib.import_module("server.app")
    api = server_module.navigation_control_api
    with api._lock:
        pi_safety_saved = (
            api._pi_estop_latched,
            api._pi_safety_session,
            api._pi_safety_epoch,
        )
        api._pi_estop_latched = False
        api._pi_safety_session = "test-pi"
        api._pi_safety_epoch = 1
        saved = (
            api._state["emergency_stop"], api._encoder_stop_violation,
            api._stop_commanded_at, api._stop_encoder_ticks,
        )
        safety_saved = (
            api._post_resume_motion_ready,
            api._connected,
            api._pi_updated_at,
            api._stop_confirmation_after_sequence,
        )
        # Simulate an already verified Resume handshake.
        api._post_resume_motion_ready = True
        api._connected = True
        api._pi_updated_at = time.time()
        api._stop_confirmation_after_sequence = None
        api._state["emergency_stop"] = False
        api._encoder_stop_violation = False
        api._stop_commanded_at = 1.0
        api._stop_encoder_ticks = (0, 0, 0, 0)
    try:
        with patch.object(server_module, "robot_ingest_failure", return_value=None), patch.object(
            server_module.connections, "send_command_wait_ack", new_callable=AsyncMock
        ) as ack_sender, patch.object(
            server_module.connections, "send_command", new_callable=AsyncMock
        ) as plain_sender:
            client = TestClient(server_module.app)
            ack_sender.return_value = False
            denied = client.post("/api/robots/pi-01/command", json={"type": "move", "direction": "forward"})
            assert denied.status_code == 409
            assert api._stop_commanded_at is not None
            plain_sender.assert_not_awaited()

            ack_sender.return_value = True
            accepted = client.post("/api/robots/pi-01/command", json={"type": "move", "direction": "forward"})
            assert accepted.status_code == 200
            assert api._stop_commanded_at is None
            assert ack_sender.await_count == 2
    finally:
        with api._lock:
            (
                api._pi_estop_latched,
                api._pi_safety_session,
                api._pi_safety_epoch,
            ) = pi_safety_saved
            (api._state["emergency_stop"], api._encoder_stop_violation,
             api._stop_commanded_at, api._stop_encoder_ticks) = saved
            (
                api._post_resume_motion_ready,
                api._connected,
                api._pi_updated_at,
                api._stop_confirmation_after_sequence,
            ) = safety_saved


def test_estop_during_direct_motion_ack_must_fail():
    import time

    with patch.dict(os.environ, {
        "DASHBOARD_ESTOP_COOLDOWN_SEC": "1",
        "DASHBOARD_GOAL_REACHED_TOLERANCE_M": "0.25",
    }):
        server = importlib.import_module("server.app")
    api = server.navigation_control_api

    with api._lock:
        pi_safety_saved = (
            api._pi_estop_latched,
            api._pi_safety_session,
            api._pi_safety_epoch,
        )
        api._pi_estop_latched = False
        api._pi_safety_session = "test-pi"
        api._pi_safety_epoch = 1
        saved = (
            api._state["emergency_stop"],
            api._encoder_stop_violation,
            api._stop_commanded_at,
            api._stop_encoder_ticks,
            api._post_resume_motion_ready,
            api._connected,
            api._pi_updated_at,
            api._estop_generation,
        )

        api._state["emergency_stop"] = False
        api._encoder_stop_violation = False
        api._stop_commanded_at = time.time()
        api._stop_encoder_ticks = (0, 0, 0, 0)
        api._post_resume_motion_ready = True
        api._connected = True
        api._pi_updated_at = time.time()

    async def ack_after_estop(robot_id, command):
        with api._lock:
            api._state["emergency_stop"] = True
            api._post_resume_motion_ready = False
            api._estop_generation += 1
        return True

    try:
        with (
            patch.object(server, "robot_ingest_failure", return_value=None),
            patch.object(
                server.connections,
                "send_command_wait_ack",
                side_effect=ack_after_estop,
            ) as ack_sender,
            patch.object(
                server.connections,
                "send_command",
                new_callable=AsyncMock,
            ) as plain_sender,
        ):
            client = TestClient(server.app)
            response = client.post(
                "/api/robots/pi-01/command",
                json={"type": "move", "direction": "forward"},
            )

            assert ack_sender.await_count == 1
            plain_sender.assert_not_awaited()
            assert response.status_code == 409
            assert api._stop_commanded_at is not None

    finally:
        with api._lock:
            (
                api._pi_estop_latched,
                api._pi_safety_session,
                api._pi_safety_epoch,
            ) = pi_safety_saved
            (
                api._state["emergency_stop"],
                api._encoder_stop_violation,
                api._stop_commanded_at,
                api._stop_encoder_ticks,
                api._post_resume_motion_ready,
                api._connected,
                api._pi_updated_at,
                api._estop_generation,
            ) = saved


def test_resume_ack_requires_matching_pi_safety_epoch():
    with patch.dict(os.environ, {
        "DASHBOARD_ESTOP_COOLDOWN_SEC": "1",
        "DASHBOARD_GOAL_REACHED_TOLERANCE_M": "0.25",
    }):
        server_module = importlib.import_module("server.app")

    async def check():
        manager = server_module.RobotConnectionManager()

        class FakeSocket:
            ack_epoch = 0

            async def send_json(self, command):
                await manager.receive_ack("pi-01", {
                    "command_id": command["command_id"],
                    "ok": True,
                    "emergency_stop": False,
                    "safety_session": "test-pi",
                    "safety_epoch": self.ack_epoch,
                })

        socket = FakeSocket()
        manager.active["pi-01"] = socket
        command = {
            "type": "resume_navigation",
            "safety_session": "test-pi",
            "safety_epoch": 1,
        }
        assert await manager.send_command_wait_ack("pi-01", command) is False
        socket.ack_epoch = 1
        assert await manager.send_command_wait_ack("pi-01", command) is True

    asyncio.run(check())


def test_settings_guide_uses_dashboard_actions_and_modal_manager():
    for label in (
        r"\ub300\uc2dc\ubcf4\ub4dc \uc0ac\uc6a9 \uc548\ub0b4",
        r"[Mapping] \uc120\ud0dd",
        r"[Driving] \uc120\ud0dd",
        r"[\uc21c\ucc30 \uae30\ub85d \uc870\ud68c]",
        r"[\uc774\ubbf8\uc9c0 \uac24\ub7ec\ub9ac]",
        r"[\ud604\uc7ac \uc0c1\ud669 \uc791\uc131]",
        r"[\uad00\ub9ac\uc790 \uc870\uce58 \uc870\ud68c]",
        r"[\uae30\uae30 \uc0c1\ud0dc \uc870\ud68c]",
    ):
        assert label in SCRIPT
    assert "DabomDashboardComponents?.modal" in SCRIPT
    assert "processGuideItems()" in SCRIPT
    assert "state.status.gpu" in SCRIPT
    assert "state.status.pi" in SCRIPT
    assert "component.id !== 'map_bridge'" in SCRIPT
    assert r"SLAM Mapping\uc740 ${label}\ub97c \ud568\uaed8 \uc2dc\uc791" in SCRIPT


def test_disconnected_status_does_not_become_process_guide_source():
    assert "function render(status, available = true)" in SCRIPT
    assert "state.status = available ? status : null" in SCRIPT
    assert "}] }, false);" in SCRIPT
    assert "if (!state.status) return [text.processUnavailable]" in SCRIPT


def test_backend_component_descriptions_match_frontend_contract():
    for description in (
        "Pi\uc758 LiDAR \ub370\uc774\ud130\ub97c ROS 2 /scan\uc73c\ub85c \uc804\ub2ec",
        "Pi\uc758 \uc5d4\ucf54\ub354 \ub370\uc774\ud130\ub97c ROS 2 /wheel_ticks\ub85c \uc804\ub2ec",
        "root GPU supervisor\uac00 \uad00\ub9ac\ud558\ub294 \uc5d4\ucf54\ub354 \uae30\ubc18 odometry",
        "LiDAR \uae30\ubc18 \uc9c0\ub3c4 \uc791\uc131\uacfc Map Bridge \ud568\uaed8 \uc2e4\ud589",
        "Mapping/Driving launch\uac00 \uc18c\uc720\ud558\ub294 \uc9c0\ub3c4\u00b7\uc704\uce58 \uc804\uc1a1 \ub178\ub4dc",
        "Pi\uc5d0\uc11c LiDAR \uc2a4\uce94 \ub370\uc774\ud130\ub97c \uc218\uc9d1",
    ):
        assert description in BACKEND


def test_pending_and_unavailable_cursors_are_distinct():
    assert "is-pending" in SCRIPT
    assert "is-unavailable" in SCRIPT
    assert ".is-pending:disabled" in STYLE
    assert "cursor: progress" in STYLE
    assert ".is-unavailable:disabled" in STYLE
    assert "cursor: not-allowed" in STYLE
    assert "system-control-normalize:disabled { cursor: wait" not in STYLE
