"""Independent audio relay contract tests; does not import GPU/ROS app."""
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware
from starlette.websockets import WebSocketDisconnect

from server.audio_intercom import register_audio_intercom


def create_app(root: Path) -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    @app.get("/test/login")
    def login(request: Request):
        request.session["user"] = {"id": "admin"}
        return {"ok": True}

    (root / "frontend/templates").mkdir(parents=True)
    (root / "frontend/templates/audio_intercom_test.html").write_text('robot=__DABOM_ROBOT_ID__')
    register_audio_intercom(app, robot_id="pi-01", robot_token="secret-token", root_dir=root)
    return app


def test_auth_guards():
    with TemporaryDirectory() as path:
        with TestClient(create_app(Path(path))) as c:
            assert c.get("/api/audio/status").status_code == 401
            assert c.get("/audio/intercom-test").status_code == 401
            try:
                with c.websocket_connect("/ws/audio/dashboard/pi-01", headers={"origin": "http://testserver"}):
                    assert False, "unauthenticated dashboard was accepted"
            except WebSocketDisconnect as exc:
                assert exc.code == 1008
            try:
                with c.websocket_connect("/ws/audio/robot/pi-01"):
                    assert False, "unauthenticated robot was accepted"
            except WebSocketDisconnect as exc:
                assert exc.code == 1008


def test_authenticated_pcm_relay_and_consent():
    with TemporaryDirectory() as path:
        with TestClient(create_app(Path(path))) as c:
            assert c.get("/test/login").status_code == 200
            assert c.get("/api/audio/status").json()["robot_connected"] is False
            assert c.get("/audio/intercom-test").status_code == 200
            with c.websocket_connect("/ws/audio/dashboard/pi-01", headers={"origin": "http://testserver"}) as browser:
                state = browser.receive_json()
                assert state["robot_connected"] is False
                with c.websocket_connect("/ws/audio/robot/pi-01", headers={"X-Robot-Control-Token": "secret-token"}) as robot:
                    assert robot.receive_json()["type"] == "state"
                    assert robot.receive_json() == {"type": "listen", "enabled": False}
                    assert robot.receive_json() == {"type": "talk", "enabled": False}
                    assert robot.receive_json() == {"type": "get_speaker_volume"}
                    assert browser.receive_json()["robot_connected"] is True
                    browser.send_json({"type": "listen", "enabled": True})
                    assert robot.receive_json() == {"type": "listen", "enabled": True}
                    assert browser.receive_json()["listening"] is True
                    sent = b"\x01\x02" * 320
                    robot.send_bytes(sent)
                    assert browser.receive_bytes() == sent
                    browser.send_json({"type": "talk", "enabled": True})
                    assert robot.receive_json() == {"type": "talk", "enabled": True}
                    assert browser.receive_json()["talking"] is True
                    browser.send_bytes(sent)
                    assert robot.receive_bytes() == sent
                    browser.send_json({"type": "talk", "enabled": False})
                    assert robot.receive_json() == {"type": "talk", "enabled": False}
                    assert browser.receive_json()["talking"] is False
                assert browser.receive_json()["robot_connected"] is False


def test_origin_rejected_even_when_logged_in():
    with TemporaryDirectory() as path:
        with TestClient(create_app(Path(path))) as c:
            c.get("/test/login")
            try:
                with c.websocket_connect("/ws/audio/dashboard/pi-01", headers={"origin": "https://untrusted.example"}):
                    assert False, "cross-origin dashboard audio was accepted"
            except WebSocketDisconnect as exc:
                assert exc.code == 1008


def test_duplicate_dashboard_connection_rejected():
    with TemporaryDirectory() as path:
        with TestClient(create_app(Path(path))) as c:
            c.get("/test/login")
            headers = {"origin": "http://testserver"}
            with c.websocket_connect("/ws/audio/dashboard/pi-01", headers=headers) as first:
                assert first.receive_json()["type"] == "state"
                try:
                    with c.websocket_connect("/ws/audio/dashboard/pi-01", headers=headers):
                        assert False, "second dashboard was accepted"
                except WebSocketDisconnect as exc:
                    assert exc.code == 1008


def test_actual_usb_speaker_volume_roundtrip_and_no_unverified_default():
    with TemporaryDirectory() as path:
        with TestClient(create_app(Path(path))) as c:
            c.get("/test/login")
            status = c.get("/api/audio/status").json()
            assert status["robot_id"] == "pi-01"
            assert status["speaker_volume_percent"] is None
            with c.websocket_connect("/ws/audio/dashboard/pi-01", headers={"origin": "http://testserver"}) as browser:
                assert browser.receive_json()["speaker_volume_percent"] is None
                with c.websocket_connect("/ws/audio/robot/pi-01", headers={"X-Robot-Control-Token": "secret-token"}) as robot:
                    assert robot.receive_json()["type"] == "state"
                    assert robot.receive_json() == {"type": "listen", "enabled": False}
                    assert robot.receive_json() == {"type": "talk", "enabled": False}
                    assert robot.receive_json() == {"type": "get_speaker_volume"}
                    assert browser.receive_json()["robot_connected"] is True
                    robot.send_json({"type": "speaker_volume", "percent": 50})
                    assert browser.receive_json() == {"type": "speaker_volume", "percent": 50}
                    assert c.get("/api/audio/status").json()["speaker_volume_percent"] == 50
                    browser.send_json({"type": "set_speaker_volume", "percent": 65})
                    assert robot.receive_json() == {"type": "set_speaker_volume", "percent": 65}
                    robot.send_json({"type": "speaker_volume", "percent": 64})
                    assert browser.receive_json() == {"type": "speaker_volume", "percent": 64}
                    assert c.get("/api/audio/status").json()["speaker_volume_percent"] == 64
                    browser.send_json({"type": "set_speaker_volume", "percent": -1})
                    error = browser.receive_json()
                    assert error["type"] == "error"
                    assert c.get("/api/audio/status").json()["speaker_volume_percent"] == 64
                assert browser.receive_json()["robot_connected"] is False
                assert c.get("/api/audio/status").json()["speaker_volume_percent"] is None


def test_robot_volume_report_rejects_malformed_values():
    with TemporaryDirectory() as path:
        with TestClient(create_app(Path(path))) as c:
            c.get("/test/login")
            with c.websocket_connect("/ws/audio/robot/pi-01", headers={"X-Robot-Control-Token": "secret-token"}) as robot:
                for _ in range(4):
                    robot.receive_json()
                robot.send_json({"type": "speaker_volume", "percent": "50"})
                robot.send_json({"type": "speaker_volume", "percent": 999})
                assert c.get("/api/audio/status").json()["speaker_volume_percent"] is None
                robot.send_json({"type": "speaker_volume", "percent": 50})
                # WebSocket receive loop is async; control message acts as a barrier.
                with c.websocket_connect("/ws/audio/dashboard/pi-01", headers={"origin": "http://testserver"}) as browser:
                    state = browser.receive_json()
                    assert state["robot_connected"] is True
                    assert state["speaker_volume_percent"] in (None, 50)
