"""Software-only regression tests for session-only LOOP behavior.

No ROS, Pi, UART or actual motor commands are used.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from server.loop_session_api import LoopSessionApi
from server.recorded_loop_core import RecordedLoopError


class NavigationStub:
    _robot_id = "test-robot"

    def __init__(self):
        import threading
        self._lock = threading.RLock()
        self._loop_session_cancel_token = 0
        self.mode = "manual"
        self.connected = True
        self.navigation_state = "READY"
        self.localization_ready = False
        self.nav2_ready = False
        self.calls = []
        self.cancel_calls = 0
        self.emergency_calls = 0

    def state_response(self):
        return {
            "navigation_mode": "DRIVING",
            "connected": self.connected,
            "robot_mode": self.mode,
            "active_map": {"map_name": "map_one"},
            "localization_ready": self.localization_ready,
            "nav2_ready": self.nav2_ready,
            "navigation_state": self.navigation_state,
            "emergency_stop": False,
            "pi_safety": {"fresh": self.connected, "emergency_stop": False,
                          "safety_session": "session", "safety_epoch": 1},
        }

    def motion_safety_context(self):
        return {"safety_session": "session", "safety_epoch": 1}

    def note_authorized_motion(self, context):
        assert context["safety_session"] == "session"
        return True

    def motion_allowed(self):
        return True

    @staticmethod
    def _pi_command_delivered(response):
        return response is True or (
            isinstance(response, dict)
            and response.get("ok") is True and response.get("delivered") is True
        )

    async def plan_goal(self, payload):
        self.calls.append(("plan", payload))
        self.navigation_state = "PATH_READY"
        return {}

    async def start_navigation(self):
        self.calls.append(("start", None))
        self.navigation_state = "SUCCEEDED"
        return {}

    async def cancel_goal(self):
        self.cancel_calls += 1
        self.navigation_state = "CANCELED"
        return {}

    async def emergency_stop(self, reason, **kwargs):
        self.emergency_calls += 1
        return {}


def fixture(tmp_path):
    yaml = tmp_path / "map_one.yaml"
    image = tmp_path / "map_one.pgm"
    yaml.write_text("image: map_one.pgm\nresolution: 0.05\norigin: [0,0,0]\n")
    image.write_bytes(b"P5\n1 1\n255\n\x00")
    nav = NavigationStub()
    sent = []

    async def send(_robot_id, command, *, preflight=None, motion_ack=False):
        if preflight is not None and not preflight():
            return False
        sent.append(dict(command))
        if command["type"] == "manual_loop_pulse":
            duration = command["duration_ms"]
            await asyncio.sleep(duration / 1000.0)
            return {"ok": True, "delivered": True, "manual_loop_pulse": {
                "pulse_id": command["pulse_id"],
                "replay_generation": command["replay_generation"],
                "elapsed_ms": duration, "duration_ms": duration, "stopped": True,
            }}
        return True

    async def stop():
        return True

    recorded = SimpleNamespace(
        map_api=SimpleNamespace(
            resolve_map=lambda name: SimpleNamespace(yaml_path=yaml),
            ros_control=SimpleNamespace(
                loop_path_valid=lambda path: True,
                finish_loop_trace=lambda: [],
                cancel_navigation=lambda: {
                    "confirmed": True, "requested": True, "completed": True,
                },
            ),
        ),
        _play_stop=stop,
        get_live_scan=lambda: None,
        _pose=lambda: None,
    )
    api = LoopSessionApi(
        app=FastAPI(), navigation=nav, recorded=recorded,
        csrf_failure=lambda request: None, send_robot_command=send)
    return api, nav, sent


def pose(x, y=0.0, yaw=0.0):
    return {"x": x, "y": y, "yaw": yaw}


def test_goal_name_rollover_and_removal(tmp_path):
    api, nav, _ = fixture(tmp_path)
    for i in range(53):
        api.toggle_goal({"pose": pose(float(i))})
    data = api.snapshot()
    names = [item["name"] for item in data["goals"]]
    assert len(names) == 52
    assert names[0] == "B" and names[-1] == "A"
    assert data["goals"][-1]["pose"]["x"] == 52
    assert api.toggle_goal({"pose": pose(1.01)}) == {"ok": True, "removed": "B"}
    assert "B" not in [item["name"] for item in api.snapshot()["goals"]]


def test_manual_bag_no_goal_or_pose_requirement(tmp_path):
    api, nav, _ = fixture(tmp_path)
    assert nav.localization_ready is False
    api.record()
    api.note_motion_ack({"type": "move", "direction": "forward", "speed": 0.25}, True)
    api.note_motion_ack({"type": "stop"}, True)
    async def scenario():
        result = await api.save()
        assert result["name"] == "Loop 1"
    asyncio.run(scenario())
    assert api.snapshot()["goals"] == []
    entry = api._entry(api._active())
    events = entry["bags"]["Loop 1"]["events"]
    assert events[0]["type"] == "move" and events[0]["speed"] == 0.25
    assert events[-1]["type"] == "stop"
    # Each LoopSessionApi has independent in-memory storage.
    another, _, _ = fixture(tmp_path)
    assert another.snapshot()["recordings"] == []


def test_discard_active_recording_without_deleting_saved_loop(tmp_path):
    api, _, _ = fixture(tmp_path)
    api.record()
    assert api.delete({})["discarded"] is True
    assert api.snapshot()["recording"] is False


def test_manual_replan_is_explicitly_rejected_without_goal(tmp_path):
    api, nav, _ = fixture(tmp_path)
    entry = api._entry(api._active())
    entry["bags"]["Loop 1"] = {
        "events": [{"at": 0.0, "type": "move", "direction": "forward", "speed": 0.2},
                   {"at": 0.02, "type": "stop"}], "seconds": 0.02}
    async def scenario():
        with pytest.raises(RecordedLoopError, match="Choose Wait"):
            api.start({"policy": "REPLAN", "name": "Loop 1"})
    asyncio.run(scenario())


def test_manual_bag_replays_without_pose_checks(tmp_path):
    api, _, sent = fixture(tmp_path)
    entry = api._entry(api._active())
    entry["bags"]["Loop 1"] = {
        "events": [
            {"at": 0.0, "type": "move", "direction": "forward", "speed": 0.25},
            {"at": 0.02, "type": "stop"},
        ],
        "seconds": 0.02,
    }
    async def clear(_direction):
        return 0.0
    api._wait_obstacle_clear = clear
    async def scenario():
        api.start({"policy": "WAIT", "name": "Loop 1"})
        await asyncio.sleep(0.09)
        await api.stop()
    asyncio.run(scenario())
    assert any(command["type"] == "manual_loop_pulse"
               and command["direction"] == "forward"
               and command["speed"] == 0.25 for command in sent)


def test_auto_three_goals_uses_existing_nav2_api(tmp_path):
    api, nav, sent = fixture(tmp_path)
    nav.mode = "auto"
    nav.localization_ready = True
    nav.nav2_ready = True
    for i in range(3):
        api.toggle_goal({"pose": pose(i * 2.0)})
    async def scenario():
        api.start({"policy": "REPLAN", "goals": ["A", "B", "C"]})
        await asyncio.sleep(0.70)
        await api.stop()
    asyncio.run(scenario())
    plans = [value for name, value in nav.calls if name == "plan"]
    assert len(plans) >= 3
    assert [item["goal"]["x"] for item in plans[:3]] == [0, 2, 4]
    assert sent == []  # AUTO uses the existing Nav2 flow, not direct Pi MOVE.


def test_manual_operator_stop_cancels_replay(tmp_path):
    api, _, _ = fixture(tmp_path)
    entry = api._entry(api._active())
    entry["bags"]["Loop 1"] = {
        "events": [{"at": 0.0, "type": "move", "direction": "forward", "speed": 0.2},
                   {"at": 0.03, "type": "stop"}], "seconds": 0.03}
    async def clear(_direction):
        return 0.0
    api._wait_obstacle_clear = clear
    async def scenario():
        api.start({"policy": "WAIT", "name": "Loop 1"})
        await asyncio.sleep(0.01)
        api.note_motion_ack({"type": "stop"}, True)
        assert api._cancelled is True
        await api.stop()
    asyncio.run(scenario())


def test_legacy_replay_owner_blocks_session_start(tmp_path):
    api, _, _ = fixture(tmp_path)
    old_task = SimpleNamespace(done=lambda: False)
    api.recorded._play_task = old_task
    with pytest.raises(RecordedLoopError, match="Stop legacy replay"):
        api.record()
    with pytest.raises(RecordedLoopError, match="Stop legacy recorded playback"):
        api.start({"name": "Loop 1", "policy": "WAIT"})


def test_auto_mode_transition_confirms_ros_cancel_and_physical_stop(tmp_path):
    api, nav, _ = fixture(tmp_path)
    nav.mode = "auto"
    nav.localization_ready = True
    nav.nav2_ready = True
    for i in range(3):
        api.toggle_goal({"pose": pose(i * 2.0)})
    checks = {"cancel": 0, "stop": 0}
    def cancel_navigation():
        checks["cancel"] += 1
        return {"confirmed": True, "requested": True, "completed": True}
    async def physical_stop():
        checks["stop"] += 1
        return True
    api.recorded.map_api.ros_control = SimpleNamespace(cancel_navigation=cancel_navigation)
    api.recorded._play_stop = physical_stop

    async def scenario():
        api.start({"policy": "REPLAN", "goals": ["A", "B", "C"]})
        await asyncio.sleep(0.02)
        result = await api.stop(from_navigation=True)
        assert result["stopped"] is True
    asyncio.run(scenario())
    assert checks["cancel"] >= 1
    assert checks["stop"] >= 1


def test_offline_modal_snapshot_without_active_map(tmp_path):
    api, nav, _ = fixture(tmp_path)
    state = nav.state_response
    nav.state_response = lambda: {
        **state(), "connected": False, "navigation_mode": "MAPPING",
        "active_map": None,
    }
    snapshot = api.snapshot()
    assert snapshot["ok"] is True
    assert snapshot["connected"] is False
    assert snapshot["map_name"] is None
    assert snapshot["goals"] == []
    assert snapshot["recordings"] == []
    with pytest.raises(RecordedLoopError, match="Pi connection"):
        api.record()
    async def check():
        with pytest.raises(RecordedLoopError, match="Pi connection"):
            api.start({"policy": "WAIT"})
    asyncio.run(check())


def test_offline_modal_preserves_cached_goals_for_selected_map(tmp_path):
    api, nav, _ = fixture(tmp_path)
    api.toggle_goal({"pose": pose(2.0)})
    state = nav.state_response
    nav.state_response = lambda: {
        **state(), "connected": False, "navigation_mode": "MAPPING",
    }
    snapshot = api.snapshot()
    assert snapshot["ok"] is True
    assert snapshot["map_name"] == "map_one"
    assert snapshot["connected"] is False
    assert [goal["name"] for goal in snapshot["goals"]] == ["A"]
    assert snapshot["goals"][0]["pose"]["x"] == 2.0
    with pytest.raises(RecordedLoopError, match="DRIVING"):
        api.toggle_goal({"pose": pose(4.0)})
