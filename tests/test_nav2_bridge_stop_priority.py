"""ROS-free tests for Nav2 command queue STOP priority and shutdown ordering."""
from __future__ import annotations

import ast
from pathlib import Path
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "navigation/ros/patrol_navigation/patrol_navigation/nav2_command_bridge.py"


def _bridge_class(*names):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    original = next(x for x in tree.body if isinstance(x, ast.ClassDef)
                    and x.name == "Nav2CommandBridge")
    methods = [x for x in original.body
               if isinstance(x, ast.FunctionDef) and x.name in names]
    assert len(methods) == len(names)

    class NavBridgeBase:
        def destroy_node(self):
            return "destroyed"

    declaration = ast.ClassDef(
        name="OfflineBridge", bases=[ast.Name(id="NavBridgeBase", ctx=ast.Load())],
        keywords=[], body=methods, decorator_list=[],
    )
    module = ast.fix_missing_locations(
        ast.Module(body=[declaration], type_ignores=[]),
    )
    env = {"threading": threading, "NavBridgeBase": NavBridgeBase}
    exec(compile(module, str(SOURCE), "exec"), env)
    return env["OfflineBridge"]


def _fake(cls):
    instance = cls()
    instance._command_condition = threading.Condition()
    instance._pending_command = None
    instance._stop_command_in_flight = False
    instance._stop_delivery_failed = False
    instance._worker_shutdown = False
    instance.server_request_enabled = True
    instance.motor_output_enabled = True
    instance.request_timeout_sec = 0.5
    instance.send_error_count = 0
    instance.get_logger = lambda: SimpleNamespace(error=lambda msg: None)
    instance._log_send_error = lambda msg: None
    return instance


def test_stop_supersedes_pending_drive_and_is_not_overwritten():
    cls = _bridge_class("_queue_command")
    bridge = _fake(cls)
    assert bridge._queue_command({"type": "auto_drive", "left_mps": 0.15})
    assert bridge._queue_command({"type": "stop", "reason": "invalid_twist"})
    assert bridge._queue_command({"type": "auto_drive", "left_mps": 0.2}) is False
    assert bridge._pending_command["type"] == "stop"
    # A newer STOP is permitted and still blocks subsequent motion.
    assert bridge._queue_command({"type": "stop", "reason": "timeout"})
    assert bridge._pending_command["reason"] == "timeout"
    bridge._stop_command_in_flight = True
    bridge._pending_command = None
    assert bridge._queue_command({"type": "auto_drive", "left_mps": 0.2}) is False


def test_inflight_drive_then_stop_preserves_stop_under_new_twists():
    cls = _bridge_class("_queue_command", "_command_worker")
    bridge = _fake(cls)
    started = threading.Event()
    release = threading.Event()
    stop_sent = threading.Event()
    messages = []

    def post(payload):
        messages.append(payload["type"])
        if payload["type"] == "auto_drive":
            started.set()
            assert release.wait(2), "test drive not released"
        else:
            stop_sent.set()
        return True

    bridge._post_command = post
    worker = threading.Thread(target=bridge._command_worker, daemon=True)
    worker.start()
    try:
        assert bridge._queue_command({"type": "auto_drive", "left_mps": 0.2})
        assert started.wait(2)
        assert bridge._queue_command({"type": "stop", "reason": "bad_twist"})
        assert not bridge._queue_command({"type": "auto_drive", "left_mps": 0.1})
        release.set()
        assert stop_sent.wait(2)
        assert messages == ["auto_drive", "stop"]
    finally:
        release.set()
        with bridge._command_condition:
            bridge._worker_shutdown = True
            bridge._command_condition.notify_all()
        worker.join(timeout=2)
        assert not worker.is_alive()


def test_stop_in_flight_blocks_motion_until_stop_completes():
    cls = _bridge_class("_queue_command", "_command_worker")
    bridge = _fake(cls)
    started = threading.Event()
    release = threading.Event()

    def post(payload):
        if payload["type"] == "stop":
            started.set()
            assert release.wait(2), "test STOP not released"
        return True

    bridge._post_command = post
    worker = threading.Thread(target=bridge._command_worker, daemon=True)
    worker.start()
    try:
        assert bridge._queue_command({"type": "stop", "reason": "timeout"})
        assert started.wait(2)
        assert not bridge._queue_command({"type": "auto_drive", "left_mps": 0.2})
        release.set()
        limit = time.monotonic() + 2
        while bridge._stop_command_in_flight and time.monotonic() < limit:
            time.sleep(0.005)
        assert not bridge._stop_command_in_flight
    finally:
        release.set()
        with bridge._command_condition:
            bridge._worker_shutdown = True
            bridge._command_condition.notify_all()
        worker.join(timeout=2)
        assert not worker.is_alive()


def test_destroy_orders_final_stop_after_worker_quiesces():
    cls = _bridge_class("_queue_command", "_command_worker", "destroy_node")
    bridge = _fake(cls)
    drive_started = threading.Event()
    release_drive = threading.Event()
    final_stop = threading.Event()
    messages = []

    def post(payload):
        messages.append(payload["type"])
        if payload["type"] == "auto_drive":
            drive_started.set()
            assert release_drive.wait(2)
        else:
            final_stop.set()
        return True

    bridge._post_command = post
    worker = threading.Thread(target=bridge._command_worker, daemon=True)
    bridge._command_worker_thread = worker
    worker.start()
    result = {}
    shutdown_thread = None
    try:
        assert bridge._queue_command({"type": "auto_drive", "left_mps": 0.2})
        assert drive_started.wait(2)
        shutdown_thread = threading.Thread(
            target=lambda: result.setdefault("result", bridge.destroy_node()),
            daemon=True,
        )
        shutdown_thread.start()
        time.sleep(0.03)
        assert not final_stop.is_set(), "STOP preceded outstanding drive"
        release_drive.set()
        shutdown_thread.join(timeout=2)
        assert not shutdown_thread.is_alive()
        assert result["result"] == "destroyed"
        assert messages == ["auto_drive", "stop"]
        assert final_stop.is_set()
    finally:
        release_drive.set()
        with bridge._command_condition:
            bridge._worker_shutdown = True
            bridge._command_condition.notify_all()
        if shutdown_thread is not None:
            shutdown_thread.join(timeout=2)
        worker.join(timeout=2)


def test_failed_stop_latches_motion_until_acknowledged_retry():
    cls = _bridge_class("_queue_command", "_command_worker")
    bridge = _fake(cls)
    finished = threading.Event()
    attempts = []

    def post(payload):
        attempts.append(payload["type"])
        if len(attempts) == 1:
            return False  # HTTP STOP not delivered/acknowledged
        finished.set()
        return True

    bridge._post_command = post
    worker = threading.Thread(target=bridge._command_worker, daemon=True)
    worker.start()
    try:
        assert bridge._queue_command({"type": "stop", "reason": "test_failure"})
        limit = time.monotonic() + 2.0
        while not bridge._stop_delivery_failed and time.monotonic() < limit:
            time.sleep(0.005)
        assert bridge._stop_delivery_failed
        assert not bridge._queue_command({"type": "auto_drive", "left_mps": 0.1})
        assert bridge._queue_command({"type": "stop", "reason": "retry"})
        assert finished.wait(2.0)
        limit = time.monotonic() + 2.0
        while bridge._stop_delivery_failed and time.monotonic() < limit:
            time.sleep(0.005)
        assert not bridge._stop_delivery_failed
        assert bridge._queue_command({"type": "auto_drive", "left_mps": 0.1})
    finally:
        with bridge._command_condition:
            bridge._pending_command = None
            bridge._worker_shutdown = True
            bridge._command_condition.notify_all()
        worker.join(timeout=2)
        assert not worker.is_alive()


def test_shutdown_timeout_queues_stop_without_racing_inflight_drive():
    cls = _bridge_class("_queue_command", "_command_worker", "destroy_node")
    bridge = _fake(cls)
    bridge.request_timeout_sec = 0.01
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    messages = []

    def post(payload):
        messages.append(payload["type"])
        if payload["type"] == "auto_drive":
            started.set()
            assert release.wait(2.0)
        else:
            stopped.set()
        return True

    bridge._post_command = post
    worker = threading.Thread(target=bridge._command_worker, daemon=True)
    bridge._command_worker_thread = worker
    worker.start()
    try:
        assert bridge._queue_command({"type": "auto_drive", "left_mps": 0.2})
        assert started.wait(2.0)
        # Join deliberately times out before releasing the in-flight drive.
        assert bridge.destroy_node() == "destroyed"
        assert not stopped.is_set()
        release.set()
        assert stopped.wait(2.0)
        worker.join(timeout=2.0)
        assert messages == ["auto_drive", "stop"]
        assert not worker.is_alive()
    finally:
        release.set()
        with bridge._command_condition:
            bridge._worker_shutdown = True
            bridge._command_condition.notify_all()
        worker.join(timeout=2.0)


def test_shutdown_join_budget_covers_drive_and_final_stop():
    code = SOURCE.read_text(encoding="utf-8")
    end = code.split("    def destroy_node(", 1)[1].split("\n\ndef main(", 1)[0]
    assert "timeout=2.0 * self.request_timeout_sec + 0.5" in end
    assert end.index('{"type": "stop", "reason": "nav2_command_bridge_shutdown"}') < (
        end.index("self._command_worker_thread.join(")
    )
