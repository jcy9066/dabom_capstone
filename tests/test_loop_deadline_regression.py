"""Software-only LOOP MANUAL replay timing, STOP, obstacle, and recovery tests.

Unlike the former absolute-event deadline tests, progress is the sum of Pi
confirmed pulse exposure. Physical hardware verification is separate.
"""
import asyncio
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "loop_test_fixture", Path(__file__).with_name("test_loop_session_api.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def events(ms=500):
    return [
        {"at": 0.0, "type": "move", "direction": "forward", "speed": 0.2},
        {"at": ms / 1000, "type": "stop"},
    ]


def test_pi_ack_timeout_forces_stop_and_never_guesses_applied_time(tmp_path, monkeypatch):
    monkeypatch.setattr("server.loop_session_api.MANUAL_ACK_TIMEOUT_SEC", 0.06)
    api, nav, sent = module.fixture(tmp_path)
    stop_calls = []
    motor_entered = asyncio.Event()

    async def stop():
        stop_calls.append(True)

    async def silent_pi(*args, **kwargs):
        motor_entered.set()
        await asyncio.Event().wait()

    async def clear(_direction):
        return 0

    api._verified_stop = stop
    api._wait_obstacle_clear = clear
    api.send = silent_pi

    async def no_receipt():
        raise RuntimeError("MANUAL_PULSE_RECEIPT_UNAVAILABLE")

    api._reconcile_uncertain_manual_pulse = no_receipt

    async def scenario():
        await asyncio.wait_for(
            api._run(api._active(), "MANUAL", events(200), None), timeout=1.0
        )
    asyncio.run(scenario())
    assert motor_entered.is_set()
    assert api._status == "FAILED"
    assert api._error == "MANUAL_PULSE_RECEIPT_UNAVAILABLE"
    assert api._manual_elapsed_ms == 0
    assert len(stop_calls) >= 2


def test_direction_reversal_requires_intervening_stop_proof(tmp_path):
    api, nav, sent = module.fixture(tmp_path)
    actions = []
    original = api._manual_send

    async def traced_pulse(packet):
        actions.append(packet["direction"])
        return await original(packet)

    async def stop():
        actions.append("STOP")

    async def clear(_direction):
        return 0

    api._manual_send = traced_pulse
    api._verified_stop = stop
    api._wait_obstacle_clear = clear
    bag = [
        {"at": 0, "type": "move", "direction": "forward", "speed": .2},
        {"at": .02, "type": "stop"},
        {"at": .03, "type": "move", "direction": "backward", "speed": .2},
        {"at": .05, "type": "stop"},
    ]
    async def scenario():
        task = asyncio.create_task(api._manual_bag(api._active(), bag))
        await asyncio.sleep(.07)
        api._cancelled = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    forward = actions.index("forward")
    backward = actions.index("backward")
    assert forward < backward
    assert "STOP" in actions[forward + 1:backward]


def test_obstacle_pause_preserves_remaining_motor_exposure(tmp_path):
    api, nav, sent = module.fixture(tmp_path)
    remaining_gate = asyncio.Event()
    first_pulse = asyncio.Event()
    pulses = []

    async def pulse(packet):
        pulses.append(packet["duration_ms"])
        await asyncio.sleep(packet["duration_ms"] / 1000)
        if len(pulses) == 1:
            first_pulse.set()
        return float(packet["duration_ms"])

    async def clearance(_direction):
        if first_pulse.is_set():
            await remaining_gate.wait()
        return 0

    async def scenario():
        api._manual_send = pulse
        api._wait_obstacle_clear = clearance
        task = asyncio.create_task(api._manual_bag(api._active(), events(400)))
        await asyncio.wait_for(first_pulse.wait(), 1)
        await asyncio.sleep(.06)
        first_progress = api._manual_elapsed_ms
        assert first_progress > 0
        assert len(pulses) == 1
        remaining_gate.set()
        await asyncio.sleep(.25)
        assert api._manual_elapsed_ms >= first_progress
        api._cancelled = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_reconnect_does_not_resume_until_explicit_operator_request(tmp_path):
    api, nav, sent = module.fixture(tmp_path)
    first = asyncio.Event()
    pulsed = []

    async def pulse(packet):
        pulsed.append(packet["pulse_id"])
        await asyncio.sleep(.02)
        if len(pulsed) == 1:
            api.note_pi_connection(False)
            nav.connected = False
            first.set()
        return float(packet["duration_ms"])

    async def clear(_direction):
        return 0

    async def scenario():
        nav.connected = True
        api._manual_send = pulse
        api._wait_obstacle_clear = clear
        api._running_kind = "MANUAL"
        api._task = asyncio.current_task()
        task = asyncio.create_task(api._manual_bag(api._active(), events(400)))
        await asyncio.wait_for(first.wait(), 1)
        await asyncio.sleep(.05)
        assert api._requires_resume
        assert len(pulsed) == 1
        nav.connected = True
        api.note_pi_connection(True)
        await asyncio.sleep(.1)
        assert api._status == "RESUME_REQUIRED"
        assert len(pulsed) == 1
        api.resume({})
        await asyncio.sleep(.08)
        assert len(pulsed) > 1
        api._cancelled = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_stale_or_malformed_pi_ack_cannot_advance_segment(tmp_path):
    api, nav, sent = module.fixture(tmp_path)
    api._running_kind = "MANUAL"
    api._replay_generation = 1
    api._manual_session_id = "test"
    async def bogus(_robot_id, command, **kwargs):
        return {"ok": True, "delivered": True, "manual_loop_pulse": {
            "pulse_id": "different-id",
            "replay_generation": command["replay_generation"],
            "duration_ms": command["duration_ms"],
            "elapsed_ms": command["duration_ms"], "stopped": True,
        }}
    api.send = bogus
    async def scenario():
        with pytest.raises(RuntimeError, match="MANUAL_PULSE_ACK_INVALID"):
            await api._manual_send({
                "pulse_id": "correct-id", "pulse_sequence": 1,
                "duration_ms": 80, "direction": "forward", "speed": .2,
            })
    asyncio.run(scenario())
    assert api._manual_elapsed_ms == 0


def test_manual_send_rejects_stale_scan_at_dispatch(tmp_path):
    api, nav, sent = module.fixture(tmp_path)
    api._replay_generation = 1
    api._running_kind = "MANUAL"
    # The scan may be safe during the preceding obstacle check, but stale
    # by the time the command reaches the serialized send preflight.
    api.recorded.get_live_scan = lambda: {
        "received_at": 0.0,
        "angle_min": -3.141592653589793,
        "angle_increment": 2 * 3.141592653589793 / 360,
        "range_min": 0.12, "range_max": 6.0, "ranges": [2.0] * 360,
    }
    async def scenario():
        with pytest.raises(RuntimeError, match="MANUAL_PULSE_ACK_UNCERTAIN"):
            await api._manual_send({
                "pulse_id": "stale-scan", "pulse_sequence": 1,
                "direction": "forward", "speed": 0.2,
                "duration_ms": 80,
            })
    asyncio.run(scenario())
    assert not any(command["type"] == "manual_loop_pulse" for command in sent)
    assert api._manual_elapsed_ms == 0


def test_uncertain_pulse_recovers_only_exact_pi_receipt(tmp_path):
    api, nav, sent = module.fixture(tmp_path)
    original = {
        "type": "manual_loop_pulse",
        "pulse_id": "receipt-007",
        "replay_generation": 2, "safety_session": "session",
        "safety_epoch": 1, "duration_ms": 100,
    }
    api._uncertain_manual_pulse = dict(original)
    api._requires_resume = True
    api._replay_generation = 3
    stopped = []

    async def confirmed_resume():
        stopped.append("confirmed_STOP_and_encoder")
        api._requires_resume = False

    async def receipt_query(robot, command, **_kwargs):
        assert stopped
        assert command["type"] == "manual_loop_query"
        assert command["pulse_id"] == original["pulse_id"]
        assert command["pulse_safety_epoch"] == 1
        return {"ok": True, "delivered": True,
                "manual_loop_pulse": {
                    "pulse_id": "receipt-007", "replay_generation": 2,
                    "duration_ms": 100, "elapsed_ms": 62, "stopped": True,
                }}

    api._wait_connection_resume = confirmed_resume
    api.send = receipt_query
    actual = asyncio.run(api._reconcile_uncertain_manual_pulse())
    assert actual == 62
    assert api._uncertain_manual_pulse is None
    assert api._status == "RUNNING"


def test_missing_receipt_does_not_guess_or_replay(tmp_path):
    api, nav, sent = module.fixture(tmp_path)
    api._uncertain_manual_pulse = {
        "pulse_id": "no-ack", "replay_generation": 2,
        "safety_session": "session", "safety_epoch": 1,
        "duration_ms": 120,
    }
    api._requires_resume = True

    async def resume():
        api._requires_resume = False

    async def missing(_robot, query, **_kwargs):
        assert query["type"] == "manual_loop_query"
        return False

    api._wait_connection_resume = resume
    api.send = missing

    async def scenario():
        with pytest.raises(RuntimeError, match="MANUAL_PULSE_RECEIPT_UNAVAILABLE"):
            await api._reconcile_uncertain_manual_pulse()

    asyncio.run(scenario())
    assert api._uncertain_manual_pulse["pulse_id"] == "no-ack"
    assert not any(v.get("type") == "manual_loop_pulse" for v in sent)
