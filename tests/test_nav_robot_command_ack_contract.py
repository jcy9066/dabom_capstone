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
                   "send_command", "send_command_wait_ack", "receive_ack",
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
    result.lock = asyncio.Lock()
    result.pending_acks = {}
    result.ack_timeout_sec = 0.08
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
