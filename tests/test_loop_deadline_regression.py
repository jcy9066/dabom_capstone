"""Software-only regression for a stalled manual movement ACK."""
import asyncio
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "loop_test_fixture", Path(__file__).with_name("test_loop_session_api.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_stop_is_requested_while_motion_ack_is_stalled(tmp_path):
    api, _, _ = module.fixture(tmp_path)

    async def scenario():
        motion_entered = asyncio.Event()
        release_motion = asyncio.Event()
        stop_requested = asyncio.Event()
        calls = 0

        async def verified_stop():
            nonlocal calls
            calls += 1
            if calls > 1:
                stop_requested.set()
                api._cancelled = True

        async def stalled_motion(_packet):
            motion_entered.set()
            await release_motion.wait()

        async def clear(_direction):
            return 0.0

        api._verified_stop = verified_stop
        api._manual_send = stalled_motion
        api._wait_obstacle_clear = clear
        events = [
            {"at": 0.0, "type": "move", "direction": "forward", "speed": 0.2},
            {"at": 0.05, "type": "stop"},
        ]
        task = asyncio.create_task(api._manual_bag(api._active(), events))
        try:
            await asyncio.wait_for(motion_entered.wait(), timeout=1.0)
            await asyncio.wait_for(stop_requested.wait(), timeout=0.3)
        finally:
            release_motion.set()
            api._cancelled = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("stop_succeeds", [True, False])
def test_timing_overrun_stops_and_fails_closed(tmp_path, stop_succeeds):
    api, nav, _ = module.fixture(tmp_path)

    async def scenario():
        stop_calls = []
        move_calls = []
        gate = asyncio.Event()

        async def stop():
            stop_calls.append(True)
            # The initial STOP succeeds; later STOPs use the selected result.
            return len(stop_calls) == 1 or stop_succeeds

        async def stalled_send(packet):
            move_calls.append(packet["direction"])
            await gate.wait()

        async def clear(_direction):
            return 0.0

        api.recorded._play_stop = stop
        api._manual_send = stalled_send
        api._wait_obstacle_clear = clear
        events = [
            {"at": 0.0, "type": "move", "direction": "forward", "speed": 0.2},
            {"at": 0.05, "type": "stop"},
            {"at": 0.06, "type": "move", "direction": "backward", "speed": 0.2},
            {"at": 0.10, "type": "stop"},
        ]

        await asyncio.wait_for(
            api._run(api._active(), "MANUAL", events, None),
            timeout=2.0,
        )

        assert move_calls == ["forward"]
        assert len(stop_calls) >= 2
        assert api._status == "FAILED"
        if stop_succeeds:
            assert api._error == "MANUAL_REPLAY_TIMING_OVERRUN"
            assert nav.emergency_calls == 0
        else:
            assert api._error == "STOP_NOT_CONFIRMED"
            assert nav.emergency_calls >= 1

    asyncio.run(scenario())


def test_normal_forward_and_backward_order(tmp_path):
    api, _, _ = module.fixture(tmp_path)

    async def scenario():
        actions = []
        stop_count = 0

        async def stop():
            nonlocal stop_count
            stop_count += 1
            actions.append("stop")
            # Initial stop, then one stop after each movement.
            if stop_count == 3:
                api._cancelled = True

        async def send(packet):
            actions.append(packet["direction"])

        async def clear(_direction):
            return 0.0

        api._verified_stop = stop
        api._manual_send = send
        api._wait_obstacle_clear = clear

        events = [
            {"at": 0.00, "type": "move", "direction": "forward", "speed": 0.2},
            {"at": 0.08, "type": "stop"},
            {"at": 0.10, "type": "move", "direction": "backward", "speed": 0.2},
            {"at": 0.18, "type": "stop"},
        ]

        await asyncio.wait_for(api._manual_bag(api._active(), events), timeout=2.0)
        assert actions == ["stop", "forward", "stop", "backward", "stop"]

    asyncio.run(scenario())


def test_stalled_ack_uses_transport_and_rejects_late_ack(tmp_path):
    transport_spec = importlib.util.spec_from_file_location(
        "transport_fixture",
        Path(__file__).with_name("test_nav_robot_command_ack_contract.py"),
    )
    transport = importlib.util.module_from_spec(transport_spec)
    transport_spec.loader.exec_module(transport)

    async def scenario():
        api, _, _ = module.fixture(tmp_path)
        manager = transport._manager()
        manager.ack_timeout_sec = 1.0

        class Socket:
            def __init__(self):
                self.commands = []

            async def send_json(self, command):
                command = dict(command)
                self.commands.append(command)
                if command["type"] == "stop":
                    asyncio.create_task(manager.receive_ack(
                        "test-robot", self,
                        {"command_id": command["command_id"], "ok": True},
                    ))

        socket = Socket()
        manager.active["test-robot"] = socket

        async def stop():
            return await manager.send_command_wait_ack(
                "test-robot", {"type": "stop"}
            )

        async def clear(_direction):
            return 0.0

        api.send = manager.send_command_wait_ack
        api.recorded._play_stop = stop
        api._wait_obstacle_clear = clear
        events = [
            {"at": 0.00, "type": "move", "direction": "forward", "speed": 0.2},
            {"at": 0.10, "type": "stop"},
            {"at": 0.12, "type": "move", "direction": "backward", "speed": 0.2},
            {"at": 0.20, "type": "stop"},
        ]

        await asyncio.wait_for(
            api._run(api._active(), "MANUAL", events, None),
            timeout=2.0,
        )

        commands = socket.commands
        moves = [item for item in commands if item["type"] == "move"]
        assert [item["direction"] for item in moves] == ["forward"]
        assert [item["type"] for item in commands].count("stop") >= 2
        assert api._status == "FAILED"
        assert api._error == "MANUAL_REPLAY_TIMING_OVERRUN"
        assert manager.pending_acks == {}

        late_ack = {
            "command_id": moves[0]["command_id"],
            "ok": True,
            "safety_session": "session",
            "safety_epoch": 1,
        }
        assert not await manager.receive_ack("test-robot", socket, late_ack)

    asyncio.run(scenario())
