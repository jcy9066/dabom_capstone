import asyncio
import math
import os
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from server.navigation_control_api import (
    NavigationControlApi,
    NavigationControlError,
    NavigationWatchdogConfig,
)


@dataclass
class FakeMap:
    map_name: str = "test_map"
    yaml_path: Path = Path("navigation/maps/test_map.yaml")
    width: int = 100
    height: int = 80
    resolution: float = 0.05
    origin_x: float = -2.0
    origin_y: float = -1.0
    origin_yaw: float = 0.0


class FakeRos:
    def __init__(self):
        self.plan_calls = 0
        self.navigate_calls = 0
        self.cancel_calls = 0
        self.state = "IDLE"
        self.live_path = []
        self.health = {
            "available": True,
            "odometry_age_sec": 0.01,
            "tf_ok": True,
            "tf_age_sec": 0.01,
            "localization_ok": True,
            "localization_age_sec": 0.01,
        }

    def compute_path(self, goal):
        self.plan_calls += 1
        return [{"x": 0.0, "y": 0.0}, {"x": goal["x"], "y": goal["y"]}]

    def navigate_to_pose(self, goal):
        self.navigate_calls += 1
        self.state = "NAVIGATING"
        return {"accepted": True, "goal": goal}

    def cancel_navigation(self):
        self.cancel_calls += 1
        self.state = "CANCELED"
        return {"requested": True, "confirmed": True}

    def navigation_status(self):
        return {"state": self.state, "error": None}

    def latest_planned_path(self):
        return [dict(point) for point in self.live_path]

    def watchdog_status(self, now=None):
        return dict(self.health)


class FakeMapApi:
    def __init__(self):
        self.ros_control = FakeRos()
        self.saved_map = FakeMap()
        self.activate_calls = []
        self.control_loader = None

    def set_control_loader(self, loader):
        self.control_loader = loader

    def resolve_map(self, map_name):
        if map_name != self.saved_map.map_name:
            raise AssertionError(f"unexpected map: {map_name}")
        return self.saved_map

    async def activate_map(self, payload, user="unknown"):
        self.activate_calls.append((payload, user))
        return {
            "ok": True,
            "active_map": {
                "map_name": self.saved_map.map_name,
                "width": self.saved_map.width,
                "height": self.saved_map.height,
                "resolution": self.saved_map.resolution,
                "origin": {"x": self.saved_map.origin_x, "y": self.saved_map.origin_y, "yaw": self.saved_map.origin_yaw},
            },
        }


class FakeProcess:
    def __init__(self):
        self.transitions = []

    def transition(self, mode, map_yaml=None):
        self.transitions.append((mode, map_yaml))
        return {"mode": mode}


class NavigationControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.map_api = FakeMapApi()
        self.process = FakeProcess()
        self.commands = []
        self.mode_changes = []

        async def sender(robot_id, command):
            self.commands.append((robot_id, dict(command)))
            return True

        self.app = FastAPI()

        @self.app.middleware("http")
        async def inject_test_session(request, call_next):
            request.scope["session"] = {"user": {"user_id": "tester"}, "csrf_token": "test-token"}
            return await call_next(request)

        def csrf_failure(request):
            if request.headers.get("X-CSRF-Token") == "test-token":
                return None
            return JSONResponse({"ok": False, "error": "csrf"}, status_code=403)

        self.api = NavigationControlApi(
            app=self.app,
            map_api=self.map_api,
            process_control=self.process,
            csrf_failure=csrf_failure,
            robot_id="pi-01",
            get_live_map=lambda: {"width": 1, "height": 1, "data": [[0, 1]]},
            save_map=lambda payload, name: {"map_name": "test_map"},
            send_robot_command=sender,
            on_mode_changed=self.mode_changes.append,
            watchdog=NavigationWatchdogConfig(
                interval_sec=10,
                lidar_timeout_sec=5,
                odometry_timeout_sec=5,
                tf_timeout_sec=5,
                localization_timeout_sec=5,
                pi_timeout_sec=5,
                stop_encoder_grace_sec=0,
                stop_encoder_tick_threshold=4,
            ),
            motor_output_enabled=False,
        )
        now = time.time()
        self.api.note_pi_status({"mode": "auto", "navigation_mode": "mapping", "emergency_stop": False}, now=now)
        self.api.note_navigation_sample("scan", now=now)

    async def driving_ready(self):
        result = await self.api.switch_mode(
            {
                "mode": "DRIVING",
                "source": "existing",
                "map_name": "test_map",
                "initial_pose": {"x": 0, "y": 0, "yaw_degrees": 0},
            },
            user="tester",
        )
        self.api.note_pi_status(
            {"mode": "auto", "navigation_mode": "driving", "emergency_stop": False}
        )
        return result

    async def test_goal_only_plans_until_explicit_start(self):
        await self.driving_ready()
        planned = await self.api.plan_goal({"goal": {"x": 1.0, "y": 1.0, "yaw": 0.4}})
        self.assertEqual("PATH_READY", planned["navigation_state"])
        self.assertEqual(1, self.map_api.ros_control.plan_calls)
        self.assertEqual(0, self.map_api.ros_control.navigate_calls)

        started = await self.api.start_navigation()
        self.assertEqual("NAVIGATING", started["navigation_state"])
        self.assertEqual(1, self.map_api.ros_control.navigate_calls)
        self.assertFalse(started["motor_output_enabled"])
        self.assertTrue(started["dry_run"])


    async def test_live_nav2_replan_replaces_displayed_planned_path(self):
        await self.driving_ready()
        await self.api.plan_goal({"goal": {"x": 1.0, "y": 1.0, "yaw": 0.0}})
        await self.api.start_navigation()

        self.map_api.ros_control.live_path = [
            {"x": 0.0, "y": 0.0},
            {"x": 0.4, "y": 0.2},
            {"x": 1.0, "y": 1.0},
        ]

        state = self.api.state_response()

        self.assertEqual(self.map_api.ros_control.live_path, state["planned_path"])

    async def test_successful_navigation_mode_switch_notifies_visualization_reset(self):
        await self.driving_ready()
        self.assertEqual(["DRIVING"], self.mode_changes)

        await self.api.switch_mode({"mode": "MAPPING"})

        self.assertEqual(["DRIVING", "MAPPING"], self.mode_changes)

    async def test_goal_terminal_and_manual_tolerance_rules(self):
        await self.driving_ready()
        await self.api.plan_goal({"goal": {"x": 1.0, "y": 1.0, "yaw": 0.0}})
        await self.api.start_navigation()
        self.map_api.ros_control.state = "FAILED"
        failed = self.api.state_response()
        self.assertEqual("FAILED", failed["navigation_state"])
        self.assertIsNotNone(failed["active_goal"])

        with self.api._lock:
            self.api._state["navigation_state"] = "NAVIGATING"
        self.map_api.ros_control.state = "SUCCEEDED"
        succeeded = self.api.state_response()
        self.assertEqual("SUCCEEDED", succeeded["navigation_state"])
        self.assertIsNone(succeeded["active_goal"])

        await self.api.plan_goal({"goal": {"x": 1.0, "y": 1.0, "yaw": 0.0}})
        self.api.note_pi_status({"mode": "manual", "navigation_mode": "driving"})
        self.api.note_navigation_sample("pose", payload={"x": 1.1, "y": 1.1})
        manual_reached = self.api.state_response()
        self.assertEqual("SUCCEEDED", manual_reached["navigation_state"])
        self.assertIsNone(manual_reached["active_goal"])

    async def test_legacy_map_load_delegates_to_driving_transition(self):
        self.assertIsNotNone(self.map_api.control_loader)
        loaded = await self.map_api.control_loader(
            {
                "map_name": "test_map",
                "initial_pose": {"x": 0, "y": 0, "yaw_degrees": 0},
            },
            user="legacy-browser",
        )
        self.assertEqual("DRIVING", loaded["navigation_mode"])
        self.assertEqual("DRIVING", self.process.transitions[-1][0])

    async def test_estop_retains_mode_goal_and_resume_replans(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        stopped = await self.api.emergency_stop("operator")
        self.assertTrue(stopped["emergency_stop"])
        self.assertEqual("DRIVING", stopped["navigation_mode"])
        self.assertEqual("auto", stopped["robot_mode"])
        self.assertEqual(1.0, stopped["active_goal"]["x"])
        self.assertEqual("EMERGENCY_STOPPED", stopped["navigation_state"])
        self.api.note_pi_status({"mode": "auto", "emergency_stop": False})
        self.assertTrue(self.api.state_response()["emergency_stop"])

        resumed = await self.api.resume_navigation()
        self.assertFalse(resumed["emergency_stop"])
        self.assertTrue(resumed["replanned"])
        self.assertEqual(2, self.map_api.ros_control.plan_calls)
        self.assertEqual(2, self.map_api.ros_control.navigate_calls)
        command_types = [command[1]["type"] for command in self.commands]
        self.assertIn("emergency_stop", command_types)
        self.assertIn("resume_navigation", command_types)
        self.assertNotIn("auto_drive", command_types)

    async def test_estop_blocks_manual_tolerance_completion(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.emergency_stop("operator")
        self.api.note_pi_status({"mode": "manual", "navigation_mode": "driving"})

        self.api.note_navigation_sample("pose", payload={"x": 1.0, "y": 1.0})

        state = self.api.state_response()
        self.assertTrue(state["emergency_stop"])
        self.assertEqual("EMERGENCY_STOPPED", state["navigation_state"])
        self.assertIsNotNone(state["active_goal"])

    async def test_mapping_estop_can_be_released_without_a_goal(self):
        stopped = await self.api.emergency_stop("operator")
        self.assertTrue(stopped["emergency_stop"])
        resumed = await self.api.resume_navigation()
        self.assertFalse(resumed["emergency_stop"])
        self.assertFalse(resumed["replanned"])
        self.assertEqual("IDLE", resumed["navigation_state"])

    async def test_active_navigation_requires_confirmed_estop_before_mapping(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.switch_mode({"mode": "MAPPING"})
        self.assertEqual(
            "NAVIGATION_STOP_CONFIRMATION_REQUIRED",
            raised.exception.error_code,
        )

        mapped = await self.api.switch_mode(
            {"mode": "MAPPING", "confirm_stop": True}
        )
        self.assertEqual("MAPPING", mapped["navigation_mode"])
        self.assertTrue(mapped["emergency_stop"])
        self.assertIsNone(mapped["active_goal"])

    async def test_pi_loss_while_navigating_triggers_automatic_estop(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        self.api.note_pi_connection(False)
        issue = await self.api.evaluate_watchdog()
        self.assertEqual("PI_CONNECTION_LOSS", issue)
        current = self.api.state_response()
        self.assertTrue(current["emergency_stop"])
        self.assertEqual("PI_CONNECTION_LOSS", current["emergency_reason"])

    async def test_encoder_motion_after_stop_updates_estop_reason(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        self.api.note_encoder({
            "left_front_ticks": 10,
            "right_front_ticks": 10,
            "left_rear_ticks": 10,
            "right_rear_ticks": 10,
        })
        await self.api.emergency_stop("operator")
        self.api.note_encoder({
            "left_front_ticks": 20,
            "right_front_ticks": 20,
            "left_rear_ticks": 20,
            "right_rear_ticks": 20,
        }, now=time.time() + 1)
        await self.api.evaluate_watchdog()
        self.assertEqual("ENCODER_MOVEMENT_AFTER_STOP", self.api.state_response()["emergency_reason"])

    async def test_opposing_wheel_deltas_after_stop_do_not_cancel_out(self):
        self.api.note_encoder({
            "left_front_ticks": 10,
            "right_front_ticks": 10,
            "left_rear_ticks": 10,
            "right_rear_ticks": 10,
        })
        await self.api.emergency_stop("operator")
        self.api.note_encoder({
            "left_front_ticks": 14,
            "right_front_ticks": 6,
            "left_rear_ticks": 14,
            "right_rear_ticks": 6,
        }, now=time.time() + 1)
        issue = await self.api.evaluate_watchdog(now=time.time() + 1)
        self.assertEqual("ENCODER_MOVEMENT_AFTER_STOP", issue)

    async def test_cancel_failure_still_sends_stop(self):
        await self.driving_ready()

        def fail_cancel():
            raise RuntimeError("cancel failed")

        self.map_api.ros_control.cancel_navigation = fail_cancel
        with self.assertRaisesRegex(RuntimeError, "cancel failed"):
            await self.api.cancel_goal()
        self.assertEqual("stop", self.commands[-1][1]["type"])

    async def test_manual_takeover_cancels_ros_but_retains_goal(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        planned_path = list(self.api.state_response()["planned_path"])

        result = await self.api.pause_for_manual()

        self.assertEqual({"x": 1.0, "y": 1.0, "yaw": 0.0}, result["active_goal"])
        self.assertEqual(planned_path, result["planned_path"])
        self.assertEqual("READY", result["navigation_state"])
        self.assertEqual("CANCELED", result["ros_navigation"]["state"])
        self.assertTrue(result["goal_retained"])
        self.assertEqual("manual_drive_takeover", self.commands[-1][1]["reason"])

    async def test_manual_takeover_preserves_emergency_stop_state(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.emergency_stop("test")

        result = await self.api.pause_for_manual()

        self.assertTrue(result["emergency_stop"])
        self.assertEqual("EMERGENCY_STOPPED", result["navigation_state"])
        self.assertIsNotNone(result["active_goal"])

    async def test_manual_takeover_poll_cannot_clear_retained_goal(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        sender_entered = asyncio.Event()
        sender_release = asyncio.Event()
        original_sender = self.api._send_robot_command

        async def blocked_sender(robot_id, command):
            if command.get("reason") == "manual_drive_takeover":
                sender_entered.set()
                await sender_release.wait()
            return await original_sender(robot_id, command)

        self.api._send_robot_command = blocked_sender
        pause_task = asyncio.create_task(self.api.pause_for_manual())
        await sender_entered.wait()

        during_pause = self.api.state_response()
        self.assertEqual("PAUSING_FOR_MANUAL", during_pause["navigation_state"])
        self.assertIsNotNone(during_pause["active_goal"])
        self.assertTrue(during_pause["planned_path"])

        sender_release.set()
        result = await pause_task
        self.assertIsNotNone(result["active_goal"])

    async def test_estop_during_navigation_start_cancels_new_goal(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        entered = threading.Event()
        release = threading.Event()
        original_navigate = self.map_api.ros_control.navigate_to_pose

        def blocked_navigate(goal):
            entered.set()
            release.wait(timeout=2)
            return original_navigate(goal)

        self.map_api.ros_control.navigate_to_pose = blocked_navigate
        start_task = asyncio.create_task(self.api.start_navigation())
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        await self.api.emergency_stop("operator")
        release.set()
        with self.assertRaises(NavigationControlError) as raised:
            await start_task
        self.assertEqual("NAVIGATION_START_INTERRUPTED", raised.exception.error_code)
        self.assertTrue(self.api.state_response()["emergency_stop"])
        self.assertGreaterEqual(self.map_api.ros_control.cancel_calls, 2)

    async def test_rotated_map_bounds_are_enforced(self):
        self.map_api.saved_map.origin_x = 0.0
        self.map_api.saved_map.origin_y = 0.0
        self.map_api.saved_map.origin_yaw = math.pi / 2
        await self.driving_ready()
        await self.api.plan_goal({"x": -1.0, "y": 1.0, "yaw": 0.0})
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        self.assertEqual("GOAL_OUT_OF_BOUNDS", raised.exception.error_code)

    async def test_led_and_warning_use_single_robot_command_path(self):
        led = await self.api.led_test({"duration_ms": 500})
        warning = await self.api.warning({"text": "warning", "led_duration_ms": 700})
        self.assertTrue(led["delivered"])
        self.assertTrue(warning["delivered"])
        self.assertEqual(["led", "warning"], [item[1]["type"] for item in self.commands[-2:]])

    def test_duration_rejects_fractional_values(self):
        with self.assertRaises(NavigationControlError):
            self.api._bounded_int(1.5, 0, 10000, "duration_ms")

    async def test_http_contract_has_one_state_endpoint_and_csrf_protected_mutations(self):
        client = TestClient(self.app)
        state_response = client.get("/api/navigation/control/state")
        self.assertEqual(200, state_response.status_code)
        self.assertEqual("auto", state_response.json()["robot_mode"])
        self.assertEqual(
            {
                "estop_cooldown_sec": float(
                    os.environ["DASHBOARD_ESTOP_COOLDOWN_SEC"]
                ),
                "goal_reached_tolerance_m": float(
                    os.environ["DASHBOARD_GOAL_REACHED_TOLERANCE_M"]
                ),
            },
            state_response.json()["dashboard_config"],
        )

        denied = client.post("/api/navigation/control/led-test", json={"duration_ms": 10})
        self.assertEqual(403, denied.status_code)
        accepted = client.post(
            "/api/navigation/control/led-test",
            json={"duration_ms": 10},
            headers={"X-CSRF-Token": "test-token"},
        )
        self.assertEqual(200, accepted.status_code)
        self.assertTrue(accepted.json()["delivered"])

    def test_state_contract_is_json_safe_while_offline(self):
        offline = NavigationControlApi(
            app=FastAPI(),
            map_api=self.map_api,
            process_control=self.process,
            csrf_failure=lambda request: None,
            robot_id="pi-01",
            get_live_map=lambda: None,
            save_map=lambda payload, name: {},
            send_robot_command=lambda robot_id, command: None,
        ).state_response()
        self.assertIsNone(offline["watchdog"]["pi_age_sec"])
        self.assertIsNone(offline["watchdog"]["lidar_age_sec"])


if __name__ == "__main__":
    unittest.main()
