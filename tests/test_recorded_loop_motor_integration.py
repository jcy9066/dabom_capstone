"""Real replay/API/motor logic with captured UART and an ideal wheel plant."""
import asyncio
import math
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI

from raspberry.controllers.motor_controller import MotorController
from server.recorded_loop_api import RecordedLoopApi
from server.recorded_loop_executor import RecordedMotorExecutor
from server.recorded_loop_motor_math import recorded_to_auto_drive
from server.recorded_loop_obstacle import path_blocked


@pytest.mark.parametrize("directions", [
    ["forward"], ["forward_left"], ["rotate_left"],
    ["forward", "backward"], ["forward_left", "forward_right"],
    ["forward", "forward_left", "forward", "rotate_right", "backward"],
])
@pytest.mark.parametrize("latency", [0., .05, .10])
def test_pwm_integral_and_curve_survive_actual_ramp_and_interlock(monkeypatch, tmp_path, directions, latency):
    import raspberry.controllers.motor_controller as driver
    import server.recorded_loop_executor as executor

    monkeypatch.setenv("MAX_WHEEL_MPS", ".212")
    motor = MotorController()
    motor._ensure_connected_locked = Mock()  # No serial device is opened.
    motor._trace_motor_output = Mock()
    now = [10.0]
    output = [0.0, 0.0]
    integral = [0.0, 0.0]
    sequence = [0]
    stopped_switches = []
    actual = []
    sent_commands = []
    context = {"safety_session": "test-pi", "safety_epoch": 1, "goal_generation": 1}

    def telemetry():
        sequence[0] += 1
        motor._encoder_state.update(sequence=sequence[0], updated_monotonic=now[0],
            left_front_ticks=round(integral[0]*10000), left_rear_ticks=round(integral[0]*10000),
            right_front_ticks=round(integral[1]*10000), right_rear_ticks=round(integral[1]*10000))

    def uart(line):
        if line.startswith("DRIVE,"):
            output[:] = map(float, line.split(",")[1:])
            actual.append(tuple(output))
            assert max(map(abs, output)) <= .3
        elif line.startswith("STOP,"):
            output[:] = [0., 0.]
            if line == "STOP,direction_switch":
                stopped_switches.append(now[0])

    motor._exchange_locked = uart

    timers = []
    class Timer:
        def __init__(self, delay, callback):
            self.at, self.callback, self.cancelled = now[0]+delay, callback, False
        def start(self): timers.append(self)
        def cancel(self): self.cancelled = True
    async def sleep(dt):
        end = now[0]+dt
        while True:
            due = [t for t in timers if not t.cancelled and t.at <= end]
            timer = min(due, key=lambda t:t.at) if due else None
            until = timer.at if timer else end
            for i in range(2): integral[i] += output[i]*(until-now[0])
            now[0] = until
            telemetry()
            if timer is None: break
            timer.cancelled = True
            timer.callback()

    async def authorize():
        return True

    async def pose():
        # Server tolerance is not used as the test's accuracy assertion;
        # the independent wheel integrals below must match the recording.
        return {"x": (integral[0]+integral[1])*.212/2, "y": 0.,
                "yaw": (integral[1]-integral[0])*.212/.201}

    async def blocked(command):
        return path_blocked({"received_at": now[0], "angle_min": -math.pi,
            "angle_increment": math.pi/180, "range_min": .1, "range_max": 12.,
            "ranges": [2.]*360}, command["direction"], now=now[0])

    async def stop():
        motor.stop("test_stop")
        previous = tuple(integral)
        for _ in range(11):
            await sleep(.05)  # Fresh 20-Hz stationary encoders, not a stale ACK.
        return previous == tuple(integral)

    async def sender(robot, command, **kwargs):
        assert kwargs["motion_ack"] is True and kwargs["preflight"]()
        sent_commands.append(dict(command))
        await sleep(latency)
        snapshot = motor.drive_recorded(command["left_mps"], command["right_mps"],
            command["rear_blind_limited"], command["replay_command_id"], command["replay_duration"])
        await sleep(latency)
        return {"ok": True, "delivered": True,
            "motion_applied": motor.current_motion.startswith("drive:"), "recorded_drive": snapshot}

    nav = SimpleNamespace(_robot_id="test-pi", recorded_safety_context=lambda: dict(context),
        _pi_command_delivered=lambda r: r["delivered"], recorded_motion_delivered=lambda c: True)
    api = RecordedLoopApi(FastAPI(), nav, None, lambda _: None, tmp_path/"routes.json",
        send_robot_command=sender, max_wheel_mps=.212)
    commands = [{"direction": direction, "speed": .3, "duration": .08} for direction in directions]
    expected = [0.0, 0.0]
    for command in commands:
        wheels = recorded_to_auto_drive(command, .212)
        for i, key in enumerate(("left_mps", "right_mps")):
            expected[i] += wheels[key]/.212*command["duration"]
    final_pose = {"x": sum(expected)*.212/2, "y": 0.,
                  "yaw": (expected[1]-expected[0])*.212/.201}
    engine = RecordedMotorExecutor(authorize=authorize, drive=api._play_drive,
        stop=stop, pose=pose, blocked=blocked, replan=Mock())
    clock = SimpleNamespace(monotonic=lambda: now[0])
    monkeypatch.setattr(driver, "time", clock)
    monkeypatch.setattr(driver, "threading", SimpleNamespace(Timer=Timer))
    monkeypatch.setattr(executor, "time", clock)
    monkeypatch.setattr(executor, "asyncio", SimpleNamespace(sleep=sleep, wait_for=asyncio.wait_for))
    result = asyncio.run(engine.run([{"source": "manual", "commands": commands,
        "poses": [{"x": 0., "y": 0., "yaw": 0.}, final_pose]}], max_laps=1))
    assert result == {"ok": True, "laps": 1}
    assert integral == pytest.approx(expected, abs=.00015)
    if directions == ["forward_left"]:
        assert actual[0][0]/actual[0][1] == pytest.approx(.35, abs=.01)
    if len(directions) > 1:
        assert len(stopped_switches) == len(directions)-1
    assert motor._direction_interlock.STABLE_SEC == .5
    assert output == [0., 0.]

    last = sent_commands[-1]
    output_count = len(actual)
    duplicate = motor.drive_recorded(last["left_mps"], last["right_mps"],
        last["rear_blind_limited"], last["replay_command_id"], last["replay_duration"])
    assert duplicate["elapsed"] == last["replay_duration"]
    assert len(actual) == output_count  # Retried completed UUID never moves again.
    for timer in timers:
        if timer.cancelled: timer.callback()  # Stale timer cannot touch a new owner.
    assert len(actual) == output_count


def test_completion_poll_cannot_cancel_timer_without_stopping_output(monkeypatch):
    import raspberry.controllers.motor_controller as driver
    monkeypatch.setenv("MAX_WHEEL_MPS", ".212")
    motor = MotorController()
    motor._ensure_connected_locked = Mock()
    motor._exchange_locked = Mock()
    motor._trace_motor_output = Mock()
    now = [10.]
    monkeypatch.setattr(driver, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(driver, "threading", SimpleNamespace(Timer=lambda *args: Mock()))
    motor.drive_recorded(.0636, .0636, False, "race", .02)
    assert motor.current_motion.startswith("drive:")
    now[0] += .1  # Expired timer has not yet acquired the UART lock.
    reply = motor.drive_recorded(.0636, .0636, False, "race", .02)
    assert reply["elapsed"] == .02
    assert motor.current_motion == "stop"
    motor._exchange_locked.assert_called_with("STOP,recorded_budget_complete")
