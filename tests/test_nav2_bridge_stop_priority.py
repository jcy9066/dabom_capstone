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
    env = {"threading": threading, "time": time, "NavBridgeBase": NavBridgeBase}
    exec(compile(module, str(SOURCE), "exec"), env)
    return env["OfflineBridge"]


def _fake(cls):
    instance = cls()
    instance._command_condition = threading.Condition()
    instance._pending_command = None
    instance._stop_command_in_flight = False
    instance._stop_delivery_failed = False
    instance._stop_retry_command = None
    instance._stop_retry_due_at = None
    instance.STOP_RETRY_INTERVAL_SEC = 0.50
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


def test_stop_retries_automatically_without_another_twist():
    cls = _bridge_class("_queue_command", "_command_worker")
    bridge = _fake(cls)
    bridge.STOP_RETRY_INTERVAL_SEC = 0.06
    succeeded = threading.Event()
    calls = []

    def post(payload):
        calls.append((payload["type"], time.monotonic()))
        if len(calls) == 1:
            return False
        succeeded.set()
        return True

    bridge._post_command = post
    worker = threading.Thread(target=bridge._command_worker, daemon=True)
    worker.start()
    try:
        assert bridge._queue_command({"type": "stop", "reason": "nav2_twist_timeout"})
        deadline = time.monotonic() + 1.0
        while not bridge._stop_delivery_failed and time.monotonic() < deadline:
            time.sleep(0.005)
        assert bridge._stop_delivery_failed
        assert not bridge._queue_command({"type": "auto_drive", "left_mps": 0.1})
        assert succeeded.wait(1.5), "worker did not retry STOP on its own"
        deadline = time.monotonic() + 1.0
        while bridge._stop_delivery_failed and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not bridge._stop_delivery_failed
        assert len(calls) == 2
        assert calls[1][1] - calls[0][1] >= 0.04
        assert bridge._queue_command({"type": "auto_drive", "left_mps": 0.1})
    finally:
        with bridge._command_condition:
            bridge._pending_command = None
            bridge._worker_shutdown = True
            bridge._command_condition.notify_all()
        worker.join(timeout=2)
        assert not worker.is_alive()


def test_repeated_stop_callbacks_cannot_bypass_retry_interval():
    cls = _bridge_class("_queue_command", "_command_worker")
    bridge = _fake(cls)
    bridge.STOP_RETRY_INTERVAL_SEC = 0.30
    succeeded = threading.Event()
    calls = []

    def post(payload):
        calls.append(time.monotonic())
        if len(calls) == 1:
            return False
        succeeded.set()
        return True

    bridge._post_command = post
    worker = threading.Thread(target=bridge._command_worker, daemon=True)
    worker.start()
    try:
        assert bridge._queue_command({"type": "stop", "reason": "first"})
        deadline = time.monotonic() + 1.0
        while not bridge._stop_delivery_failed and time.monotonic() < deadline:
            time.sleep(0.005)
        assert bridge._stop_delivery_failed
        for index in range(12):
            assert bridge._queue_command({"type": "stop", "reason": f"repeat-{index}"})
            assert not bridge._queue_command({"type": "auto_drive", "left_mps": 0.1})
        time.sleep(0.06)
        assert len(calls) == 1, "duplicate STOP events bypassed retry throttle"
        assert succeeded.wait(1.5)
        assert len(calls) == 2
        assert calls[1] - calls[0] >= 0.25
    finally:
        with bridge._command_condition:
            bridge._pending_command = None
            bridge._worker_shutdown = True
            bridge._command_condition.notify_all()
        worker.join(timeout=2)
        assert not worker.is_alive()


def test_repeated_failures_keep_drive_blocked_until_stop_ack():
    cls = _bridge_class("_queue_command", "_command_worker")
    bridge = _fake(cls)
    bridge.STOP_RETRY_INTERVAL_SEC = 0.04
    succeeded = threading.Event()
    attempts = []

    def post(payload):
        attempts.append(payload["type"])
        if len(attempts) < 4:
            return False
        succeeded.set()
        return True

    bridge._post_command = post
    worker = threading.Thread(target=bridge._command_worker, daemon=True)
    worker.start()
    try:
        assert bridge._queue_command({"type": "stop", "reason": "timeout"})
        deadline = time.monotonic() + 1.5
        while len(attempts) < 3 and time.monotonic() < deadline:
            assert not bridge._queue_command({"type": "auto_drive", "left_mps": 0.1})
            time.sleep(0.01)
        assert len(attempts) >= 3
        assert succeeded.wait(1.5)
        assert attempts == ["stop"] * 4
        deadline = time.monotonic() + 1
        while bridge._stop_delivery_failed and time.monotonic() < deadline:
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


def test_shutdown_does_not_continue_background_stop_retries():
    cls = _bridge_class("_queue_command", "_command_worker", "destroy_node")
    bridge = _fake(cls)
    bridge.STOP_RETRY_INTERVAL_SEC = 0.05
    failed = threading.Event()
    calls = []

    def post(payload):
        calls.append(payload["type"])
        failed.set()
        return False

    bridge._post_command = post
    worker = threading.Thread(target=bridge._command_worker, daemon=True)
    bridge._command_worker_thread = worker
    worker.start()
    try:
        assert bridge._queue_command({"type": "stop", "reason": "bad_http"})
        assert failed.wait(1)
        deadline = time.monotonic() + 1
        while not bridge._stop_delivery_failed and time.monotonic() < deadline:
            time.sleep(0.005)
        assert bridge._stop_delivery_failed
        assert bridge.destroy_node() == "destroyed"
        assert not worker.is_alive()
        assert calls.count("stop") == 2  # original + final shutdown STOP
    finally:
        with bridge._command_condition:
            bridge._worker_shutdown = True
            bridge._command_condition.notify_all()
        worker.join(timeout=2)


def test_http_deadline_allows_remote_pi_ack_round_trip():
    launch = (
        ROOT / "navigation/ros/patrol_navigation/launch/navigation.launch.py"
    ).read_text(encoding="utf-8")
    assert '"nav2_request_timeout_sec",\n                # Must permit' in launch
    assert 'default_value="1.50"' in launch
