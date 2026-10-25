"""Hardware-independent regressions for server -> Pi physical ACK semantics.

The actual server functions are AST-extracted to avoid importing the GPU/ROS
stack. Fake sockets never connect to a real Pi and send no motor commands.
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "server/app.py"


def _manager():
    source = ast.parse(APP.read_text(encoding="utf-8"))
    manager = next(node for node in source.body
                   if isinstance(node, ast.ClassDef)
                   and node.name == "RobotConnectionManager")
    methods = [node for node in manager.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name in {
                   "_send_serialized_command", "send_command",
                   "send_command_wait_ack", "receive_ack",
               }]
    isolated = ast.ClassDef(
        name="IsolatedManager", bases=[], keywords=[],
        body=methods, decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[isolated], type_ignores=[]))
    env = {"asyncio": asyncio, "time": time, "uuid4": uuid4}
    exec(compile(module, str(APP), "exec"), env)
    result = env["IsolatedManager"]()
    result.active = {}
    result._send_locks = {}
    result.lock = asyncio.Lock()
    result.pending_acks = {}
    result.ack_timeout_sec = 0.08
    result.send_timeout_sec = 0.08
    return result


class FakeWebSocket:
    def __init__(self):
        self.commands = []
        self.sent = asyncio.Event()

    async def send_json(self, data):
        self.commands.append(dict(data))
        self.sent.set()


async def _round_trip(command, ack, *, timeout=False):
    manager = _manager()
    ws = FakeWebSocket()
    manager.active["pi-01"] = ws
    request = asyncio.create_task(
        manager.send_command_wait_ack("pi-01", command)
    )
    await asyncio.wait_for(ws.sent.wait(), 1)
    if not timeout:
        assert await manager.receive_ack(
            "pi-01", ws,
            {**ack, "command_id": ws.commands[0]["command_id"]},
        )
    return await request, manager


@pytest.mark.parametrize("name", ["stop", "emergency_stop", "auto_drive", "move"])
def test_physical_commands_require_actual_positive_pi_ack(name):
    result, pending = asyncio.run(_round_trip(
        {"type": name, "command_id": "c1"}, {"ok": False}
    ))
    assert result is False
    assert pending.pending_acks == {}
    result, pending = asyncio.run(_round_trip(
        {"type": name, "command_id": "c2"}, {"ok": True}
    ))
    assert result is True
    assert pending.pending_acks == {}


@pytest.mark.parametrize("name", ["stop", "auto_drive"])
def test_timeout_and_invalid_ack_fail_closed(name):
    result, pending = asyncio.run(_round_trip(
        {"type": name}, {}, timeout=True,
    ))
    assert result is False
    assert not pending.pending_acks
    result, pending = asyncio.run(_round_trip(
        {"type": name}, {"ok": "true"},
    ))
    assert result is False


def test_ack_observer_receives_only_command_id_matched_live_pi_acks():
    async def scenario():
        manager = _manager()
        ws = FakeWebSocket()
        manager.active["pi-01"] = ws
        observed = []
        manager.command_ack_observer = lambda robot, cmd, ack: observed.append(
            (robot, cmd["type"], ack.get("safety_epoch"))
        )
        task = asyncio.create_task(manager.send_command_wait_ack(
            "pi-01", {"type": "stop", "command_id": "stop-observe"},
        ))
        await asyncio.wait_for(ws.sent.wait(), 1)
        assert not await manager.receive_ack("pi-01", ws, {
            "command_id": "wrong-id", "ok": True, "safety_epoch": 99,
        })
        assert observed == []
        assert await manager.receive_ack("pi-01", ws, {
            "command_id": "stop-observe", "ok": True, "safety_epoch": 78,
        })
        assert await task is True
        assert observed == [("pi-01", "stop", 78)]
    asyncio.run(scenario())


def test_transport_failure_observer_reports_preflight_and_timeout():
    async def scenario():
        manager = _manager()
        ws = FakeWebSocket()
        manager.active["pi-01"] = ws
        failures = []
        manager.command_transport_observer = (
            lambda robot, kind, reason: failures.append((robot, kind, reason))
        )
        assert not await manager.send_command_wait_ack(
            "pi-01", {"type": "auto_drive"}, preflight=lambda: False,
        )
        assert ws.commands == []
        assert failures == [
            ("pi-01", "auto_drive", "WebSocket send/preflight rejected"),
        ]
        assert not await manager.send_command_wait_ack(
            "pi-01", {"type": "auto_drive"},
        )
        assert failures[-1] == (
            "pi-01", "auto_drive", "ACK/send deadline exceeded",
        )
    asyncio.run(scenario())


def test_motion_safety_generation_must_match_pi_ack():
    command = {
        "type": "auto_drive", "safety_session": "pi-session-1",
        "safety_epoch": 12,
    }
    for ack in [
        {"ok": True, "safety_session": "pi-session-1", "safety_epoch": 11},
        {"ok": True, "safety_session": "wrong-session", "safety_epoch": 12},
        {"ok": True},
        {"ok": True, "safety_session": "pi-session-1", "safety_epoch": True},
    ]:
        result, _ = asyncio.run(_round_trip(command, ack))
        assert result is False
    result, _ = asyncio.run(_round_trip(
        command, {
            "ok": True, "safety_session": "pi-session-1", "safety_epoch": 12,
        },
    ))
    assert result is True


def test_server_endpoint_requires_ack_for_all_physical_commands():
    code = ast.parse(APP.read_text(encoding="utf-8"))
    fn = next(node for node in code.body
              if isinstance(node, ast.AsyncFunctionDef)
              and node.name == "send_robot_command")
    source = ast.get_source_segment(APP.read_text(encoding="utf-8"), fn)
    assert source is not None
    assert 'command_type in {"move", "auto_drive", "stop", "emergency_stop"}' in source
    assert "if physical_command:" in source
    assert "await connections.send_command_wait_ack(robot_id, command)" in source
    assert "preflight=lambda: navigation_control_api.motion_safety_context() == motion_context" in source
    assert "if delivered and controlled_motion:" in source
    assert '"blocked": not delivered' in source
    assert source.index("if physical_command:") < source.index(
        "if delivered and controlled_motion:"
    )
    # Physical STOP must not use a transport-only ACK shortcut.
    assert "controlled_motion and navigation_control_api.motion_requires_ack()" not in source


def test_robot_manager_only_completes_matching_command_id():
    async def run():
        manager = _manager()
        ws = FakeWebSocket()
        manager.active["pi-01"] = ws
        task = asyncio.create_task(manager.send_command_wait_ack(
            "pi-01", {"type": "stop", "command_id": "this-stop"}
        ))
        await asyncio.wait_for(ws.sent.wait(), 1)
        assert not await manager.receive_ack(
            "pi-01", ws, {"command_id": "other-stop", "ok": True}
        )
        assert not task.done()
        assert await manager.receive_ack(
            "pi-01", ws, {"command_id": "this-stop", "ok": True}
        )
        assert await task is True
    asyncio.run(run())


def test_resume_safety_check_normalizes_only_real_pi_ack():
    command = {"type": "resume_safety_check", "command_id": "check-ok"}
    manager = _manager()
    async def scenario():
        ws = FakeWebSocket()
        manager.active["pi-01"] = ws
        task = asyncio.create_task(
            manager.send_command_wait_ack("pi-01", command)
        )
        await asyncio.wait_for(ws.sent.wait(), 1)
        assert await manager.receive_ack(
            "pi-01", ws, {
                "command_id": "check-ok",
                "ok": True,
                "encoder_sequence": 17,
                "safety_session": "session-1",
                "safety_epoch": 4,
            },
        )
        result = await task
        assert result == {
            "command_id": "check-ok",
            "ok": True,
            "delivered": True,
            "encoder_sequence": 17,
            "safety_session": "session-1",
            "safety_epoch": 4,
        }
        assert not manager.pending_acks
    asyncio.run(scenario())


def test_resume_safety_check_never_trusts_partial_or_negative_pi_ack():
    async def scenario(ack):
        manager = _manager()
        ws = FakeWebSocket()
        manager.active["pi-01"] = ws
        task = asyncio.create_task(
            manager.send_command_wait_ack(
                "pi-01", {"type": "resume_safety_check", "command_id": "check"},
            )
        )
        await asyncio.wait_for(ws.sent.wait(), 1)
        assert await manager.receive_ack(
            "pi-01", ws, {"command_id": "check", **ack},
        )
        result = await task
        assert isinstance(result, dict)
        assert result["delivered"] is False
        return result

    # Even an incoming claim of delivered=true does not override ok=false,
    # and no "ok" field may be upgraded to success.
    for payload in (
        {"ok": False, "delivered": True},
        {"delivered": True},
        {"ok": "true", "delivered": True},
    ):
        result = asyncio.run(scenario(payload))
        assert result["delivered"] is False


def test_resume_safety_check_http_timeout_does_not_claim_delivery():
    result, manager = asyncio.run(_round_trip(
        {"type": "resume_safety_check", "command_id": "timeout"},
        {}, timeout=True,
    ))
    assert result is False
    assert manager.pending_acks == {}


def test_navigation_ack_helper_requires_explicit_dict_delivery():
    import ast
    from pathlib import Path
    nav_source = (
        ROOT / "server/navigation_control_api.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(nav_source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "NavigationControlApi")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                  and n.name == "_pi_command_delivered")
    isolated = ast.fix_missing_locations(ast.Module(
        body=[ast.ClassDef(
            name="OfflineNavigation", bases=[], keywords=[],
            body=[method], decorator_list=[],
        )],
        type_ignores=[],
    ))
    env = {"Any": object}
    exec(compile(isolated, str(APP), "exec"), env)
    valid = env["OfflineNavigation"]._pi_command_delivered
    assert valid(True) is True
    assert valid(False) is False
    assert valid({"ok": True, "delivered": True}) is True
    for payload in (
        {"ok": True}, {"ok": True, "delivered": None},
        {"ok": True, "delivered": 1},
        {"ok": True, "delivered": "true"},
        {"ok": False, "delivered": True},
        {"ok": "true", "delivered": True},
        {"ok": True, "delivered": False},
        None, "OK", 1,
    ):
        assert valid(payload) is False, repr(payload)



def test_stale_queued_auto_drive_is_not_sent_after_goal_cancel():
    async def scenario():
        manager = _manager()
        manager.ack_timeout_sec = 0.5
        ws = FakeWebSocket()
        manager.active["pi-01"] = ws
        send_lock = asyncio.Lock()
        manager._send_locks["pi-01"] = send_lock
        valid = True
        await send_lock.acquire()
        task = asyncio.create_task(manager.send_command_wait_ack(
            "pi-01", {"type": "auto_drive", "command_id": "queued-drive"},
            preflight=lambda: valid,
        ))
        for _ in range(100):
            if "queued-drive" in manager.pending_acks:
                break
            await asyncio.sleep(0)
        assert "queued-drive" in manager.pending_acks
        valid = False
        send_lock.release()
        assert await task is False
        assert ws.commands == []
        assert manager.pending_acks == {}
        # Non-motion STOP still uses the original physical ACK path.
        stop = asyncio.create_task(manager.send_command_wait_ack(
            "pi-01", {"type": "stop", "command_id": "stop-after-cancel"}
        ))
        await asyncio.wait_for(ws.sent.wait(), 1)
        assert await manager.receive_ack("pi-01", ws, {
            "command_id": "stop-after-cancel", "ok": True,
        })
        assert await stop is True
    asyncio.run(scenario())


def test_preflight_is_optional_for_other_commands():
    async def scenario():
        manager = _manager()
        ws = FakeWebSocket()
        manager.active["pi-01"] = ws
        assert await manager.send_command("pi-01", {"type": "led", "enabled": True})
        assert ws.commands[-1]["type"] == "led"
    asyncio.run(scenario())



def test_pi_mode_ack_must_report_actual_requested_mode():
    async def scenario(command, reported):
        manager = _manager()
        ws = FakeWebSocket()
        manager.active["pi-01"] = ws
        command = dict(command, command_id="mode-check")
        task = asyncio.create_task(manager.send_command_wait_ack("pi-01", command))
        await asyncio.wait_for(ws.sent.wait(), 1)
        assert await manager.receive_ack("pi-01", ws, {
            "command_id": "mode-check", "ok": True, **reported,
        })
        return await task

    for command, good, bad in (
        ({"type": "mode", "mode": "auto"}, {"mode": "auto"}, {"mode": "manual"}),
        ({"type": "navigation_mode", "mode": "driving"},
         {"navigation_mode": "driving"}, {"navigation_mode": "mapping"}),
    ):
        assert asyncio.run(scenario(command, good)) is True
        assert asyncio.run(scenario(command, bad)) is False
        assert asyncio.run(scenario(command, {})) is False


def test_ack_dispatch_never_promotes_ack_frame_to_pi_telemetry():
    source = (ROOT / "server/app.py").read_text(encoding="utf-8")
    ack_block = source.split('elif message.get("type") == "ack":', 1)[1]
    ack_block = ack_block.split("except WebSocketDisconnect", 1)[0]
    assert "connections.receive_ack" in ack_block
    assert "navigation_control_api.note_pi_status(message)" not in ack_block


def test_ack_dispatch_never_promotes_ack_frame_to_pi_telemetry():
    source = (ROOT / "server/app.py").read_text(encoding="utf-8")
    ack_block = source.split('elif message.get("type") == "ack":', 1)[1]
    ack_block = ack_block.split("except WebSocketDisconnect", 1)[0]
    assert "connections.receive_ack" in ack_block
    assert "navigation_control_api.note_pi_status(message)" not in ack_block
