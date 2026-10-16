"""ROS-independent regression of the Nav2 bridge HTTP and STOP worker.

Extract only the bridge class's pure methods with Python AST.  These tests
never import ROS2, connect to the server, or drive physical actuators.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = (
    ROOT / "navigation/ros/patrol_navigation/patrol_navigation"
    / "nav2_command_bridge.py"
)


def _bridge_methods(*names, responder=None):
    tree = ast.parse(BRIDGE.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                and n.name == "Nav2CommandBridge")
    methods = [n for n in node.body
               if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(methods) == len(names)
    isolated = ast.ClassDef(
        name="OfflineBridge", bases=[], keywords=[], body=methods,
        decorator_list=[],
    )
    module = ast.fix_missing_locations(
        ast.Module(body=[isolated], type_ignores=[]),
    )
    context = {
        "json": json, "time": time, "Request": Request,
        "urlopen": responder, "HTTPError": HTTPError,
        "URLError": URLError,
    }
    exec(compile(module, str(BRIDGE), "exec"), context)
    return context["OfflineBridge"]


def _fake_bridge(cls):
    bridge = cls()
    bridge.command_url = "http://127.0.0.1:21063/api/robots/test/command"
    bridge.control_token = "test-token"
    bridge.request_timeout_sec = 0.05
    bridge.motor_output_enabled = False
    bridge.sent_count = 0
    bridge.send_error_count = 0
    bridge.last_server_log_at = 0.0
    bridge.last_error_log_at = 0.0
    # The isolated worker has no ROS constructor, so initialize its
    # fail-closed STOP retry state explicitly.
    bridge._stop_command_in_flight = False
    bridge._stop_delivery_failed = False
    bridge._stop_retry_command = None
    bridge._stop_retry_due_at = None
    bridge.STOP_RETRY_INTERVAL_SEC = 0.50
    bridge.SHUTDOWN_STOP_MAX_ATTEMPTS = 3
    bridge.SHUTDOWN_STOP_RETRY_SEC = 0.10
    logged = []
    bridge.get_logger = lambda: SimpleNamespace(info=lambda msg: logged.append(msg))
    bridge._log_send_error = lambda msg: logged.append(msg)
    return bridge, logged


class _Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self):
        return self.data


def test_invalid_http_payload_is_caught_without_killing_worker():
    cls = _bridge_methods(
        "_post_command", responder=lambda *_args, **_kw: _Response(b'{"ok":true}'),
    )
    bridge, logs = _fake_bridge(cls)
    assert bridge._post_command({"type": "auto_drive", "left_mps": float("nan")}) is False
    assert bridge.send_error_count == 1
    assert bridge._post_command({"type": "stop", "reason": "invalid_input"}) is True
    assert bridge.sent_count == 1
    assert any("command failed:" in line for line in logs)


@pytest.mark.parametrize("payload", [
    b'[]', b'null', b'"ok"', b'{"ok":"true"}', b'{"ok":false}',
    b'not-json',
])
def test_malformed_response_is_rejected_without_worker_crash(payload):
    cls = _bridge_methods(
        "_post_command", responder=lambda *_args, **_kw: _Response(payload),
    )
    bridge, _ = _fake_bridge(cls)
    assert bridge._post_command({"type": "stop", "reason": "test"}) is False
    assert bridge.sent_count == 0
    assert bridge.send_error_count == 1


def test_http_worker_survives_unexpected_send_exception():
    cls = _bridge_methods("_command_worker")
    bridge, logs = _fake_bridge(cls)
    bridge._command_condition = threading.Condition()
    bridge._worker_shutdown = False
    bridge._pending_command = None
    done = threading.Event()
    attempts = []

    def simulate_send(payload):
        attempts.append(payload)
        if len(attempts) == 1:
            raise TypeError("unexpected serialization failure")
        done.set()
        return True

    bridge._post_command = simulate_send
    worker = threading.Thread(target=bridge._command_worker, daemon=True)
    worker.start()
    try:
        with bridge._command_condition:
            bridge._pending_command = {"type": "auto_drive"}
            bridge._command_condition.notify_all()
        limit = time.monotonic() + 2.0
        while len(attempts) < 1 and time.monotonic() < limit:
            time.sleep(0.005)
        assert len(attempts) == 1
        with bridge._command_condition:
            bridge._pending_command = {"type": "stop"}
            bridge._command_condition.notify_all()
        assert done.wait(2.0), "STOP was not processed after a prior exception"
        assert attempts[-1]["type"] == "stop"
        assert bridge.send_error_count == 1
        assert any("command worker failure" in entry for entry in logs)
    finally:
        with bridge._command_condition:
            bridge._worker_shutdown = True
            bridge._command_condition.notify_all()
        worker.join(timeout=2)
        assert not worker.is_alive()


def test_bridge_rejects_nonfinite_runtime_limits_before_worker_start():
    code = BRIDGE.read_text(encoding="utf-8")
    assert "not math.isfinite(self.wheel_track_m)" in code
    assert "not math.isfinite(self.twist_timeout_sec)" in code
    assert "not math.isfinite(request_timeout_sec)" in code
    assert code.index("not math.isfinite(request_timeout_sec)") < code.index(
        "self._command_worker_thread.start()"
    )


def test_actual_motor_stop_requires_explicit_delivery_ack():
    cls = _bridge_methods(
        "_post_command",
        responder=lambda *_args, **_kw: _Response(
            b'{"ok":true,"delivered":false}'
        ),
    )
    bridge, _ = _fake_bridge(cls)
    bridge.motor_output_enabled = True
    assert bridge._post_command({"type": "stop", "reason": "test"}) is False
    assert bridge.send_error_count == 1
    assert bridge.sent_count == 0


def test_dry_run_stop_does_not_claim_physical_delivery():
    cls = _bridge_methods(
        "_post_command",
        responder=lambda *_args, **_kw: _Response(
            b'{"ok":true,"delivered":false,"dry_run":true}'
        ),
    )
    bridge, _ = _fake_bridge(cls)
    bridge.motor_output_enabled = False
    assert bridge._post_command({"type": "stop", "reason": "test"}) is True


def test_actual_motor_drive_requires_explicit_delivery_ack():
    cls = _bridge_methods(
        "_post_command",
        responder=lambda *_args, **_kw: _Response(
            b'{"ok":true,"accepted":true,"delivered":false,"blocked":true}'
        ),
    )
    bridge, _ = _fake_bridge(cls)
    bridge.motor_output_enabled = True
    assert bridge._post_command({
        "type": "auto_drive", "left_mps": 0.10, "right_mps": 0.10,
    }) is False
    assert bridge.send_error_count == 1
    assert bridge.sent_count == 0


def test_actual_motor_drive_rejects_contradictory_blocked_ack():
    cls = _bridge_methods(
        "_post_command",
        responder=lambda *_args, **_kw: _Response(
            b'{"ok":true,"delivered":true,"blocked":true}'
        ),
    )
    bridge, _ = _fake_bridge(cls)
    bridge.motor_output_enabled = True
    assert bridge._post_command({
        "type": "auto_drive", "left_mps": 0.10, "right_mps": 0.10,
    }) is False


def test_dry_run_drive_can_be_accepted_without_pi_delivery():
    cls = _bridge_methods(
        "_post_command",
        responder=lambda *_args, **_kw: _Response(
            b'{"ok":true,"accepted":true,"delivered":false,"blocked":true,"dry_run":true}'
        ),
    )
    bridge, _ = _fake_bridge(cls)
    bridge.motor_output_enabled = False
    assert bridge._post_command({
        "type": "auto_drive", "left_mps": 0.10, "right_mps": 0.10,
    }) is True
