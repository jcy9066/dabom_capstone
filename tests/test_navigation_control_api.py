import asyncio
import math
import os
import threading
import time
import unittest
from unittest.mock import patch
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from server.navigation_control_api import (
    NavigationControlApi,
    NavigationControlError,
    NavigationWatchdogConfig,
)
from server.navigation_ros_control import NavigationRosControl


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
    ROTATION_MIN_ANGULAR_RPS = NavigationRosControl.ROTATION_MIN_ANGULAR_RPS
    ROTATION_MAX_LINEAR_MPS = NavigationRosControl.ROTATION_MAX_LINEAR_MPS

    def __init__(self):
        self.plan_calls = 0
        self.navigate_calls = 0
        self.cancel_calls = 0
        self.through_plan_calls = []
        self.through_navigation_calls = []
        self.state = "IDLE"
        self.health = {
            "available": True,
            "odometry_age_sec": 0.01,
            "tf_ok": True,
            "tf_age_sec": 0.01,
            "localization_ok": True,
            "localization_age_sec": 0.01,
        }
        self.rotation = {}
        self.backup_distances = []
        self.spin_radians = []
        self.loop_reapproach_calls = []
        self.follow_path_calls = []
        self.trace = []
        self.trace_started = False
        self.cache_valid = True
        self.validity_queries = 0
        self.path_valid_results = []

    def begin_loop_trace(self):
        self.trace_started = True

    def finish_loop_trace(self):
        self.trace_started = False
        return [dict(p) for p in self.trace]

    def discard_loop_trace(self):
        self.trace_started = False
        self.trace = []

    def loop_path_valid(self, poses):
        self.validity_queries += 1
        return (
            self.path_valid_results.pop(0) if self.path_valid_results
            else self.cache_valid
        )

    def follow_loop_path(self, poses, *, preempt=False):
        self.navigate_calls += 1
        self.follow_path_calls.append({
            "poses": [dict(p) for p in poses],
            "preempt": preempt,
        })
        self.state = "NAVIGATING"
        return {"accepted": True, "action": "/follow_path", "cached": True}

    def compute_path(self, goal, *, loop_forward=False):
        self.plan_calls += 1
        self.last_loop_forward = loop_forward
        return [{"x": 0.0, "y": 0.0}, {"x": goal["x"], "y": goal["y"]}]

    def navigate_to_pose(self, goal, *, loop_reapproach=False):
        self.navigate_calls += 1
        if loop_reapproach:
            self.loop_reapproach_calls.append(dict(goal))
        self.state = "NAVIGATING"
        return {"accepted": True, "goal": goal}

    def reverse_loop_recovery(self, distance_m):
        self.backup_distances.append(distance_m)
        self.state = "NAVIGATING"
        return {"accepted": True, "action": "/backup"}

    def spin_loop_turn(self, radians):
        self.spin_radians.append(radians)
        self.state = "NAVIGATING"
        return {"accepted": True, "action": "/spin"}

    def compute_path_through_poses(self, poses):
        self.plan_calls += 1
        self.through_plan_calls.append([dict(p) for p in poses])
        return [{"x": 0.0, "y": 0.0}, *[dict(p) for p in poses]]

    def navigate_through_poses(self, poses, *, loop=False):
        self.navigate_calls += 1
        self.through_navigation_calls.append([dict(p) for p in poses])
        self.last_loop_flag = loop
        self.state = "NAVIGATING"
        return {"accepted": True, "action": "/navigate_through_poses"}

    def preempt_navigation_route(self, poses):
        self.through_navigation_calls.append([dict(p) for p in poses])
        self.state = "NAVIGATING"
        return {"accepted": True, "preempted": True}

    def cancel_navigation(self):
        self.cancel_calls += 1
        self.state = "CANCELED"
        return {"requested": True, "confirmed": True, "completed": True}

    def navigation_status(self):
        return {"state": self.state, "error": None}

    def watchdog_status(self, now=None):
        return dict(self.health)

    def rotation_status(self):
        return dict(self.rotation)


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

    def transition(self, mode, map_yaml=None, restart=False):
        self.transitions.append((mode, map_yaml, restart))
        return {"mode": mode}


class NavigationControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._env_patch = patch.dict(
            os.environ,
            {
                "NAV_DRIVING_READY_TIMEOUT_SEC": "5",
                "DASHBOARD_ESTOP_COOLDOWN_SEC": "0",
                "DASHBOARD_GOAL_REACHED_TOLERANCE_M": "0.25",
            },
            clear=False,
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

        self.map_api = FakeMapApi()
        self.process = FakeProcess()
        self.commands = []
        self._auto_encoder_recovery = True
        self._fake_safety_epoch = 0

        async def sender(robot_id, command):
            self.commands.append((robot_id, dict(command)))
            if command["type"] == "navigation_mode":
                self.api.note_pi_status({
                    "navigation_mode": command["mode"],
                    "mode": self.api._robot_mode,
                    "emergency_stop": False,
                    **({
                        "safety_session": "test-pi",
                        "safety_epoch": self._fake_safety_epoch,
                    } if self.api._pi_safety_session is not None else {}),
                })
            if command["type"] == "mode":
                self.api.note_pi_status({
                    "mode": command["mode"],
                    "navigation_mode": self.api._pi_navigation_mode or "mapping",
                    "emergency_stop": False,
                    **({
                        "safety_session": "test-pi",
                        "safety_epoch": self._fake_safety_epoch,
                    } if self.api._pi_safety_session is not None else {}),
                })
            if command["type"] == "emergency_stop":
                self._fake_safety_epoch += 1
                self.api.note_pi_status({
                    "safety_session": "test-pi",
                    "safety_epoch": self._fake_safety_epoch,
                    "emergency_stop": True,
                })
            if command["type"] == "resume_safety_check":
                marker = self.api._encoder_sequence or 0
                self.api.note_pi_status({
                    "safety_session": "test-pi",
                    "safety_epoch": self._fake_safety_epoch,
                    "emergency_stop": True,
                })
                if self._auto_encoder_recovery:
                    ticks = self.api._encoder_ticks or (0, 0, 0, 0)
                    loop = asyncio.get_running_loop()
                    for delay, sequence in ((0.02, marker + 1), (0.17, marker + 2)):
                        loop.call_later(delay, self.api.note_encoder, {
                            "sequence": sequence,
                            **dict(zip(
                                ("left_front_ticks", "right_front_ticks", "left_rear_ticks", "right_rear_ticks"),
                                ticks,
                            )),
                        })
                return {
                    "ok": True,
                    "delivered": True,
                    "encoder_sequence": marker,
                    "safety_session": "test-pi",
                    "safety_epoch": self._fake_safety_epoch,
                }
            if command["type"] == "resume_navigation":
                self.api.note_pi_status({
                    "safety_session": "test-pi",
                    "safety_epoch": self._fake_safety_epoch,
                    "emergency_stop": False,
                })
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
            get_live_map=lambda: {
                "width": 1,
                "height": 1,
                "data": [[0, 1]],
                "navigation_mode": "mapping",
            },
            get_live_pose=lambda: {"x": 0.25, "y": 0.5, "yaw": 0.4},
            save_map=lambda payload, name: {"map_name": "test_map"},
            send_robot_command=sender,
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
        self.api.STOP_CONFIRM_STABLE_SEC = 0.1
        self.api.STOP_CONFIRM_TIMEOUT_SEC = 0.8
        now = time.time()
        self.api.note_pi_status({"mode": "auto", "navigation_mode": "mapping", "emergency_stop": False}, now=now)
        self.api.note_navigation_sample("scan", now=now)

    async def test_pi_safety_state_is_fresh_and_separate_from_gpu_estop(self):
        now = time.time()
        self.api.note_pi_status({
            "safety_session": "test-pi",
            "safety_epoch": 7,
            "emergency_stop": True,
        }, now=now)
        result = self.api.state_response(now=now + 0.1)
        self.assertTrue(result["pi_safety"]["fresh"])
        self.assertTrue(result["pi_safety"]["emergency_stop"])
        self.assertEqual("test-pi", result["pi_safety"]["safety_session"])
        self.assertEqual(7, result["pi_safety"]["safety_epoch"])
        stale = self.api.state_response(now=now + 10)
        self.assertFalse(stale["pi_safety"]["fresh"])
        self.assertIsNone(stale["pi_safety"]["emergency_stop"])
        self.api.note_pi_connection(False)
        disconnected = self.api.state_response()
        self.assertFalse(disconnected["pi_safety"]["fresh"])
        self.assertIsNone(disconnected["pi_safety"]["emergency_stop"])

    async def test_pi_mode_telemetry_does_not_override_gpu_mode(self):
        self.api.note_pi_status({
            "mode": "manual",
            "navigation_mode": "driving",
            "emergency_stop": False,
        })
        state = self.api.state_response()
        self.assertEqual("MAPPING", state["navigation_mode"])
        self.assertEqual("DRIVING", state["pi_navigation_mode"])

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

    async def confirm_pending_stop_for_test(self):
        """Simulate a stop ACK followed by fresh, stable encoder samples."""
        with self.api._lock:
            self.assertIsNotNone(self.api._stop_commanded_at)
            marker = self.api._stop_confirmation_after_sequence
            if marker is None:
                # Tests that manually constructed an acknowledged stop.
                marker = self.api._encoder_sequence or 0
                self.api._stop_confirmation_after_sequence = marker
            ticks = self.api._stop_encoder_ticks or (0, 0, 0, 0)
            self.api._stop_confirmation_ticks = None
            self.api._stop_confirmation_started_at = None
            self.api._stop_confirmation_last_at = None
            self.api._stop_confirmation_samples = 0

        keys = (
            "left_front_ticks", "right_front_ticks",
            "left_rear_ticks", "right_rear_ticks",
        )
        self.api.note_encoder({
            "sequence": marker + 1,
            **dict(zip(keys, ticks)),
        })
        await asyncio.sleep(self.api.STOP_CONFIRM_STABLE_SEC + 0.04)
        self.api.note_encoder({
            "sequence": marker + 2,
            **dict(zip(keys, ticks)),
        })
        with self.api._lock:
            self.assertTrue(self.api._stop_confirmed_locked(time.time()))

    def set_rotation(self, sequence, *, command_age=0.01, odom_age=0.01,
                     angular=0.4, yaw_delta=0.04, linear=0.0, yaw=None):
        self.map_api.ros_control.rotation = {
            "odom_sequence": sequence,
            "command_age_sec": command_age,
            "odometry_age_sec": odom_age,
            "command_linear_x": linear,
            "command_angular_z": angular,
            "odom_yaw": sequence * 0.04 if yaw is None else yaw,
            "odom_yaw_delta": yaw_delta,
            "command_since_at": 1.0,
            "odom_interval_start_at": 2.0,
        }

    def refresh_watchdog_samples(self, now):
        self.api.note_pi_status(
            {"mode": "auto", "navigation_mode": "driving", "emergency_stop": False}, now=now,
        )
        self.api.note_navigation_sample("scan", now=now)

    async def test_current_mapping_transitions_directly_to_driving_with_live_pose(self):
        result = await self.api.switch_mode(
            {"mode": "DRIVING", "source": "current"},
            user="tester",
        )

        self.assertEqual("DRIVING", result["navigation_mode"])
        self.assertTrue(result["localization_ready"])
        self.assertTrue(result["nav2_ready"])
        payload, _user = self.map_api.activate_calls[-1]
        self.assertAlmostEqual(0.25, payload["initial_pose"]["x"])
        self.assertAlmostEqual(0.5, payload["initial_pose"]["y"])
        self.assertAlmostEqual(
            math.degrees(0.4),
            payload["initial_pose"]["yaw_degrees"],
        )

    async def test_current_mapping_requires_live_pose(self):
        self.api._get_live_pose = lambda: None
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.switch_mode(
                {"mode": "DRIVING", "source": "current"},
                user="tester",
            )
        self.assertEqual("INITIAL_POSE_REQUIRED", raised.exception.error_code)

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
        with self.api._lock:
            self.assertIsNotNone(self.api._navigation_progress_at)
            self.assertEqual((0.25, 0.5), self.api._navigation_progress_pose)

    async def test_new_goal_replaces_active_goal_after_encoder_stop(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.2})
        await self.api.start_navigation()
        old_plans = self.map_api.ros_control.plan_calls
        task = asyncio.create_task(
            self.api.plan_goal({"x": 1.5, "y": 1.5, "yaw": -0.3})
        )
        await asyncio.sleep(0.03)
        self.assertFalse(task.done())
        interim = self.api.state_response()
        self.assertEqual("REPLACING_GOAL", interim["navigation_state"])
        self.assertEqual(1.0, interim["active_goal"]["x"])
        self.assertFalse(self.api.motion_allowed())
        self.assertEqual(old_plans, self.map_api.ros_control.plan_calls)
        self.assertTrue(any(
            command.get("reason") == "navigation_goal_replace"
            for _, command in self.commands
        ))
        await self.confirm_pending_stop_for_test()
        result = await task
        self.assertEqual("PATH_READY", result["navigation_state"])
        self.assertEqual(1.5, result["active_goal"]["x"])
        self.assertEqual(-0.3, result["active_goal"]["yaw"])
        self.assertEqual(old_plans + 1, self.map_api.ros_control.plan_calls)
        self.assertEqual(1, self.map_api.ros_control.cancel_calls)
        self.assertEqual(1, self.map_api.ros_control.navigate_calls)
        # Replanned Goal must still be explicitly started by operator.
        started = await self.api.start_navigation()
        self.assertEqual("NAVIGATING", started["navigation_state"])
        self.assertEqual(2, self.map_api.ros_control.navigate_calls)

    async def test_invalid_new_goal_does_not_interrupt_old_navigation(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        plans = self.map_api.ros_control.plan_calls
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.plan_goal({"x": 200.0, "y": 1.0, "yaw": 0.0})
        self.assertEqual("GOAL_OUT_OF_BOUNDS", raised.exception.error_code)
        self.assertEqual(0, self.map_api.ros_control.cancel_calls)
        self.assertEqual(plans, self.map_api.ros_control.plan_calls)
        self.assertEqual("NAVIGATING", self.api.state_response()["navigation_state"])

    async def test_replace_goal_fails_closed_without_stop_ack(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        original = self.api._send_robot_command

        async def no_stop(robot_id, command):
            if command["type"] == "stop":
                return False
            return await original(robot_id, command)

        self.api._send_robot_command = no_stop
        plans = self.map_api.ros_control.plan_calls
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.plan_goal({"x": 1.5, "y": 1.0, "yaw": 0.0})
        self.assertEqual(
            "GOAL_REPLACEMENT_STOP_NOT_DELIVERED", raised.exception.error_code,
        )
        state = self.api.state_response()
        self.assertEqual("FAILED", state["navigation_state"])
        self.assertEqual(1.0, state["active_goal"]["x"])
        self.assertFalse(self.api.motion_allowed())
        self.assertEqual(plans, self.map_api.ros_control.plan_calls)
        self.assertFalse(state["emergency_stop"])

    async def test_replace_goal_times_out_without_fresh_encoders(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        self.api.STOP_CONFIRM_TIMEOUT_SEC = 0.10
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.plan_goal({"x": 1.5, "y": 1.0, "yaw": 0.0})
        self.assertEqual(
            "GOAL_REPLACEMENT_STOP_NOT_CONFIRMED", raised.exception.error_code,
        )
        state = self.api.state_response()
        self.assertEqual("FAILED", state["navigation_state"])
        self.assertEqual(1.0, state["active_goal"]["x"])
        self.assertFalse(self.api.motion_allowed())
        self.assertFalse(state["emergency_stop"])

    async def test_replace_goal_cancel_error_keeps_old_goal_and_stop_gate(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        old_plans = self.map_api.ros_control.plan_calls

        def reject_cancel():
            raise RuntimeError("Nav2 cancellation unavailable")

        self.map_api.ros_control.cancel_navigation = reject_cancel
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.plan_goal({"x": 1.5, "y": 1.0, "yaw": 0.0})
        self.assertEqual(
            "GOAL_REPLACEMENT_CANCEL_FAILED", raised.exception.error_code,
        )
        state = self.api.state_response()
        self.assertEqual("FAILED", state["navigation_state"])
        self.assertEqual(1.0, state["active_goal"]["x"])
        self.assertEqual(old_plans, self.map_api.ros_control.plan_calls)
        self.assertFalse(self.api.motion_allowed())

    async def test_replace_goal_e_stop_race_cannot_publish_new_goal(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        task = asyncio.create_task(
            self.api.plan_goal({"x": 1.5, "y": 1.5, "yaw": 0.0})
        )
        await asyncio.sleep(0.03)
        self.assertEqual(
            "REPLACING_GOAL", self.api.state_response()["navigation_state"],
        )
        await self.api.emergency_stop("operator")
        with self.assertRaises(NavigationControlError):
            await task
        state = self.api.state_response()
        self.assertTrue(state["emergency_stop"])
        self.assertEqual("EMERGENCY_STOPPED", state["navigation_state"])
        self.assertEqual(1.0, state["active_goal"]["x"])

    async def test_new_goal_while_path_ready_does_not_send_stop(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        result = await self.api.plan_goal({"x": 1.5, "y": 1.0, "yaw": 0.2})
        self.assertEqual("PATH_READY", result["navigation_state"])
        self.assertEqual(1.5, result["active_goal"]["x"])
        self.assertEqual(2, self.map_api.ros_control.plan_calls)
        self.assertEqual(0, self.map_api.ros_control.cancel_calls)
        self.assertFalse(any(
            c.get("reason") == "navigation_goal_replace" for _, c in self.commands
        ))

    async def test_replacement_plan_error_keeps_old_goal_after_stop(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        old_goal = dict(self.api.state_response()["active_goal"])

        def unavailable_plan(_goal):
            raise RuntimeError("Nav2 planner unavailable")

        task = asyncio.create_task(
            self.api.plan_goal({"x": 1.5, "y": 1.5, "yaw": 0.0})
        )
        await asyncio.sleep(0.03)
        self.map_api.ros_control.compute_path = unavailable_plan
        await self.confirm_pending_stop_for_test()
        with self.assertRaises(RuntimeError):
            await task
        current = self.api.state_response()
        self.assertEqual("FAILED", current["navigation_state"])
        self.assertEqual("GOAL_REPLACEMENT_PLAN_FAILED", current["last_error"])
        self.assertEqual(old_goal, current["active_goal"])
        self.assertFalse(current["emergency_stop"])
        self.assertIsNone(self.api.motion_safety_context())

    async def test_waypoint_route_uses_one_nav2_action_and_final_yaw(self):
        await self.driving_ready()
        request = {
            "goal": {"x": 1.6, "y": 1.0, "yaw": -0.7},
            "waypoints": [{"x": 0.5, "y": 0.5}, {"x": 1.0, "y": 0.8}],
        }
        planned = await self.api.plan_goal(request)
        self.assertEqual("PATH_READY", planned["navigation_state"])
        self.assertEqual(2, len(planned["waypoints"]))
        self.assertEqual(1, len(self.map_api.ros_control.through_plan_calls))
        route = self.map_api.ros_control.through_plan_calls[0]
        self.assertEqual(3, len(route))
        self.assertAlmostEqual(math.atan2(0.3, 0.5), route[0]["yaw"])
        self.assertAlmostEqual(math.atan2(0.2, 0.6), route[1]["yaw"])
        self.assertAlmostEqual(-0.7, route[-1]["yaw"])
        started = await self.api.start_navigation()
        self.assertEqual("/navigate_through_poses", started["navigation"]["action"])
        self.assertEqual(1, len(self.map_api.ros_control.through_navigation_calls))
        self.assertEqual(1, self.map_api.ros_control.navigate_calls)

    async def test_loop_requires_verified_spin_before_next_forward_leg(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        prepared = await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        self.assertEqual("PATH_READY", prepared["navigation_state"])
        started = await self.api.start_navigation()
        self.assertEqual("NAVIGATING", started["navigation_state"])
        self.assertEqual("drive", started["loop_stage"])
        self.assertEqual(1, self.map_api.ros_control.navigate_calls)
        self.assertEqual([a], self.map_api.ros_control.loop_reapproach_calls)
        self.assertEqual([], self.map_api.ros_control.through_navigation_calls)
        self.assertEqual([], self.map_api.ros_control.spin_radians)

        # Simulate arrival at B after initial A and A->B, both physically
        # confirmed. The next B->A leg MUST NOT start before a signed Spin.
        with self.api._lock:
            self.api._loop_completed_legs = 2
            self.api._loop_leg_index = 1
            self.api._loop_stage = "turn_pending"
            self.api._loop_pending = True
            self.api._state["navigation_state"] = "READY"
        with patch.object(self.api, "_stop_confirmed_locked", return_value=False):
            await self.api._advance_loop_leg()
        self.assertEqual([], self.map_api.ros_control.spin_radians)
        self.assertEqual(1, self.map_api.ros_control.navigate_calls)

        live_b = lambda: {"x": b["x"], "y": b["y"], "yaw": 0.0,
                          "received_at": time.time()}
        with patch.object(self.api, "_safe_live_pose", side_effect=live_b):
            with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
                with patch.object(self.api, "_watchdog_issue_locked", return_value=None):
                    await self.api._advance_loop_leg()
        self.assertEqual([-math.pi], self.map_api.ros_control.spin_radians)
        self.assertEqual(1, self.map_api.ros_control.navigate_calls)
        self.assertEqual("turn", self.api.state_response()["loop_stage"])

        # Nav2 SUCCEEDED without encoder-confirmed Pi STOP cannot proceed.
        with self.api._lock:
            self.api._pending_nav_success = {
                "started_at": time.time(), "processing": False,
            }
        with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
            await self.api._resolve_pending_nav_success()
        self.assertEqual("drive_pending", self.api.state_response()["loop_stage"])
        self.assertEqual(0, self.api.state_response()["loop_leg_index"])
        self.assertFalse(self.api._loop_next_turn_clockwise)
        self.assertEqual(1, self.map_api.ros_control.navigate_calls)

        with patch.object(self.api, "_safe_live_pose", side_effect=live_b):
            with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
                with patch.object(self.api, "_watchdog_issue_locked", return_value=None):
                    await self.api._advance_loop_leg()
        self.assertEqual("drive", self.api.state_response()["loop_stage"])
        self.assertEqual(2, self.map_api.ros_control.navigate_calls)
        self.assertEqual(a["x"], self.map_api.ros_control.loop_reapproach_calls[-1]["x"])

    async def test_loop_turn_direction_alternates_after_verified_half_turn(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        with self.api._lock:
            self.api._loop_completed_legs = 3
            self.api._loop_leg_index = 0
            self.api._loop_next_turn_clockwise = False
            self.api._loop_stage = "turn_pending"
            self.api._loop_pending = True
            self.api._state["navigation_state"] = "READY"
        with patch.object(self.api, "_safe_live_pose", return_value={
            "x": a["x"], "y": a["y"], "yaw": math.pi, "received_at": time.time(),
        }):
            with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
                with patch.object(self.api, "_watchdog_issue_locked", return_value=None):
                    await self.api._advance_loop_leg()
        self.assertEqual([math.pi], self.map_api.ros_control.spin_radians)

    def test_loop_leg_and_spin_require_different_arrival_proof(self):
        now = time.time()
        target = {"x": 1.0, "y": 2.0, "yaw": math.pi}
        pose = {"x": 1.1, "y": 2.0, "yaw": 0.0, "received_at": now}
        verify = self.api._loop_pose_confirmed
        self.assertTrue(verify(target, pose, need_yaw=False, now=now, after=now-0.1))
        self.assertFalse(verify(target, pose, need_yaw=True, now=now, after=now-0.1))
        self.assertFalse(verify(target, pose, need_yaw=False, now=now+1, after=now-0.1))
        self.assertFalse(verify(target, pose, need_yaw=False, now=now, after=now+0.01))
        self.assertFalse(verify(target, {**pose, "x": 1.8},
                                need_yaw=False, now=now, after=now-0.1))

    async def test_loop_pivot_headings_for_non_axis_aligned_waypoints(self):
        a = {"x": 0.2, "y": 0.1, "yaw": 1.1}
        b = {"x": 1.2, "y": 1.1, "yaw": -1.3}
        route = self.api._loop_route_points([a, b], first=0, count=6)
        forward_heading = math.atan2(1.0, 1.0)
        for index, point in enumerate(route):
            expected = forward_heading if index % 2 == 0 else forward_heading - math.pi
            self.assertAlmostEqual(0.0, math.atan2(
                math.sin(point["yaw"] - expected),
                math.cos(point["yaw"] - expected),
            ), places=6)
        self.assertEqual([a["x"], b["x"]] * 3, [p["x"] for p in route])

    async def test_loop_spin_success_without_physical_yaw_never_starts_next_leg(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        with self.api._lock:
            self.api._loop_stage = "turn"
            self.api._loop_turn_requested = -math.pi
            self.api._loop_leg_index = 1
            self.api._state["active_goal"] = {
                "x": b["x"], "y": b["y"], "yaw": -math.pi,
            }
            self.api._state["navigation_state"] = "NAVIGATING"
            self.api._pending_nav_success = {
                "started_at": time.time(), "processing": False,
            }
        with patch.object(self.api, "_motor_output_enabled", True):
            with patch.object(self.api, "_safe_live_pose", side_effect=lambda: {
                "x": b["x"], "y": b["y"], "yaw": 0.0,
                "received_at": time.time(),
            }):
                with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
                    await self.api._resolve_pending_nav_success()
        state = self.api.state_response()
        self.assertEqual("FAILED", state["navigation_state"])
        self.assertEqual("ORIGINAL_GOAL_NOT_REACHED", state["last_error"])
        self.assertEqual([], state["loop_goals"])
        self.assertEqual(1, self.map_api.ros_control.navigate_calls)
        self.assertFalse(state["loop_pending"])

    async def test_loop_cancel_between_actions_needs_no_second_nav_cancel(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        with self.api._lock:
            self.api._loop_stage = "turn_pending"
            self.api._loop_pending = True
            self.api._state["active_goal"] = None
            self.api._state["navigation_state"] = "READY"
            # The HTTP handler revokes loop ownership *before* serializing
            # the cancel operation.
            self.api._clear_loop_locked()
        before = self.map_api.ros_control.cancel_calls
        with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
            result = await self.api.cancel_goal()
        self.assertTrue(result["cancel_completed"])
        self.assertFalse(result["loop_active"])
        self.assertEqual(before, self.map_api.ros_control.cancel_calls)

    async def test_late_loop_start_acceptance_after_cancel_is_revoked(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        original = self.map_api.ros_control.navigate_to_pose

        def late_acceptance(goal, *, loop_reapproach=False):
            # Simulate authenticated operator cancel while Nav2 accepts
            # the first action from another thread.
            with self.api._lock:
                self.api._clear_loop_locked()
            return original(goal, loop_reapproach=loop_reapproach)

        with patch.object(self.map_api.ros_control, "navigate_to_pose",
                          side_effect=late_acceptance):
            with self.assertRaises(NavigationControlError) as raised:
                await self.api.start_navigation()
        self.assertEqual("NAVIGATION_START_INTERRUPTED", raised.exception.error_code)
        self.assertTrue(self.api.state_response()["emergency_stop"])
        self.assertGreaterEqual(self.map_api.ros_control.cancel_calls, 1)

    async def test_loop_without_verified_stop_never_starts_spin(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        with self.api._lock:
            self.api._loop_completed_legs = 2
            self.api._loop_leg_index = 1
            self.api._loop_stage = "turn_pending"
            self.api._loop_pending = True
            self.api._state["navigation_state"] = "READY"
        with patch.object(self.api, "_stop_confirmed_locked", return_value=False):
            await self.api._advance_loop_leg()
        self.assertTrue(self.api._loop_pending)
        self.assertEqual([], self.map_api.ros_control.spin_radians)

    async def test_loop_pivot_failure_requires_recent_rotation_and_fresh_pose(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        self.api._loop_stage = "turn"
        self.api._loop_leg_index = 1
        self.api._loop_turn_requested = math.pi
        now = time.time()
        self.map_api.ros_control.rotation = {
            "pivot_command_age_sec": 0.1,
            "pivot_command_angular_z": -0.4,
            "pivot_command_linear_x": 0.0,
        }
        with patch.object(self.api, "_safe_live_pose", return_value={
            "x": b["x"], "y": b["y"], "yaw": 0.0, "received_at": now,
        }):
            with self.api._lock:
                terminal = self.api._sync_ros_navigation_terminal_locked(
                    {"state": "FAILED", "action": "spin"}, now,
                )
        self.assertEqual("FAILED", terminal)
        self.assertIsNotNone(self.api._loop_recovery)
        self.assertEqual("backup_wait_stop", self.api._loop_recovery["phase"])
        self.assertTrue(self.api._loop_reverse_used)
        self.assertEqual(1, self.api._loop_recovery["goal_index"])
        self.assertAlmostEqual(math.pi, self.api._loop_recovery["spin_angle"])

    async def test_loop_spin_without_new_pivot_command_never_backs_up(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        self.api._loop_stage = "turn"
        self.api._loop_leg_index = 1
        self.api._loop_turn_requested = -math.pi
        now = time.time()
        # A last generic angular velocity or stale previous Spin is never
        # proof that the current turn physically attempted rotation.
        self.map_api.ros_control.rotation = {
            "command_age_sec": 0.02, "command_angular_z": -0.4,
            "command_linear_x": 0.0,
            "pivot_command_age_sec": None,
        }
        with patch.object(self.api, "_safe_live_pose", return_value={
            "x": b["x"], "y": b["y"], "yaw": 0.0,
            "received_at": now,
        }):
            with self.api._lock:
                self.api._sync_ros_navigation_terminal_locked(
                    {"state": "FAILED", "action": "spin"}, now,
                )
        self.assertIsNone(self.api._loop_recovery)
        self.assertFalse(self.api._loop_reverse_used)
        self.assertEqual([], self.api.state_response()["loop_goals"])
        self.assertEqual([], self.map_api.ros_control.backup_distances)

    async def test_loop_generic_failure_never_backs_up(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        now = time.time()
        self.map_api.ros_control.rotation = {
            "command_age_sec": 0.1, "command_angular_z": 0.0,
            "command_linear_x": 0.2,
        }
        with self.api._lock:
            self.api._sync_ros_navigation_terminal_locked(
                {"state": "FAILED", "action": "navigate_to_pose"}, now,
            )
        self.assertIsNone(self.api._loop_recovery)
        self.assertEqual([], self.api.state_response()["loop_goals"])
        self.assertEqual([], self.map_api.ros_control.backup_distances)

    async def test_loop_backup_only_one_and_yaw_flip_retry(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        now = time.time()
        self.api._loop_reverse_used = True
        self.api._state["navigation_state"] = "LOOP_RECOVERY_STOPPING"
        self.api._loop_recovery = {
            "phase": "backup_wait_stop",
            "epoch": self.api._loop_epoch,
            "goal_index": 1, "goal": b,
            "failed_pose": {"x": 1.6, "y": 0.5, "yaw": 0.0},
            "spin_angle": math.pi,
        }
        with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
            with patch.object(self.api, "_watchdog_issue_locked", return_value=None):
                await self.api._advance_loop_recovery()
        self.assertEqual([0.35], self.map_api.ros_control.backup_distances)
        self.assertEqual("backing", self.api._loop_recovery["phase"])
        self.api._loop_recovery["phase"] = "spin_wait_stop"
        self.api._state["navigation_state"] = "READY"
        self.api._state["emergency_stop"] = False
        with patch.object(self.api, "_safe_live_pose", return_value={
            "x": 1.6, "y": 0.5, "yaw": 0.0, "received_at": time.time(),
        }):
            with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
                with patch.object(self.api, "_watchdog_issue_locked", return_value=None):
                    await self.api._advance_loop_recovery()
        self.assertEqual([math.pi], self.map_api.ros_control.spin_radians)
        self.assertEqual("turning", self.api._loop_recovery["phase"])
        self.assertTrue(self.api._loop_reverse_used)

    async def test_loop_second_pivot_failure_never_retries_backup(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        self.api._loop_reverse_used = True
        self.api._loop_stage = "turn"
        self.api._loop_leg_index = 1
        now = time.time()
        self.map_api.ros_control.rotation = {
            "pivot_command_age_sec": 0.1, "pivot_command_angular_z": -0.4,
            "pivot_command_linear_x": 0.0,
        }
        with patch.object(self.api, "_safe_live_pose", return_value={
            "x": b["x"], "y": b["y"], "yaw": 0.0, "received_at": now,
        }):
            with self.api._lock:
                result = self.api._sync_ros_navigation_terminal_locked(
                    {"state": "FAILED", "action": "spin"}, now,
                )
        self.assertEqual("FAILED", result)
        self.assertIsNone(self.api._loop_recovery)
        self.assertEqual([], self.api.state_response()["loop_goals"])
        self.assertEqual([], self.map_api.ros_control.backup_distances)

    async def test_loop_backup_requires_encoder_confirmed_stop(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        recovery = {
            "phase": "backup_wait_stop", "epoch": self.api._loop_epoch,
            "goal_index": 1, "goal": b,
            "failed_pose": {"x": 1.6, "y": 0.5, "yaw": 0.0},
            "spin_angle": math.pi, "started_at": time.time(),
        }
        self.api._loop_recovery = recovery
        self.api._loop_reverse_used = True
        self.api._state["navigation_state"] = "LOOP_RECOVERY_STOPPING"
        with patch.object(self.api, "_stop_confirmed_locked", return_value=False):
            await self.api._advance_loop_recovery()
        self.assertEqual("backup_wait_stop", recovery["phase"])
        self.assertEqual([], self.map_api.ros_control.backup_distances)
        await self.api.emergency_stop("test_backup_preempt")
        await self.api._advance_loop_recovery()
        self.assertIsNone(self.api._loop_recovery)
        self.assertEqual([], self.map_api.ros_control.backup_distances)

    async def test_operator_estop_destroys_loop_and_blocks_refill(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        before = self.map_api.ros_control.navigate_calls
        stopped = await self.api.emergency_stop("test_loop_estop")
        self.assertTrue(stopped["emergency_stop"])
        self.assertFalse(stopped["loop_active"])
        self.assertEqual([], stopped["loop_goals"])
        self.assertIsNone(stopped["active_goal"])
        self.assertEqual([], stopped["waypoints"])
        await self.api._advance_loop_leg()
        self.assertEqual(before, self.map_api.ros_control.navigate_calls)
        self.assertEqual([], self.map_api.ros_control.spin_radians)

    def test_loop_reverse_requires_fresh_post_stop_real_displacement(self):
        now = time.time()
        r = {"failed_pose": {"x": 1.0, "y": 0.3, "yaw": 0.0}}
        good = {"x": 0.65, "y": 0.3, "yaw": 0.0, "received_at": now}
        verify = self.api._loop_recovery_reverse_progress
        self.assertTrue(verify(r, good, now=now, after=now - 0.1))
        self.assertFalse(verify(r, good, now=now + 2.0, after=now - 0.1))
        self.assertFalse(verify(r, good, now=now, after=now + 0.01))
        self.assertFalse(verify(r, {**good, "x": 1.4}, now=now, after=now - 0.1))
        self.assertFalse(verify(r, {**good, "received_at": None},
                                now=now, after=now - 0.1))

    async def test_loop_recovery_reapproach_and_spin_resume_preserves_budget(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        recovery = {
            "phase": "reapproach_wait_stop",
            "epoch": self.api._loop_epoch,
            "goal_index": 1, "goal": b,
            "failed_pose": {"x": 1.6, "y": 0.5, "yaw": 0.0},
            "spin_angle": math.pi,
        }
        self.api._loop_recovery = recovery
        self.api._loop_reverse_used = True
        self.api._state["navigation_state"] = "READY"
        with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
            with patch.object(self.api, "_watchdog_issue_locked", return_value=None):
                await self.api._advance_loop_recovery()
        self.assertEqual([b], self.map_api.ros_control.loop_reapproach_calls)
        self.assertEqual("reapproaching", recovery["phase"])

        recovery["phase"] = "spin_wait_stop"
        self.api._state["navigation_state"] = "READY"
        with patch.object(self.api, "_safe_live_pose", return_value={
            "x": b["x"], "y": b["y"], "yaw": 0.0, "received_at": time.time(),
        }):
            with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
                with patch.object(self.api, "_watchdog_issue_locked", return_value=None):
                    await self.api._advance_loop_recovery()
        self.assertEqual([math.pi], self.map_api.ros_control.spin_radians)
        self.assertEqual("turning", recovery["phase"])

        recovery["phase"] = "resume_wait_stop"
        self.api._state["navigation_state"] = "READY"
        with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
            with patch.object(self.api, "_watchdog_issue_locked", return_value=None):
                await self.api._advance_loop_recovery()
        self.assertIsNone(self.api._loop_recovery)
        self.assertTrue(self.api._loop_reverse_used)
        self.assertEqual("drive_pending", self.api.state_response()["loop_stage"])
        self.assertEqual(0, self.api.state_response()["loop_leg_index"])
        self.assertTrue(self.api._loop_pending)
        with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
            with patch.object(self.api, "_watchdog_issue_locked", return_value=None):
                await self.api._advance_loop_leg()
        self.assertEqual("NAVIGATING", self.api.state_response()["navigation_state"])
        self.assertEqual("drive", self.api.state_response()["loop_stage"])
        self.assertEqual(a["x"], self.map_api.ros_control.loop_reapproach_calls[-1]["x"])
        self.assertEqual([], self.map_api.ros_control.backup_distances)

    async def test_loop_planning_is_forward_only_but_regular_goal_remains_original(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        self.assertTrue(self.map_api.ros_control.last_loop_forward)
        await self.api.plan_goal({"goal": b})
        self.assertFalse(self.map_api.ros_control.last_loop_forward)

    def test_recorded_amcl_trace_is_physical_and_invalid_jump_is_discarded(self):
        ros = NavigationRosControl()
        ros.begin_loop_trace()

        def observation(x, y, yaw=0.0):
            q = SimpleNamespace(
                x=0.0, y=0.0, z=math.sin(yaw / 2), w=math.cos(yaw / 2),
            )
            covariance = [0.0] * 36
            return SimpleNamespace(pose=SimpleNamespace(
                pose=SimpleNamespace(
                    position=SimpleNamespace(x=x, y=y),
                    orientation=q,
                ), covariance=covariance,
            ))

        for i in range(14):
            ros._on_amcl_pose(observation(0.3 + i * 0.1, 0.5))
        trace = ros.finish_loop_trace()
        self.assertEqual(14, len(trace))
        self.assertEqual(1.6, round(trace[-1]["x"], 1))
        self.assertEqual(
            trace, self.api._certify_physical_loop_path(
                trace, {"x": 0.3, "y": 0.5}, {"x": 1.6, "y": 0.5},
            ),
        )
        ros.begin_loop_trace()
        ros._on_amcl_pose(observation(0.3, 0.5))
        ros._on_amcl_pose(observation(1.2, 0.5))
        self.assertEqual([], ros.finish_loop_trace())

    def test_physical_path_cache_uses_driven_samples_not_global_plan(self):
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        driven = [
            {"x": 0.3 + i * 0.1, "y": 0.5 + 0.04 * math.sin(i / 4),
             "yaw": 0.0}
            for i in range(14)
        ]
        saved = self.api._certify_physical_loop_path(driven, a, b)
        self.assertEqual(driven, saved)
        self.assertNotEqual(
            saved, self.map_api.ros_control.compute_path(b),
        )
        self.assertEqual([], self.api._certify_physical_loop_path(
            [{**p, "yaw": math.pi} for p in driven], a, b,
        ))
        self.assertEqual([], self.api._certify_physical_loop_path(
            driven[:3], a, b,
        ))

    def test_cached_trace_must_end_close_enough_for_replay_goal_checker(self):
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        safe = [
            {"x": 0.3 + i * 0.1, "y": 0.5, "yaw": 0.0}
            for i in range(11)
        ]  # Ends 0.30m before B, within cache admission margin.
        self.assertEqual(
            safe, self.api._certify_physical_loop_path(safe, a, b),
        )
        too_short = safe[:9]  # Ends 0.50m before B.
        self.assertEqual(
            [], self.api._certify_physical_loop_path(too_short, a, b),
        )

    async def test_physical_arrival_commits_recorded_a_to_b_trace(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        actual = [
            {"x": a["x"] + 0.1 * i,
             "y": a["y"] + 0.02 * math.sin(i / 3), "yaw": 0.0}
            for i in range(14)
        ]
        # No global path is used as a substitute for these actual samples.
        ros = self.map_api.ros_control
        ros.trace = actual
        with self.api._lock:
            self.api._loop_leg_index = 1
            self.api._loop_completed_legs = 1
            self.api._loop_stage = "drive"
            self.api._state["active_goal"] = dict(b)
            self.api._state["navigation_state"] = "NAVIGATING"
            self.api._pending_nav_success = {
                "started_at": time.time(), "processing": False,
            }
        with patch.object(self.api, "_motor_output_enabled", True):
            with patch.object(self.api, "_safe_live_pose", side_effect=lambda: {
                "x": actual[-1]["x"], "y": actual[-1]["y"],
                "yaw": 0.0, "received_at": time.time(),
            }):
                with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
                    await self.api._resolve_pending_nav_success()
        state = self.api.state_response()
        self.assertEqual("READY", state["navigation_state"])
        self.assertEqual("turn_pending", state["loop_stage"])
        self.assertEqual([1], state["loop_cached_directions"])
        self.assertEqual(actual, self.api._loop_path_cache[1])
        self.assertFalse(ros.trace_started)
        self.assertEqual(0, state["loop_cache_hits"])

    async def test_second_a_to_b_reuses_physical_path_without_planner(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        recorded = [
            {"x": 0.3 + i * 0.1, "y": 0.5, "yaw": 0.0}
            for i in range(14)
        ]
        with self.api._lock:
            self.api._loop_path_cache[1] = recorded
            self.api._loop_stage = "drive_pending"
            self.api._loop_leg_index = 1
            self.api._loop_pending = True
            self.api._state["navigation_state"] = "READY"
        plans = self.map_api.ros_control.plan_calls
        with patch.object(self.api, "_safe_live_pose", return_value={
            "x": a["x"], "y": a["y"], "yaw": 0.0,
            "received_at": time.time(),
        }):
            with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
                with patch.object(self.api, "_watchdog_issue_locked", return_value=None):
                    await self.api._advance_loop_leg()
        self.assertEqual(plans, self.map_api.ros_control.plan_calls)
        self.assertEqual(1, len(self.map_api.ros_control.follow_path_calls))
        self.assertEqual(recorded, self.map_api.ros_control.follow_path_calls[-1]["poses"])
        self.assertEqual(1, self.api.state_response()["loop_cache_hits"])
        self.assertEqual([1], self.api.state_response()["loop_cached_directions"])

    async def test_second_b_to_a_reuses_reverse_direction_trace_forward_only(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        recorded = [
            {"x": 1.6 - i * 0.1, "y": 0.5, "yaw": math.pi}
            for i in range(14)
        ]
        with self.api._lock:
            self.api._loop_path_cache[0] = recorded
            self.api._loop_leg_index = 0
            self.api._loop_stage = "drive_pending"
            self.api._loop_pending = True
            self.api._state["navigation_state"] = "READY"
        previous_plans = self.map_api.ros_control.plan_calls
        with patch.object(self.api, "_safe_live_pose", return_value={
            "x": b["x"], "y": b["y"], "yaw": math.pi,
            "received_at": time.time(),
        }):
            with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
                with patch.object(self.api, "_watchdog_issue_locked", return_value=None):
                    await self.api._advance_loop_leg()
        state = self.api.state_response()
        self.assertEqual("drive", state["loop_stage"])
        self.assertEqual([0], state["loop_cached_directions"])
        self.assertEqual(1, state["loop_cache_hits"])
        self.assertEqual(previous_plans, self.map_api.ros_control.plan_calls)
        self.assertEqual(recorded, self.map_api.ros_control.follow_path_calls[-1]["poses"])
        self.assertFalse(self.map_api.ros_control.follow_path_calls[-1]["preempt"])

    async def test_start_misalignment_keeps_learned_path_for_later_lap(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        recorded = [
            {"x": 0.3 + i * 0.1, "y": 0.5, "yaw": 0.0}
            for i in range(14)
        ]
        with self.api._lock:
            self.api._loop_path_cache[1] = recorded
            self.api._loop_stage = "drive_pending"
            self.api._loop_leg_index = 1
            self.api._loop_pending = True
            self.api._state["navigation_state"] = "READY"
        start = {"x": a["x"], "y": a["y"] + 0.37, "yaw": 0.0,
                 "received_at": time.time()}
        with patch.object(self.api, "_safe_live_pose", return_value=start):
            with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
                with patch.object(self.api, "_watchdog_issue_locked", return_value=None):
                    await self.api._advance_loop_leg()
        self.assertEqual([1], self.api.state_response()["loop_cached_directions"])
        self.assertEqual(0, self.api.state_response()["loop_replans"])
        self.assertEqual([], self.map_api.ros_control.follow_path_calls)
        self.assertEqual("drive", self.api.state_response()["loop_stage"])

    async def test_cached_loop_ignores_anonymous_nav2_plan_refresh(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        with self.api._lock:
            self.api._loop_stage = "drive"
            self.api._loop_follow_active = True
            self.api._loop_path_cache[1] = [
                {"x": 0.3 + i * 0.1, "y": 0.5, "yaw": 0.0}
                for i in range(14)
            ]
            self.api._state["planned_path"] = list(self.api._loop_path_cache[1])
        old_plan = list(self.api.state_response()["planned_path"])
        old_calls = self.map_api.ros_control.plan_calls
        self.api.note_nav2_replan_signal()
        self.api.note_replanned_path([
            {"x": 50.0, "y": 50.0, "yaw": 0.0},
            {"x": 51.0, "y": 50.0, "yaw": 0.0},
        ])
        self.assertEqual(old_calls, self.map_api.ros_control.plan_calls)
        self.assertEqual(old_plan, self.api.state_response()["planned_path"])
        self.assertFalse(self.api._replan_refresh_inflight)

    async def test_obstructed_cached_leg_discards_cache_and_replans(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        recorded = [
            {"x": 0.3 + i * 0.1, "y": 0.5, "yaw": 0.0}
            for i in range(14)
        ]
        with self.api._lock:
            self.api._loop_path_cache[1] = recorded
            self.api._loop_stage = "drive_pending"
            self.api._loop_leg_index = 1
            self.api._loop_pending = True
            self.api._state["navigation_state"] = "READY"
        self.map_api.ros_control.cache_valid = False
        plans = self.map_api.ros_control.plan_calls
        with patch.object(self.api, "_safe_live_pose", return_value={
            "x": a["x"], "y": a["y"], "yaw": 0.0,
            "received_at": time.time(),
        }):
            with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
                with patch.object(self.api, "_watchdog_issue_locked", return_value=None):
                    await self.api._advance_loop_leg()
        self.assertEqual(plans + 1, self.map_api.ros_control.plan_calls)
        self.assertEqual([], self.map_api.ros_control.follow_path_calls)
        self.assertEqual([], self.api.state_response()["loop_cached_directions"])
        self.assertEqual(1, self.api.state_response()["loop_replans"])

    async def test_during_cached_drive_replans_only_after_path_is_blocked(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        saved = [
            {"x": 0.3 + i * 0.1, "y": 0.5, "yaw": 0.0}
            for i in range(14)
        ]
        with self.api._lock:
            self.api._loop_path_cache[1] = saved
            self.api._loop_leg_index = 1
            self.api._loop_stage = "drive"
            self.api._loop_follow_active = True
            self.api._loop_active_path = saved
            self.api._state["active_goal"] = b
            self.api._state["navigation_state"] = "NAVIGATING"
        def live():
            return {"x": 0.8, "y": 0.5, "yaw": 0.0,
                    "received_at": time.time()}
        old_plans = self.map_api.ros_control.plan_calls
        with patch.object(self.api, "_safe_live_pose", side_effect=live):
            self.map_api.ros_control.path_valid_results = [True]
            await self.api._monitor_loop_cached_path()
            self.assertEqual(old_plans, self.map_api.ros_control.plan_calls)
            self.assertEqual([], self.map_api.ros_control.follow_path_calls)
            self.map_api.ros_control.path_valid_results = [False, True]
            await self.api._monitor_loop_cached_path()
        self.assertEqual(old_plans + 1, self.map_api.ros_control.plan_calls)
        self.assertEqual(1, len(self.map_api.ros_control.follow_path_calls))
        self.assertTrue(self.map_api.ros_control.follow_path_calls[0]["preempt"])
        self.assertEqual([], self.api.state_response()["loop_cached_directions"])
        self.assertEqual(1, self.api.state_response()["loop_replans"])
        self.assertFalse(self.api.state_response()["emergency_stop"])

    async def test_operator_cancel_sends_stop_before_blocked_operation_lock(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        async def read_json():
            return {}
        request = SimpleNamespace(json=read_json)
        async def action(_):
            return {"ok": True}
        await self.api._operation_lock.acquire()
        try:
            with patch.object(self.api, "_access", return_value=None):
                pending = asyncio.create_task(self.api._mutation(
                    request, action, abort_loop=True,
                ))
                # The API must send Pi STOP without waiting for a slow
                # FollowPath preemption already holding this operation lock.
                for _ in range(50):
                    if any(
                        cmd.get("reason") == "operator_loop_abort_during_preemption"
                        for _, cmd in self.commands
                    ):
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(any(
                    cmd.get("reason") == "operator_loop_abort_during_preemption"
                    for _, cmd in self.commands
                ))
                self.assertFalse(pending.done())
                self.assertEqual([], self.api.state_response()["loop_goals"])
        finally:
            self.api._operation_lock.release()
        result = await asyncio.wait_for(pending, 1.0)
        self.assertEqual({"ok": True}, result)

    async def test_cancel_epoch_during_new_loop_leg_planning_never_dispatches_goal(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        with self.api._lock:
            self.api._loop_stage = "drive_pending"
            self.api._loop_leg_index = 1
            self.api._loop_pending = True
            self.api._state["navigation_state"] = "READY"
        entered, release = threading.Event(), threading.Event()
        original_compute = self.api._compute_path_for_route

        def slow_plan(*args, **kwargs):
            entered.set()
            if not release.wait(2.0):
                raise RuntimeError("loop planning test timeout")
            return original_compute(*args, **kwargs)

        before = self.map_api.ros_control.navigate_calls
        try:
            with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
                with patch.object(self.api, "_watchdog_issue_locked", return_value=None):
                    with patch.object(self.api, "_compute_path_for_route", side_effect=slow_plan):
                        task = asyncio.create_task(self.api._advance_loop_leg())
                        self.assertTrue(await asyncio.to_thread(entered.wait, 1.0))
                        # Slow planning must not block authenticated cancellation.
                        self.assertFalse(self.api._operation_lock.locked())
                        await asyncio.wait_for(self.api.cancel_goal(), 1.0)
                        release.set()
                        await asyncio.wait_for(task, 3.0)
        finally:
            release.set()
        self.assertEqual(before, self.map_api.ros_control.navigate_calls)
        self.assertEqual([], self.api.state_response()["loop_goals"])

    async def test_cancel_epoch_remains_unblocked_during_cache_replan(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        path = [
            {"x": 0.3 + 0.1 * i, "y": 0.5, "yaw": 0.0}
            for i in range(14)
        ]
        with self.api._lock:
            self.api._loop_path_cache[1] = path
            self.api._loop_stage = "drive"
            self.api._loop_leg_index = 1
            self.api._loop_follow_active = True
            self.api._loop_active_path = path
            self.api._state["active_goal"] = b
            self.api._state["navigation_state"] = "NAVIGATING"
        started, release = threading.Event(), threading.Event()
        def delayed_plan(goal, waypoints, *, loop_forward=False):
            started.set()
            release.wait(2.0)
            return self.map_api.ros_control.compute_path(goal)
        self.map_api.ros_control.path_valid_results = [False, True]
        try:
            with patch.object(self.api, "_safe_live_pose", return_value={
                "x": 0.8, "y": 0.5, "yaw": 0.0,
                "received_at": time.time(),
            }):
                with patch.object(self.api, "_compute_path_for_route",
                                  side_effect=delayed_plan):
                    worker = asyncio.create_task(self.api._monitor_loop_cached_path())
                    self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
                    self.assertFalse(self.api._operation_lock.locked())
                    with self.api._lock:
                        self.api._clear_loop_locked()  # authenticated Cancel epoch
                    release.set()
                    await asyncio.wait_for(worker, 3.0)
        finally:
            release.set()
        self.assertEqual([], self.map_api.ros_control.follow_path_calls)
        self.assertEqual([], self.api.state_response()["loop_cached_directions"])
        self.assertFalse(self.api._loop_pending)

    def test_cached_loop_pose_freshness_guard(self):
        recent = {"x": 0.5, "y": 0.3, "yaw": 0.0, "received_at": 100.0}
        self.assertTrue(self.api._fresh_loop_pose(recent, now=100.2))
        for sample in (
            {**recent, "received_at": 99.49},
            {**recent, "received_at": 100.01},
            {**recent, "received_at": float("nan")},
            {**recent, "x": float("inf")},
            {key: value for key, value in recent.items()
             if key != "received_at"},
        ):
            self.assertFalse(self.api._fresh_loop_pose(sample, now=100.0))

    async def test_stale_pose_cannot_trigger_cached_follow_path(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        recorded = [
            {"x": 0.3 + 0.1*i, "y": 0.5, "yaw": 0.0}
            for i in range(14)
        ]
        with self.api._lock:
            self.api._loop_path_cache[1] = recorded
            self.api._loop_leg_index = 1
            self.api._loop_stage = "drive_pending"
            self.api._loop_pending = True
            self.api._state["navigation_state"] = "READY"
        old_plans = self.map_api.ros_control.plan_calls
        with patch.object(self.api, "_safe_live_pose", return_value={
            **a, "received_at": time.time() - 2.0,
        }):
            with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
                with patch.object(self.api, "_watchdog_issue_locked", return_value=None):
                    await self.api._advance_loop_leg()
        self.assertEqual([], self.map_api.ros_control.follow_path_calls)
        self.assertEqual(old_plans + 1, self.map_api.ros_control.plan_calls)
        self.assertEqual([1], self.api.state_response()["loop_cached_directions"])

    async def test_stale_pose_during_cached_drive_triggers_estop(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        recorded = [
            {"x": 0.3 + 0.1*i, "y": 0.5, "yaw": 0.0}
            for i in range(14)
        ]
        with self.api._lock:
            self.api._loop_path_cache[1] = recorded
            self.api._loop_active_path = recorded
            self.api._loop_leg_index = 1
            self.api._loop_stage = "drive"
            self.api._loop_follow_active = True
            self.api._state["active_goal"] = b
            self.api._state["navigation_state"] = "NAVIGATING"
        with patch.object(self.api, "_safe_live_pose", return_value={
            "x": 0.8, "y": 0.5, "yaw": 0.0,
            "received_at": time.time() - 2.0,
        }):
            await self.api._monitor_loop_cached_path()
        self.assertTrue(self.api.state_response()["emergency_stop"])
        self.assertEqual(0, self.map_api.ros_control.validity_queries)

    async def test_cached_drive_validation_fails_closed_if_no_safe_replan(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        saved = [
            {"x": 0.3 + i * 0.1, "y": 0.5, "yaw": 0.0}
            for i in range(14)
        ]
        with self.api._lock:
            self.api._loop_path_cache[1] = saved
            self.api._loop_leg_index = 1
            self.api._loop_stage = "drive"
            self.api._loop_follow_active = True
            self.api._loop_active_path = saved
            self.api._state["active_goal"] = b
            self.api._state["navigation_state"] = "NAVIGATING"
        self.map_api.ros_control.cache_valid = False
        with patch.object(self.api, "_safe_live_pose", return_value={
            "x": 0.8, "y": 0.5, "yaw": 0.0,
            "received_at": time.time(),
        }):
            await self.api._monitor_loop_cached_path()
        self.assertTrue(self.api.state_response()["emergency_stop"])
        self.assertEqual([], self.api.state_response()["loop_cached_directions"])
        self.assertEqual([], self.map_api.ros_control.follow_path_calls)

    async def test_controller_abort_replans_only_once_after_confirmed_stop(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        with self.api._lock:
            self.api._loop_leg_index = 1
            self.api._loop_stage = "drive"
            self.api._loop_follow_active = True
            self.api._loop_path_cache[1] = [
                {"x": 0.3 + 0.1*i, "y": 0.5, "yaw": 0.0}
                for i in range(14)
            ]
        with self.api._lock:
            outcome = self.api._sync_ros_navigation_terminal_locked(
                {"state": "FAILED", "action": "follow_path"}, time.time(),
            )
        self.assertEqual("FAILED", outcome)
        self.assertEqual("READY", self.api.state_response()["navigation_state"])
        self.assertEqual("drive_pending", self.api.state_response()["loop_stage"])
        self.assertTrue(self.api._loop_pending)
        self.assertTrue(self.api._loop_failed_follow_retry)
        self.assertEqual([], self.api.state_response()["loop_cached_directions"])
        self.assertIsNotNone(self.api._pending_terminal_stop)
        before = self.map_api.ros_control.plan_calls
        with patch.object(self.api, "_stop_confirmed_locked", return_value=False):
            await self.api._advance_loop_leg()
        self.assertEqual(before, self.map_api.ros_control.plan_calls)
        with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
            with patch.object(self.api, "_watchdog_issue_locked", return_value=None):
                await self.api._advance_loop_leg()
        self.assertEqual(before + 1, self.map_api.ros_control.plan_calls)
        self.assertFalse(self.api._loop_pending)

    async def test_second_followpath_abort_terminates_loop_without_retry(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        with self.api._lock:
            self.api._loop_leg_index = 1
            self.api._loop_stage = "drive"
            self.api._loop_follow_active = True
            self.api._loop_failed_follow_retry = True
            result = self.api._sync_ros_navigation_terminal_locked(
                {"state": "FAILED", "action": "follow_path"}, time.time(),
            )
        self.assertEqual("FAILED", result)
        self.assertEqual([], self.api.state_response()["loop_goals"])
        self.assertFalse(self.api._loop_pending)

    def test_cache_validation_ignores_traversed_behind_robot_path(self):
        path = [{"x": i / 10, "y": 0.0, "yaw": 0.0} for i in range(20)]
        remaining = self.api._remaining_loop_path(
            path, {"x": 1.1, "y": 0.0},
        )
        self.assertGreaterEqual(remaining[0]["x"], 0.9)
        self.assertEqual(path[-1], remaining[-1])
        self.assertEqual([], self.api._remaining_loop_path(
            path, {"x": 1.1, "y": 2.0},
        ))

    async def test_loop_rejects_goals_with_overlapping_arrival_discs(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.3, "y": 0.5, "yaw": 0.0}
        # A/B distance 1m exceeded old 0.5m check but both 0.55m
        # arrival discs overlapped and could trigger an immediate false Goal.
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        self.assertEqual("INVALID_LOOP", raised.exception.error_code)
        self.assertEqual([], self.api.state_response()["loop_goals"])

    def test_loop_spin_rejects_translation_and_wrong_signed_turn(self):
        now = time.time()
        origin = {"x": 1.0, "y": 0.5, "yaw": math.pi}
        drift = {"x": 1.20, "y": 0.5, "yaw": math.pi, "received_at": now}
        self.assertTrue(self.api._loop_pose_confirmed(
            origin, drift, need_yaw=False, now=now, after=now - 0.1,
        ))
        self.assertFalse(self.api._loop_pose_confirmed(
            origin, drift, need_yaw=True, now=now, after=now - 0.1,
        ))
        good = {"signed_yaw_rad": math.pi - 0.05, "samples": 50,
                "age_sec": 0.8, "invalid": False}
        verify = self.api._loop_signed_turn_confirmed
        self.assertTrue(verify(good, math.pi))
        self.assertFalse(verify({**good, "signed_yaw_rad": -math.pi}, math.pi))
        self.assertFalse(verify({**good, "samples": 1}, math.pi))
        self.assertFalse(verify({**good, "age_sec": 3.1}, math.pi))
        self.assertFalse(verify({**good, "invalid": True}, math.pi))
        self.assertFalse(verify(good, -math.pi))

    async def test_physical_spin_wrong_direction_blocks_next_loop_leg(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        with self.api._lock:
            self.api._loop_stage = "turn"
            self.api._loop_turn_requested = -math.pi
            self.api._loop_leg_index = 1
            self.api._state["active_goal"] = {
                "x": b["x"], "y": b["y"], "yaw": -math.pi,
            }
            self.api._pending_nav_success = {
                "started_at": time.time(), "processing": False,
            }
        with patch.object(self.api, "_motor_output_enabled", True):
            with patch.object(self.api, "_safe_live_pose", side_effect=lambda: {
                "x": b["x"], "y": b["y"], "yaw": math.pi,
                "received_at": time.time(),
            }):
                with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
                    with patch.object(self.map_api.ros_control, "spin_progress",
                                      create=True, return_value={
                                          "signed_yaw_rad": math.pi,
                                          "samples": 40,
                                          "age_sec": 0.3,
                                          "invalid": False,
                                      }):
                        await self.api._resolve_pending_nav_success()
        state = self.api.state_response()
        self.assertEqual("FAILED", state["navigation_state"])
        self.assertEqual([], state["loop_goals"])
        self.assertNotEqual("drive_pending", state["loop_stage"])
        self.assertEqual(1, self.map_api.ros_control.navigate_calls)

    async def test_physical_spin_matching_direction_unlocks_loop_leg(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        with self.api._lock:
            self.api._loop_stage = "turn"
            self.api._loop_turn_requested = -math.pi
            self.api._loop_leg_index = 1
            self.api._loop_completed_legs = 2
            self.api._state["active_goal"] = {
                "x": b["x"], "y": b["y"], "yaw": -math.pi,
            }
            self.api._pending_nav_success = {
                "started_at": time.time(), "processing": False,
            }
        with patch.object(self.api, "_motor_output_enabled", True):
            with patch.object(self.api, "_safe_live_pose", side_effect=lambda: {
                "x": b["x"], "y": b["y"], "yaw": -math.pi,
                "received_at": time.time(),
            }):
                with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
                    with patch.object(self.map_api.ros_control, "spin_progress",
                                      create=True, return_value={
                                          "signed_yaw_rad": -math.pi + 0.02,
                                          "samples": 40,
                                          "age_sec": 0.3,
                                          "invalid": False,
                                      }):
                        await self.api._resolve_pending_nav_success()
        state = self.api.state_response()
        self.assertEqual("READY", state["navigation_state"])
        self.assertEqual("drive_pending", state["loop_stage"])
        self.assertTrue(state["loop_pending"])
        self.assertEqual(0, state["loop_leg_index"])
        self.assertFalse(self.api._loop_next_turn_clockwise)

    async def test_physical_recovery_reapproach_does_not_require_spin_proof(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        # This value is deliberately left over from the failed first Spin.
        # Recovery phases must take priority over that old loop stage.
        with self.api._lock:
            self.api._loop_stage = "turn"
            self.api._loop_recovery = {
                "phase": "reapproaching", "epoch": self.api._loop_epoch,
                "goal_index": 1, "goal": b,
                "failed_pose": b, "spin_angle": -math.pi,
                "started_at": time.time(),
            }
            self.api._state["active_goal"] = b
            self.api._pending_nav_success = {
                "started_at": time.time(), "processing": False,
            }
        with patch.object(self.api, "_motor_output_enabled", True):
            with patch.object(self.api, "_safe_live_pose", side_effect=lambda: {
                "x": b["x"], "y": b["y"], "yaw": 1.0,
                "received_at": time.time(),
            }):
                with patch.object(self.api, "_stop_confirmed_locked", return_value=True):
                    await self.api._resolve_pending_nav_success()
        self.assertEqual("READY", self.api.state_response()["navigation_state"])
        self.assertEqual("spin_wait_stop", self.api._loop_recovery["phase"])
        self.assertEqual(1, self.map_api.ros_control.navigate_calls)

    async def test_loop_pending_stage_timeout_fails_closed(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 1.6, "y": 0.5, "yaw": 0.0}
        await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        await self.api.start_navigation()
        with self.api._lock:
            self.api._loop_stage = "turn_pending"
            self.api._loop_pending = True
            self.api._loop_pending_since = (
                time.time() - self.api.LOOP_STAGE_PENDING_TIMEOUT_SEC - 1
            )
            self.api._state["active_goal"] = None
            self.api._state["navigation_state"] = "READY"
        await self.api._advance_loop_leg()
        state = self.api.state_response()
        self.assertEqual("FAILED", state["navigation_state"])
        self.assertEqual("LOOP_STAGE_WAIT_TIMEOUT", state["last_error"])
        self.assertFalse(state["loop_active"])
        self.assertEqual([], self.map_api.ros_control.spin_radians)
        self.assertIsNone(state["active_goal"])
        self.assertIsNotNone(self.api._pending_terminal_stop)

    def test_ros_spin_odometry_unwraps_and_rejects_direction_reversal(self):
        ros = NavigationRosControl()
        ros._navigate_action_kind = "spin"
        ros._navigation_state = "NAVIGATING"
        ros._spin_last_odom_yaw = math.pi - 0.2
        for angle in (math.pi - 0.1, -math.pi + 0.05, -math.pi + 0.2):
            pose = SimpleNamespace(
                pose=SimpleNamespace(orientation=SimpleNamespace(
                    x=0.0, y=0.0, z=math.sin(angle / 2),
                    w=math.cos(angle / 2),
                ))
            )
            ros._on_odom(SimpleNamespace(pose=pose))
        progress = ros.spin_progress()
        self.assertEqual(3, progress["samples"])
        self.assertAlmostEqual(0.4, progress["signed_yaw_rad"], places=5)
        self.assertFalse(progress["invalid"])
        # A 1.2 rad jump is not legitimate short-interval yaw motion.
        angle = -math.pi + 1.4
        pose = SimpleNamespace(pose=SimpleNamespace(
            pose=SimpleNamespace(orientation=SimpleNamespace(
                x=0.0, y=0.0, z=math.sin(angle / 2),
                w=math.cos(angle / 2),
            ))
        ))
        ros._on_odom(pose)
        self.assertTrue(ros.spin_progress()["invalid"])

    async def test_loop_rejects_overlapping_a_b(self):
        await self.driving_ready()
        a = {"x": 0.3, "y": 0.5, "yaw": 0.0}
        b = {"x": 0.4, "y": 0.5, "yaw": 0.0}
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.plan_goal({"goal": a, "loop_goals": [a, b]})
        self.assertEqual("INVALID_LOOP", raised.exception.error_code)
        self.assertEqual(0, self.map_api.ros_control.navigate_calls)

    async def test_intermediate_waypoint_yaw_is_not_required(self):
        await self.driving_ready()
        request = {
            "goal": {"x": 1.5, "y": 1.0, "yaw": 0.2},
            "waypoints": [{"x": 0.5, "y": 0.4, "yaw": 99.0}],
        }
        response = await self.api.plan_goal(request)
        self.assertEqual("PATH_READY", response["navigation_state"])
        self.assertNotEqual(99.0, response["waypoints"][0]["yaw"])
        self.assertEqual(0.2, response["active_goal"]["yaw"])

    async def test_invalid_waypoint_rejected_without_canceling_running_goal(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.plan_goal({
                "goal": {"x": 1.5, "y": 1.5, "yaw": 0.0},
                "waypoints": [{"x": 300.0, "y": 0.0}],
            })
        self.assertEqual("GOAL_OUT_OF_BOUNDS", raised.exception.error_code)
        self.assertEqual(0, self.map_api.ros_control.cancel_calls)
        self.assertEqual("NAVIGATING", self.api.state_response()["navigation_state"])

    async def test_passed_waypoint_progress_culls_route_without_dropping_final(self):
        await self.driving_ready()
        await self.api.plan_goal({
            "goal": {"x": 1.7, "y": 1.3, "yaw": 0.4},
            "waypoints": [{"x": 0.5, "y": 0.5}, {"x": 1.0, "y": 0.8}],
        })
        await self.api.start_navigation()
        with self.api._lock:
            self.api._sync_waypoint_progress_locked({
                "action": "navigate_through_poses",
                "number_of_poses_remaining": 2,
            })
        state = self.api.state_response()
        self.assertEqual(1, len(state["waypoints"]))
        self.assertEqual(1.0, state["waypoints"][0]["x"])
        self.assertEqual(1.7, state["active_goal"]["x"])
        with self.api._lock:
            self.api._sync_waypoint_progress_locked({
                "action": "navigate_through_poses",
                "number_of_poses_remaining": 1,
            })
        self.assertEqual([], self.api.state_response()["waypoints"])
        self.assertIsNotNone(self.api.state_response()["active_goal"])

    async def test_stale_waypoint_count_never_reinstates_removed_points(self):
        await self.driving_ready()
        await self.api.plan_goal({
            "goal": {"x": 1.7, "y": 1.3, "yaw": 0.4},
            "waypoints": [{"x": 0.5, "y": 0.5}, {"x": 1.0, "y": 0.8}],
        })
        await self.api.start_navigation()
        with self.api._lock:
            self.api._sync_waypoint_progress_locked({
                "action": "navigate_through_poses",
                "number_of_poses_remaining": 2,
            })
            self.api._sync_waypoint_progress_locked({
                "action": "navigate_through_poses",
                "number_of_poses_remaining": 3,
            })
            self.api._sync_waypoint_progress_locked({
                "action": "navigate_to_pose",
                "number_of_poses_remaining": 1,
            })
        self.assertEqual(1, len(self.api.state_response()["waypoints"]))

    async def test_emergency_stop_preserves_completed_waypoint_progress(self):
        await self.driving_ready()
        await self.api.plan_goal({
            "goal": {"x": 1.7, "y": 1.3, "yaw": -0.2},
            "waypoints": [{"x": 0.5, "y": 0.5}, {"x": 1.0, "y": 0.8}],
        })
        await self.api.start_navigation()
        original_status = self.map_api.ros_control.navigation_status
        self.map_api.ros_control.navigation_status = lambda: {
            **original_status(),
            "action": "navigate_through_poses",
            "number_of_poses_remaining": 2,
        }
        result = await self.api.emergency_stop("operator")
        self.assertTrue(result["emergency_stop"])
        self.assertEqual(1, len(result["waypoints"]))
        self.assertEqual(1.0, result["waypoints"][0]["x"])
        self.assertAlmostEqual(-0.2, result["active_goal"]["yaw"])

    async def test_manual_final_xy_cannot_bypass_pending_intermediates(self):
        await self.driving_ready()
        await self.api.plan_goal({
            "goal": {"x": 1.5, "y": 1.2, "yaw": 0.0},
            "waypoints": [{"x": 0.5, "y": 0.4}],
        })
        self.api.note_pi_status({"mode": "manual", "navigation_mode": "driving"})
        self.api.note_navigation_sample(
            "pose", payload={"x": 1.5, "y": 1.2, "yaw": 0.0},
        )
        state = self.api.state_response()
        self.assertIsNotNone(state["active_goal"])
        self.assertEqual("PATH_READY", state["navigation_state"])

    async def test_bad_waypoint_numbers_are_rejected(self):
        await self.driving_ready()
        for bad in ([{"x": True, "y": 0}], [{"x": "nan", "y": 0}], "not-a-list"):
            with self.subTest(bad=bad):
                with self.assertRaises(NavigationControlError) as raised:
                    await self.api.plan_goal({
                        "goal": {"x": 1.5, "y": 1.0, "yaw": 0.0},
                        "waypoints": bad,
                    })
                self.assertEqual("INVALID_WAYPOINTS", raised.exception.error_code)
        self.assertEqual(0, self.map_api.ros_control.plan_calls)

    async def test_goal_terminal_and_manual_tolerance_rules(self):
        await self.driving_ready()
        await self.api.plan_goal({"goal": {"x": 1.0, "y": 1.0, "yaw": 0.0}})
        await self.api.start_navigation()
        self.map_api.ros_control.state = "FAILED"
        failed = self.api.state_response()
        self.assertEqual("FAILED", failed["navigation_state"])
        self.assertIsNotNone(failed["active_goal"])
        self.assertFalse(self.api.motion_allowed())
        self.assertIsNone(self.api.motion_safety_context())
        await self.api.evaluate_watchdog()
        await self.confirm_pending_stop_for_test()

        with self.api._lock:
            self.api._state["navigation_state"] = "NAVIGATING"
        self.map_api.ros_control.state = "SUCCEEDED"
        pending = self.api.state_response()
        self.assertEqual("NAVIGATING", pending["navigation_state"])
        self.assertIsNotNone(pending["active_goal"])
        finishing = asyncio.create_task(self.api.evaluate_watchdog())
        await asyncio.sleep(0.02)
        await self.confirm_pending_stop_for_test()
        self.assertIsNone(await finishing)
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
        with self.api._lock:
            self.assertIsNotNone(self.api._stop_commanded_at)
            self.assertEqual((0, 0, 0, 0), self.api._stop_encoder_ticks)

    async def test_motion_after_resume_without_command_triggers_estop(self):
        await self.api.emergency_stop("operator")
        await self.api.resume_navigation()
        self.api.note_encoder(dict.fromkeys((
            "left_front_ticks", "right_front_ticks", "left_rear_ticks", "right_rear_ticks"
        ), 4))
        self.assertEqual("ENCODER_MOVEMENT_AFTER_STOP", await self.api.evaluate_watchdog())
        self.assertTrue(self.api.state_response()["emergency_stop"])

    async def test_authorized_motion_releases_post_resume_baseline(self):
        await self.api.emergency_stop("operator")
        await self.api.resume_navigation()
        self.assertTrue(self.api.note_authorized_motion())
        self.api.note_encoder(dict.fromkeys((
            "left_front_ticks", "right_front_ticks", "left_rear_ticks", "right_rear_ticks"
        ), 4))
        self.assertIsNone(await self.api.evaluate_watchdog())

    async def test_mapping_transition_clears_stale_visualization(self):
        await self.driving_ready()
        resets = []
        self.api._clear_visualization = lambda: resets.append(True)

        mapped = await self.api.switch_mode({"mode": "MAPPING"})

        self.assertEqual([True], resets)
        self.assertEqual("MAPPING", mapped["navigation_mode"])
        self.assertIsNone(mapped["active_map"])
        self.assertFalse(mapped["localization_ready"])
        self.assertFalse(mapped["nav2_ready"])

    async def test_mapping_restart_is_forwarded_to_process_control(self):
        mapped = await self.api.switch_mode(
            {"mode": "MAPPING", "restart": True}
        )

        self.assertEqual("MAPPING", mapped["navigation_mode"])
        self.assertEqual(("MAPPING", None, True), self.process.transitions[-1])


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

    async def test_blocked_navigation_fails_after_five_seconds_without_estop(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        self.api.note_navigation_sample(
            "pose",
            now=base,
            payload={"x": 0.0, "y": 0.0, "yaw": 0.0},
        )
        self.api.note_pi_status(
            {"mode": "auto", "navigation_mode": "driving", "emergency_stop": False},
            now=base + 5.1,
        )
        self.api.note_navigation_sample("scan", now=base + 5.1)

        issue = await self.api.evaluate_watchdog(now=base + 5.1)

        self.assertEqual("BLOCKED_TIMEOUT", issue)
        current = self.api.state_response(now=base + 5.1)
        self.assertEqual("FAILED", current["navigation_state"])
        self.assertEqual("BLOCKED_TIMEOUT", current["last_error"])
        self.assertFalse(current["emergency_stop"])
        self.assertIsNotNone(current["active_goal"])
        self.assertEqual("stop", self.commands[-1][1]["type"])
        self.assertEqual("navigation_blocked_timeout", self.commands[-1][1]["reason"])
        self.assertGreaterEqual(self.map_api.ros_control.cancel_calls, 1)

    async def test_ten_centimeter_translation_resets_five_second_progress_timer(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        with self.api._lock:
            self.api._start_navigation_progress_locked({"x": 0.0, "y": 0.0}, base)
        self.api.note_navigation_sample("pose", now=base + 4.0, payload={"x": 0.10, "y": 0.0})
        self.refresh_watchdog_samples(base + 5.1)
        self.assertIsNone(await self.api.evaluate_watchdog(now=base + 5.1))
        self.refresh_watchdog_samples(base + 9.1)
        self.assertEqual("BLOCKED_TIMEOUT", await self.api.evaluate_watchdog(now=base + 9.1))

    async def test_rotation_does_not_mask_blocked_translation(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        self.api.note_navigation_sample(
            "pose",
            now=base,
            payload={"x": 0.0, "y": 0.0, "yaw": 0.0},
        )
        self.api.note_navigation_sample(
            "pose",
            now=base + 4.0,
            payload={"x": 0.0, "y": 0.0, "yaw": 0.40},
        )
        self.api.note_pi_status(
            {"mode": "auto", "navigation_mode": "driving", "emergency_stop": False},
            now=base + 5.1,
        )
        self.api.note_navigation_sample("scan", now=base + 5.1)

        issue = await self.api.evaluate_watchdog(now=base + 5.1)

        self.assertEqual("BLOCKED_TIMEOUT", issue)
        self.assertEqual("FAILED", self.api.state_response(now=base + 5.1)["navigation_state"])

    async def test_fresh_nav2_command_and_odom_rotation_extend_progress_until_cap(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        with self.api._lock:
            self.api._start_navigation_progress_locked({"x": 0.0, "y": 0.0}, base)

        for sequence, elapsed in enumerate((4.0, 8.0, 12.0), start=1):
            self.set_rotation(sequence)
            self.refresh_watchdog_samples(base + elapsed)
            self.assertIsNone(await self.api.evaluate_watchdog(now=base + elapsed))
        self.assertEqual("NAVIGATING", self.api.state_response()["navigation_state"])

        self.set_rotation(4)
        self.refresh_watchdog_samples(base + 15.0)
        self.assertEqual("BLOCKED_TIMEOUT", await self.api.evaluate_watchdog(now=base + 15.0))

    async def test_translation_resets_cumulative_rotation_allowance(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        with self.api._lock:
            self.api._start_navigation_progress_locked({"x": 0.0, "y": 0.0}, base)
        for sequence, elapsed in enumerate((4.0, 8.0, 12.0), start=1):
            self.set_rotation(sequence)
            self.refresh_watchdog_samples(base + elapsed)
            self.assertIsNone(await self.api.evaluate_watchdog(now=base + elapsed))
        self.api.note_navigation_sample("pose", now=base + 12.1, payload={"x": 0.10, "y": 0.0})
        self.set_rotation(4)
        self.refresh_watchdog_samples(base + 16.0)
        self.assertIsNone(await self.api.evaluate_watchdog(now=base + 16.0))
        self.assertEqual("NAVIGATING", self.api.state_response()["navigation_state"])

    async def test_rotation_command_without_new_odom_yaw_keeps_five_second_timeout(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        with self.api._lock:
            self.api._start_navigation_progress_locked({"x": 0.0, "y": 0.0}, base)
        self.set_rotation(1, yaw_delta=0.0)
        self.refresh_watchdog_samples(base + 5.1)
        self.assertEqual("BLOCKED_TIMEOUT", await self.api.evaluate_watchdog(now=base + 5.1))

    async def test_old_yaw_change_cannot_be_reused_for_new_rotation_progress(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        with self.api._lock:
            self.api._start_navigation_progress_locked({"x": 0.0, "y": 0.0}, base)
        self.set_rotation(1, yaw=0.04)
        self.refresh_watchdog_samples(base + 4.0)
        self.assertIsNone(await self.api.evaluate_watchdog(now=base + 4.0))
        self.set_rotation(2, yaw=0.04)
        self.refresh_watchdog_samples(base + 9.1)
        self.assertEqual("BLOCKED_TIMEOUT", await self.api.evaluate_watchdog(now=base + 9.1))

    async def test_stale_rotation_command_does_not_extend_progress(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        with self.api._lock:
            self.api._start_navigation_progress_locked({"x": 0.0, "y": 0.0}, base)
        self.set_rotation(1, command_age=0.51)
        self.refresh_watchdog_samples(base + 5.1)
        self.assertEqual("BLOCKED_TIMEOUT", await self.api.evaluate_watchdog(now=base + 5.1))

    async def test_stale_odometry_does_not_extend_progress(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        with self.api._lock:
            self.api._start_navigation_progress_locked({"x": 0.0, "y": 0.0}, base)
        self.set_rotation(1, odom_age=0.51)
        self.refresh_watchdog_samples(base + 5.1)
        self.assertEqual("BLOCKED_TIMEOUT", await self.api.evaluate_watchdog(now=base + 5.1))

    async def test_opposite_yaw_does_not_extend_rotation_progress(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        with self.api._lock:
            self.api._start_navigation_progress_locked({"x": 0.0, "y": 0.0}, base)
        self.set_rotation(1, angular=-0.4)
        self.refresh_watchdog_samples(base + 5.1)
        self.assertEqual("BLOCKED_TIMEOUT", await self.api.evaluate_watchdog(now=base + 5.1))

    async def test_linear_nav2_command_does_not_extend_rotation_progress(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        with self.api._lock:
            self.api._start_navigation_progress_locked({"x": 0.0, "y": 0.0}, base)
        self.set_rotation(1, linear=0.1)
        self.refresh_watchdog_samples(base + 5.1)
        self.assertEqual("BLOCKED_TIMEOUT", await self.api.evaluate_watchdog(now=base + 5.1))

    async def test_resuming_does_not_trigger_no_progress_timeout(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        base = time.time()
        with self.api._lock:
            self.api._state["navigation_state"] = "RESUMING"
            self.api._start_navigation_progress_locked(
                {"x": 0.0, "y": 0.0},
                base,
            )
        self.api.note_pi_status(
            {"mode": "auto", "navigation_mode": "driving", "emergency_stop": False},
            now=base + 5.1,
        )
        self.api.note_navigation_sample("scan", now=base + 5.1)

        issue = await self.api.evaluate_watchdog(now=base + 5.1)

        self.assertIsNone(issue)
        self.assertEqual("RESUMING", self.api.state_response(now=base + 5.1)["navigation_state"])
        self.assertEqual(0, self.map_api.ros_control.cancel_calls)

    async def test_terminal_result_during_blocked_cancel_wins_over_timeout(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        self.api.note_navigation_sample(
            "pose",
            now=base,
            payload={"x": 0.0, "y": 0.0, "yaw": 0.0},
        )
        self.api.note_pi_status(
            {"mode": "auto", "navigation_mode": "driving", "emergency_stop": False},
            now=base + 5.1,
        )
        self.api.note_navigation_sample("scan", now=base + 5.1)

        def completes_while_canceling():
            self.map_api.ros_control.cancel_calls += 1
            self.map_api.ros_control.state = "SUCCEEDED"
            return {
                "requested": False,
                "confirmed": True,
                "completed": True,
                "terminal": "SUCCEEDED",
            }

        self.map_api.ros_control.cancel_navigation = completes_while_canceling

        issue = await self.api.evaluate_watchdog(now=base + 5.1)

        self.assertIsNone(issue)
        pending = self.api.state_response()
        self.assertEqual("NAVIGATING", pending["navigation_state"])
        finishing = asyncio.create_task(self.api.evaluate_watchdog())
        await asyncio.sleep(0.02)
        await self.confirm_pending_stop_for_test()
        self.assertIsNone(await finishing)
        current = self.api.state_response()
        self.assertEqual("SUCCEEDED", current["navigation_state"])
        self.assertIsNone(current["active_goal"])
        self.assertFalse(current["emergency_stop"])
        self.assertFalse(
            any(command.get("reason") == "navigation_blocked_timeout" for _, command in self.commands)
        )

    async def test_user_navigation_mutation_wins_blocked_watchdog_race(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        self.api.note_navigation_sample(
            "pose",
            now=base,
            payload={"x": 0.0, "y": 0.0, "yaw": 0.0},
        )
        self.api.note_pi_status(
            {"mode": "auto", "navigation_mode": "driving", "emergency_stop": False},
            now=base + 5.1,
        )
        self.api.note_navigation_sample("scan", now=base + 5.1)

        await self.api._operation_lock.acquire()
        try:
            watchdog_task = asyncio.create_task(
                self.api.evaluate_watchdog(now=base + 5.1)
            )
            await asyncio.sleep(0)
            with self.api._lock:
                self.api._state["navigation_state"] = "READY"
                self.api._state["active_goal"] = None
                self.api._state["planned_path"] = []
        finally:
            self.api._operation_lock.release()

        issue = await watchdog_task

        self.assertIsNone(issue)
        self.assertEqual("READY", self.api.state_response(now=base + 5.1)["navigation_state"])
        self.assertEqual(0, self.map_api.ros_control.cancel_calls)
        self.assertFalse(self.api.state_response(now=base + 5.1)["emergency_stop"])

    async def test_nav2_cancel_exception_is_not_a_physical_path_estop(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        self.api.note_navigation_sample(
            "pose", now=base, payload={"x": 0.0, "y": 0.0, "yaw": 0.0},
        )
        self.refresh_watchdog_samples(base + 5.1)

        def reject_cancel():
            raise RuntimeError("Nav2 cancel transport failure")

        self.map_api.ros_control.cancel_navigation = reject_cancel
        self.assertEqual(
            "BLOCKED_TIMEOUT",
            await self.api.evaluate_watchdog(now=base + 5.1),
        )
        result = self.api.state_response()
        self.assertEqual("FAILED", result["navigation_state"])
        self.assertEqual("NAV_CANCEL_FAILED", result["last_error"])
        self.assertFalse(result["emergency_stop"])
        self.assertFalse(self.api.motion_allowed())
        self.assertTrue(any(
            command["type"] == "stop" for _, command in self.commands
        ))
        self.assertFalse(any(
            command["type"] == "emergency_stop" for _, command in self.commands
        ))

    async def test_blocked_cancel_error_after_estop_does_not_reclassify_failure(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        self.api.note_navigation_sample(
            "pose",
            now=base,
            payload={"x": 0.0, "y": 0.0, "yaw": 0.0},
        )
        self.api.note_pi_status(
            {"mode": "auto", "navigation_mode": "driving", "emergency_stop": False},
            now=base + 5.1,
        )
        self.api.note_navigation_sample("scan", now=base + 5.1)

        def canceled_by_estop():
            with self.api._lock:
                self.api._state["emergency_stop"] = True
                self.api._state["emergency_reason"] = "operator"
                self.api._state["navigation_state"] = "EMERGENCY_STOPPED"
            raise RuntimeError("duplicate cancel rejected")

        self.map_api.ros_control.cancel_navigation = canceled_by_estop

        issue = await self.api.evaluate_watchdog(now=base + 5.1)

        self.assertIsNone(issue)
        current = self.api.state_response(now=base + 5.1)
        self.assertTrue(current["emergency_stop"])
        self.assertEqual("EMERGENCY_STOPPED", current["navigation_state"])
        self.assertEqual("operator", current["emergency_reason"])

    async def test_watchdog_syncs_ros_success_without_state_polling(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        self.api.note_navigation_sample(
            "pose",
            now=base,
            payload={"x": 0.0, "y": 0.0, "yaw": 0.0},
        )
        self.map_api.ros_control.state = "SUCCEEDED"

        pending = self.api.state_response()
        self.assertEqual("NAVIGATING", pending["navigation_state"])
        finishing = asyncio.create_task(self.api.evaluate_watchdog())
        await asyncio.sleep(0.02)
        await self.confirm_pending_stop_for_test()
        self.assertIsNone(await finishing)
        self.map_api.ros_control.state = "NAVIGATING"
        current = self.api.state_response()
        self.assertEqual("SUCCEEDED", current["navigation_state"])
        self.assertIsNone(current["active_goal"])

    async def test_ros_success_never_reports_success_without_fresh_stop(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        self.api.STOP_CONFIRM_TIMEOUT_SEC = 0.10
        self.map_api.ros_control.state = "SUCCEEDED"

        interim = self.api.state_response()
        self.assertEqual("NAVIGATING", interim["navigation_state"])
        self.assertIsNotNone(interim["active_goal"])
        self.assertFalse(self.api.motion_allowed())

        self.assertIsNone(await self.api.evaluate_watchdog())
        result = self.api.state_response()
        self.assertEqual("FAILED", result["navigation_state"])
        self.assertEqual("STOP_NOT_CONFIRMED", result["last_error"])
        self.assertIsNotNone(result["active_goal"])
        self.assertFalse(result["emergency_stop"])
        self.assertFalse(self.api.motion_allowed())

    async def test_ros_success_stop_delivery_failure_is_not_success(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        original = self.api._send_robot_command

        async def deny_stop(robot_id, command):
            if command["type"] == "stop":
                return False
            return await original(robot_id, command)

        self.api._send_robot_command = deny_stop
        self.map_api.ros_control.state = "SUCCEEDED"
        self.assertEqual("NAVIGATING", self.api.state_response()["navigation_state"])
        self.assertIsNone(await self.api.evaluate_watchdog())
        result = self.api.state_response()
        self.assertEqual("FAILED", result["navigation_state"])
        self.assertEqual("STOP_NOT_DELIVERED", result["last_error"])
        self.assertTrue(result["active_goal"])
        self.assertFalse(result["emergency_stop"])

    async def test_pi_loss_while_navigating_stops_without_automatic_estop(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        self.api.note_pi_connection(False)
        issue = await self.api.evaluate_watchdog(now=base)
        self.assertEqual("PI_CONNECTION_LOSS", issue)
        current = self.api.state_response()
        self.assertFalse(current["emergency_stop"])
        self.assertEqual("NAVIGATING", current["navigation_state"])
        self.assertIsNotNone(current["active_goal"])
        self.assertFalse(self.api.motion_allowed())
        self.assertTrue(any(c["type"] == "stop" for _, c in self.commands))
        self.assertEqual(1, self.map_api.ros_control.cancel_calls)

        # Pi never recovered: end the bounded recovery episode as FAILED.
        self.assertIsNone(await self.api.evaluate_watchdog(now=base + 5.1))
        current = self.api.state_response()
        self.assertEqual("FAILED", current["navigation_state"])
        self.assertEqual(
            "SENSOR_RECOVERY_TIMEOUT:PI_CONNECTION_LOSS", current["last_error"],
        )
        self.assertFalse(current["emergency_stop"])
        self.assertFalse(self.api.motion_allowed())

    async def test_lidar_loss_recovers_after_stable_samples_and_new_path(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        self.api.note_navigation_sample("scan", now=base - 6.0)
        self.assertEqual(
            "LIDAR_DATA_LOSS",
            await self.api.evaluate_watchdog(now=base),
        )
        self.assertFalse(self.api.motion_allowed())
        self.assertEqual(1, self.map_api.ros_control.cancel_calls)

        # A restored sensor does not re-enable motor output until the Pi STOP
        # is proven with fresh, stable post-command wheel encoder samples.
        self.refresh_watchdog_samples(base + 0.1)
        self.assertIsNone(await self.api.evaluate_watchdog(now=base + 0.1))
        self.assertEqual("NAVIGATING", self.api.state_response()["navigation_state"])
        self.assertFalse(self.api.motion_allowed())
        await self.confirm_pending_stop_for_test()

        self.refresh_watchdog_samples(base + 1.25)
        self.assertIsNone(await self.api.evaluate_watchdog(now=base + 1.25))
        state = self.api.state_response()
        self.assertEqual("NAVIGATING", state["navigation_state"])
        self.assertFalse(state["emergency_stop"])
        self.assertEqual(2, self.map_api.ros_control.plan_calls)
        self.assertEqual(2, self.map_api.ros_control.navigate_calls)
        self.assertIsNone(self.api._sensor_recovery)

    async def test_recovery_does_not_start_new_goal_without_stop_confirmation(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        base = time.time()
        self.api.note_navigation_sample("scan", now=base - 6.0)
        self.assertEqual("LIDAR_DATA_LOSS", await self.api.evaluate_watchdog(now=base))
        self.refresh_watchdog_samples(base + 0.1)
        self.assertIsNone(await self.api.evaluate_watchdog(now=base + 0.1))
        self.refresh_watchdog_samples(base + 1.2)
        self.assertIsNone(await self.api.evaluate_watchdog(now=base + 1.2))
        self.assertEqual(1, self.map_api.ros_control.navigate_calls)
        self.assertFalse(self.api.motion_allowed())

    async def test_nav2_failed_issues_physical_stop_without_emergency_stop(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        self.map_api.ros_control.state = "FAILED"
        observed = self.api.state_response()
        self.assertEqual("FAILED", observed["navigation_state"])
        self.assertFalse(self.api.motion_allowed())
        self.assertEqual(0, sum(
            c["type"] == "emergency_stop" for _, c in self.commands
        ))
        self.assertIsNone(await self.api.evaluate_watchdog())
        self.assertTrue(any(
            c.get("reason") == "nav2_failed" for _, c in self.commands
        ))
        self.assertFalse(self.api.state_response()["emergency_stop"])

    async def test_sensor_recovery_rejected_when_pi_stop_undeliverable(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        original_sender = self.api._send_robot_command

        async def reject_stop(robot_id, command):
            if command["type"] == "stop":
                return False
            return await original_sender(robot_id, command)

        self.api._send_robot_command = reject_stop
        base = time.time()
        self.api.note_navigation_sample("scan", now=base - 6)
        self.assertEqual(
            "LIDAR_DATA_LOSS", await self.api.evaluate_watchdog(now=base),
        )
        self.refresh_watchdog_samples(base + 0.1)
        self.assertIsNone(await self.api.evaluate_watchdog(now=base + 0.1))
        self.assertIsNone(await self.api.evaluate_watchdog(now=base + 1.2))
        self.assertEqual(1, self.map_api.ros_control.navigate_calls)
        self.assertFalse(self.api.motion_allowed())
        self.assertIsNone(await self.api.evaluate_watchdog(now=base + 5.1))
        failed = self.api.state_response()
        self.assertEqual("FAILED", failed["navigation_state"])
        self.assertFalse(failed["emergency_stop"])
        self.assertIsNone(self.api.motion_safety_context())

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

    async def test_start_rejects_unresolved_encoder_violation(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        with self.api._lock:
            self.api._stop_commanded_at = time.time() - 10
            self.api._stop_encoder_ticks = (0, 0, 0, 0)
            self.api._encoder_stop_violation = True
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.start_navigation()
        self.assertEqual("ENCODER_MOVEMENT_AFTER_STOP", raised.exception.error_code)
        self.assertEqual(0, self.map_api.ros_control.navigate_calls)
        with self.api._lock:
            self.assertIsNotNone(self.api._stop_commanded_at)
            self.assertEqual((0, 0, 0, 0), self.api._stop_encoder_ticks)
            self.assertTrue(self.api._encoder_stop_violation)
        self.assertEqual("ENCODER_MOVEMENT_AFTER_STOP", await self.api.evaluate_watchdog())
        self.api.note_encoder(dict.fromkeys(
            ("left_front_ticks", "right_front_ticks", "left_rear_ticks", "right_rear_ticks"), 20,
        ), now=time.time() + 1)
        self.assertIsNone(await self.api.evaluate_watchdog())

    async def test_cancel_blocks_direct_motion_until_stop_confirmed(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()

        canceled = await self.api.cancel_goal()
        self.assertTrue(canceled["stop_delivered"])

        # An outstanding stop must not be bypassed by manual motion.
        self.assertFalse(self.api.motion_allowed())
        self.assertFalse(self.api.note_authorized_motion())

        with self.api._lock:
            self.assertIsNotNone(self.api._stop_commanded_at)

        await self.confirm_pending_stop_for_test()

        self.assertTrue(self.api.motion_allowed())
        self.assertTrue(self.api.note_authorized_motion())

    async def test_cancel_then_confirmed_stop_allows_new_goal(self):
        await self.driving_ready()
        self.api.note_encoder({
            "sequence": 10,
            **dict.fromkeys((
                "left_front_ticks", "right_front_ticks",
                "left_rear_ticks", "right_rear_ticks",
            ), 0),
        })
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        canceled = await self.api.cancel_goal()
        self.assertTrue(canceled["stop_delivered"])

        with self.assertRaises(NavigationControlError) as raised:
            await self.api.plan_goal({"x": 1.5, "y": 1.0, "yaw": 0.0})
        self.assertEqual("CANCEL_IN_PROGRESS", raised.exception.error_code)

        await self.confirm_pending_stop_for_test()
        self.assertTrue(self.api.state_response()["cancel_completed"])
        await self.api.plan_goal({"x": 1.5, "y": 1.0, "yaw": 0.0})
        started = await self.api.start_navigation()
        self.assertEqual("NAVIGATING", started["navigation_state"])
        self.assertEqual(2, self.map_api.ros_control.navigate_calls)
        with self.api._lock:
            self.assertIsNone(self.api._stop_commanded_at)

    async def test_failed_start_keeps_old_stop_baseline(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        with self.api._lock:
            self.api._stop_commanded_at = time.time() - 10
            self.api._stop_encoder_ticks = (0, 0, 0, 0)
        await self.confirm_pending_stop_for_test()
        self.map_api.ros_control.navigate_to_pose = lambda _goal: (_ for _ in ()).throw(RuntimeError("rejected"))
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            await self.api.start_navigation()
        with self.api._lock:
            self.assertIsNotNone(self.api._stop_commanded_at)
            self.assertEqual((0, 0, 0, 0), self.api._stop_encoder_ticks)

    async def test_resume_requires_stable_fresh_encoder_before_clearing_violation(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.emergency_stop("operator")
        with self.api._lock:
            self.api._encoder_stop_violation = True
        self._auto_encoder_recovery = False
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.resume_navigation()
        self.assertEqual("STOP_NOT_CONFIRMED", raised.exception.error_code)
        self.assertTrue(self.api.state_response()["emergency_stop"])
        with self.api._lock:
            self.assertTrue(self.api._encoder_stop_violation)
        self.assertNotIn("resume_navigation", [command[1]["type"] for command in self.commands])
        self._auto_encoder_recovery = True
        resumed = await self.api.resume_navigation()
        self.assertEqual("NAVIGATING", resumed["navigation_state"])
        with self.api._lock:
            self.assertIsNone(self.api._stop_commanded_at)
            self.assertIsNone(self.api._stop_encoder_ticks)
            self.assertFalse(self.api._encoder_stop_violation)
        self.assertIsNone(await self.api.evaluate_watchdog())

    async def test_resume_rejects_stale_encoder_sequence(self):
        await self.api.emergency_stop("operator")
        self._auto_encoder_recovery = False
        self.api.STOP_CONFIRM_TIMEOUT_SEC = 0.3
        self.api.note_encoder(dict.fromkeys(
            ("left_front_ticks", "right_front_ticks", "left_rear_ticks", "right_rear_ticks"), 0,
        ) | {"sequence": 10})
        original_sender = self.api._send_robot_command

        async def stale_sender(robot_id, command):
            result = await original_sender(robot_id, command)
            if command["type"] == "resume_safety_check":
                loop = asyncio.get_running_loop()
                for delay, sequence in ((0.02, 10), (0.17, 9)):
                    loop.call_later(delay, self.api.note_encoder, {
                        "sequence": sequence,
                        **dict.fromkeys((
                            "left_front_ticks", "right_front_ticks", "left_rear_ticks", "right_rear_ticks"
                        ), 0),
                    })
            return result

        self.api._send_robot_command = stale_sender
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.resume_navigation()
        self.assertEqual("STOP_NOT_CONFIRMED", raised.exception.error_code)
        self.assertTrue(self.api.state_response()["emergency_stop"])
        self.assertNotIn("resume_navigation", [command[1]["type"] for command in self.commands])

    async def test_resume_rejects_encoder_motion_during_confirmation(self):
        await self.api.emergency_stop("operator")
        self._auto_encoder_recovery = False
        self.api.STOP_CONFIRM_TIMEOUT_SEC = 0.4
        original_sender = self.api._send_robot_command

        async def moving_sender(robot_id, command):
            result = await original_sender(robot_id, command)
            if command["type"] == "resume_safety_check":
                loop = asyncio.get_running_loop()
                for delay, sequence, ticks in ((0.02, 1, 0), (0.17, 2, 1), (0.32, 3, 2)):
                    loop.call_later(delay, self.api.note_encoder, {
                        "sequence": sequence,
                        **dict.fromkeys((
                            "left_front_ticks", "right_front_ticks", "left_rear_ticks", "right_rear_ticks"
                        ), ticks),
                    })
            return result

        self.api._send_robot_command = moving_sender
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.resume_navigation()
        self.assertEqual("STOP_NOT_CONFIRMED", raised.exception.error_code)
        self.assertTrue(self.api.state_response()["emergency_stop"])
        self.assertNotIn("resume_navigation", [command[1]["type"] for command in self.commands])

    async def test_new_estop_during_resume_confirmation_blocks_release(self):
        await self.api.emergency_stop("initial")
        self._auto_encoder_recovery = False
        original_sender = self.api._send_robot_command

        async def interrupted_sender(robot_id, command):
            result = await original_sender(robot_id, command)
            if command["type"] == "resume_safety_check":
                asyncio.get_running_loop().call_later(
                    0.02, lambda: asyncio.create_task(self.api.emergency_stop("operator"))
                )
            return result

        self.api._send_robot_command = interrupted_sender
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.resume_navigation()
        self.assertEqual("RESUME_INTERRUPTED", raised.exception.error_code)
        self.assertTrue(self.api.state_response()["emergency_stop"])
        self.assertNotIn("resume_navigation", [command[1]["type"] for command in self.commands])

    async def test_start_rejects_unconfirmed_previous_stop(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})

        with self.api._lock:
            self.api._stop_commanded_at = time.time()
            self.api._stop_encoder_ticks = (0, 0, 0, 0)
            self.api._encoder_stop_violation = False

        with self.assertRaises(NavigationControlError) as raised:
            await self.api.start_navigation()

        self.assertEqual("STOP_NOT_CONFIRMED", raised.exception.error_code)
        self.assertEqual(0, self.map_api.ros_control.navigate_calls)

    async def test_encoder_violation_during_start_cannot_be_approved(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        with self.api._lock:
            self.api._stop_commanded_at = time.time() - 1
            self.api._stop_encoder_ticks = (0, 0, 0, 0)
        await self.confirm_pending_stop_for_test()
        original_navigate = self.map_api.ros_control.navigate_to_pose

        def violating_navigate(goal):
            result = original_navigate(goal)
            self.api.note_encoder(dict.fromkeys((
                "left_front_ticks", "right_front_ticks", "left_rear_ticks", "right_rear_ticks"
            ), 4))
            return result

        self.map_api.ros_control.navigate_to_pose = violating_navigate
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.start_navigation()
        self.assertEqual("NAVIGATION_START_INTERRUPTED", raised.exception.error_code)
        self.assertTrue(self.api.state_response()["emergency_stop"])
        self.assertEqual("ENCODER_MOVEMENT_AFTER_STOP", self.api.state_response()["emergency_reason"])
        self.assertTrue(self.api._encoder_stop_violation)

    async def test_encoder_violation_during_resume_release_cannot_be_approved(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        self.api.note_encoder(dict.fromkeys((
            "left_front_ticks", "right_front_ticks", "left_rear_ticks", "right_rear_ticks"
        ), 0))
        await self.api.emergency_stop("operator")
        original_sender = self.api._send_robot_command

        async def violating_sender(robot_id, command):
            result = await original_sender(robot_id, command)
            if command["type"] == "resume_navigation":
                self.api.note_encoder(dict.fromkeys((
                    "left_front_ticks", "right_front_ticks", "left_rear_ticks", "right_rear_ticks"
                ), 4))
            return result

        self.api._send_robot_command = violating_sender
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.resume_navigation()
        self.assertEqual("RESUME_INTERRUPTED", raised.exception.error_code)
        self.assertTrue(self.api.state_response()["emergency_stop"])
        self.assertTrue(self.api._encoder_stop_violation)
        self.assertEqual(0, self.map_api.ros_control.navigate_calls)

    async def test_new_pi_estop_during_resume_release_cannot_be_approved(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.emergency_stop("operator")
        original_sender = self.api._send_robot_command

        async def stopped_sender(robot_id, command):
            result = await original_sender(robot_id, command)
            if command["type"] == "resume_navigation":
                self.api.note_pi_status({"emergency_stop": True})
            return result

        self.api._send_robot_command = stopped_sender
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.resume_navigation()
        self.assertEqual("RESUME_INTERRUPTED", raised.exception.error_code)
        self.assertTrue(self.api.state_response()["emergency_stop"])
        self.assertEqual(0, self.map_api.ros_control.navigate_calls)

    async def test_pi_latch_still_true_after_release_blocks_resume(self):
        await self.api.emergency_stop("operator")
        original_sender = self.api._send_robot_command

        async def still_latched_sender(robot_id, command):
            if command["type"] == "resume_navigation":
                return True
            return await original_sender(robot_id, command)

        self.api._send_robot_command = still_latched_sender
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.resume_navigation()
        self.assertEqual("RESUME_INTERRUPTED", raised.exception.error_code)
        self.assertTrue(self.api.state_response()["emergency_stop"])

    async def test_newer_pi_estop_status_survives_older_release_ack(self):
        await self.api.emergency_stop("operator")
        original_sender = self.api._send_robot_command

        async def reordered_sender(robot_id, command):
            if command["type"] == "resume_navigation":
                self.api.note_pi_status({
                    "safety_session": "test-pi",
                    "safety_epoch": self._fake_safety_epoch + 1,
                    "emergency_stop": True,
                })
                self.api.note_pi_status({
                    "safety_session": "test-pi",
                    "safety_epoch": self._fake_safety_epoch,
                    "emergency_stop": False,
                })
                return True
            return await original_sender(robot_id, command)

        self.api._send_robot_command = reordered_sender
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.resume_navigation()
        self.assertEqual("RESUME_INTERRUPTED", raised.exception.error_code)
        self.assertTrue(self.api.state_response()["emergency_stop"])
        self.assertTrue(self.api._pi_estop_latched)

    async def test_motion_during_slow_release_invalidates_prior_confirmation(self):
        await self.api.emergency_stop("operator")
        with self.api._lock:
            self.api._encoder_stop_violation = True
        original_sender = self.api._send_robot_command

        async def moving_release(robot_id, command):
            result = await original_sender(robot_id, command)
            if command["type"] == "resume_navigation":
                marker = self.api._encoder_sequence or 0
                ticks = ("left_front_ticks", "right_front_ticks", "left_rear_ticks", "right_rear_ticks")
                self.api.note_encoder({"sequence": marker + 1, **dict.fromkeys(ticks, 1)})
                await asyncio.sleep(0.12)
                self.api.note_encoder({"sequence": marker + 2, **dict.fromkeys(ticks, 1)})
            return result

        self.api._send_robot_command = moving_release
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.resume_navigation()
        self.assertEqual("RESUME_INTERRUPTED", raised.exception.error_code)
        self.assertTrue(self.api._encoder_stop_violation)
        self.assertTrue(self.api.state_response()["emergency_stop"])

    async def test_encoder_violation_watchdog_sends_one_estop(self):
        self.api.note_encoder(dict.fromkeys((
            "left_front_ticks", "right_front_ticks", "left_rear_ticks", "right_rear_ticks"
        ), 0))
        await self.api.emergency_stop("operator")
        self.api.note_encoder(dict.fromkeys((
            "left_front_ticks", "right_front_ticks", "left_rear_ticks", "right_rear_ticks"
        ), 4))
        self.assertEqual("ENCODER_MOVEMENT_AFTER_STOP", await self.api.evaluate_watchdog())
        self.assertIsNone(await self.api.evaluate_watchdog())
        self.assertEqual(2, sum(command[1]["type"] == "emergency_stop" for command in self.commands))
        self.assertTrue(self.api._encoder_stop_violation)

    async def test_failed_encoder_estop_is_retried_with_a_bound(self):
        await self.api.emergency_stop("operator")
        with self.api._lock:
            self.api._encoder_stop_violation = True
        original_sender = self.api._send_robot_command
        attempts = 0

        async def flaky_stop(robot_id, command):
            nonlocal attempts
            if command["type"] == "emergency_stop":
                attempts += 1
                if attempts == 1:
                    return False
            return await original_sender(robot_id, command)

        self.api._send_robot_command = flaky_stop
        self.assertEqual("ENCODER_MOVEMENT_AFTER_STOP", await self.api.evaluate_watchdog())
        self.assertIsNone(await self.api.evaluate_watchdog())
        with self.api._lock:
            self.api._estop_last_attempt_at = time.time() - 2
        self.assertEqual("ENCODER_MOVEMENT_AFTER_STOP", await self.api.evaluate_watchdog())
        self.assertEqual(2, attempts)
        self.assertIsNone(await self.api.evaluate_watchdog())
        self.assertTrue(self.api._estop_command_acknowledged)

    async def test_unlatched_pi_status_rearms_failed_estop_delivery(self):
        await self.api.emergency_stop("operator")
        with self.api._lock:
            self.api._encoder_stop_violation = True
        original_sender = self.api._send_robot_command
        attempts = 0

        async def failed_stop(robot_id, command):
            nonlocal attempts
            if command["type"] == "emergency_stop":
                attempts += 1
                return False
            return await original_sender(robot_id, command)

        self.api._send_robot_command = failed_stop
        for attempt in range(self.api.ESTOP_MAX_ATTEMPTS):
            if attempt:
                with self.api._lock:
                    self.api._estop_last_attempt_at = time.time() - 2
            self.assertEqual("ENCODER_MOVEMENT_AFTER_STOP", await self.api.evaluate_watchdog())
        self.assertIsNone(await self.api.evaluate_watchdog())
        self.assertEqual(self.api.ESTOP_MAX_ATTEMPTS, attempts)
        self.api.note_pi_status({
            "safety_session": "test-pi",
            "safety_epoch": self._fake_safety_epoch,
            "emergency_stop": False,
        })
        self.assertEqual("ENCODER_MOVEMENT_AFTER_STOP", await self.api.evaluate_watchdog())
        self.assertEqual(self.api.ESTOP_MAX_ATTEMPTS + 1, attempts)

        for _ in range(1, self.api.ESTOP_MAX_ATTEMPTS):
            with self.api._lock:
                self.api._estop_last_attempt_at = time.time() - 2
            self.assertEqual("ENCODER_MOVEMENT_AFTER_STOP", await self.api.evaluate_watchdog())
        self.assertIsNone(await self.api.evaluate_watchdog())
        self.api.note_pi_status({
            "safety_session": "test-pi",
            "safety_epoch": self._fake_safety_epoch,
            "emergency_stop": False,
        })
        with self.api._lock:
            self.api._estop_last_attempt_at = time.time() - self.api.ESTOP_RETRY_WINDOW_SEC - 1
        self.assertEqual("ENCODER_MOVEMENT_AFTER_STOP", await self.api.evaluate_watchdog())
        self.assertEqual(self.api.ESTOP_MAX_ATTEMPTS * 2 + 1, attempts)

    async def test_ros_terminal_does_not_hide_encoder_violation(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        with self.api._lock:
            self.api._encoder_stop_violation = True
        self.map_api.ros_control.state = "SUCCEEDED"
        self.assertEqual("ENCODER_MOVEMENT_AFTER_STOP", await self.api.evaluate_watchdog())
        self.assertTrue(self.api.state_response()["emergency_stop"])

    async def test_pi_reconnect_requires_fresh_status_for_motion(self):
        await self.driving_ready()
        # Only active NavigateToPose sessions may consume automatic motion
        # safety tokens. A DRIVING-ready screen is not an active Goal.
        with self.api._lock:
            self.api._state["navigation_state"] = "NAVIGATING"

        status = {
            "mode": "auto",
            "navigation_mode": "driving",
            "emergency_stop": False,
            "safety_session": "reconnect-test-pi",
            "safety_epoch": 1,
        }

        self.api.note_pi_status(status)
        self.assertIsNotNone(self.api.motion_safety_context())

        self.api.note_pi_connection(False)
        self.assertIsNone(self.api.motion_safety_context())

        self.api.note_pi_connection(True)

        # A WebSocket connection alone is not a fresh Pi safety report.
        self.assertIsNone(self.api.motion_safety_context())

        self.api.note_pi_status(status)
        self.assertIsNotNone(self.api.motion_safety_context())

    async def test_pi_reconnect_accepts_new_encoder_sequence(self):
        self.api.note_encoder({
            "sequence": 15,
            **dict.fromkeys((
                "left_front_ticks", "right_front_ticks", "left_rear_ticks", "right_rear_ticks"
            ), 0),
        })
        self.api.note_pi_connection(False)
        self.api.note_pi_connection(True)
        self.api.note_encoder({
            "sequence": 1,
            **dict.fromkeys((
                "left_front_ticks", "right_front_ticks", "left_rear_ticks", "right_rear_ticks"
            ), 0),
        })
        self.assertEqual(1, self.api._encoder_sequence)

    async def test_cancel_goal_preserves_terminal_success_race(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()

        def completes_before_cancel():
            self.map_api.ros_control.cancel_calls += 1
            self.map_api.ros_control.state = "SUCCEEDED"
            return {
                "requested": False,
                "confirmed": True,
                "completed": True,
                "terminal": "SUCCEEDED",
            }

        self.map_api.ros_control.cancel_navigation = completes_before_cancel

        result = await self.api.cancel_goal()
        self.assertEqual("NAVIGATING", result["navigation_state"])
        self.assertIsNotNone(result["active_goal"])
        self.assertFalse(result["emergency_stop"])
        finishing = asyncio.create_task(self.api.evaluate_watchdog())
        await asyncio.sleep(0.02)
        await self.confirm_pending_stop_for_test()
        self.assertIsNone(await finishing)
        complete = self.api.state_response()
        self.assertEqual("SUCCEEDED", complete["navigation_state"])
        self.assertIsNone(complete["active_goal"])
        self.assertEqual([], complete["planned_path"])

    async def test_manual_takeover_preserves_goal_when_ros_was_already_canceled(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()

        def already_canceled():
            self.map_api.ros_control.cancel_calls += 1
            self.map_api.ros_control.state = "CANCELED"
            return {
                "requested": False,
                "confirmed": True,
                "completed": True,
                "terminal": "CANCELED",
            }

        self.map_api.ros_control.cancel_navigation = already_canceled

        result = await self.api.pause_for_manual()

        self.assertEqual("READY", result["navigation_state"])
        self.assertIsNotNone(result["active_goal"])
        self.assertTrue(result["planned_path"])
        self.assertTrue(result["goal_retained"])

    async def test_manual_takeover_preserves_terminal_failure_and_goal(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()

        def already_failed():
            self.map_api.ros_control.cancel_calls += 1
            self.map_api.ros_control.state = "FAILED"
            return {
                "requested": False,
                "confirmed": True,
                "completed": True,
                "terminal": "FAILED",
            }

        self.map_api.ros_control.cancel_navigation = already_failed

        result = await self.api.pause_for_manual()

        self.assertEqual("FAILED", result["navigation_state"])
        self.assertIsNotNone(result["active_goal"])
        self.assertTrue(result["planned_path"])
        self.assertTrue(result["goal_retained"])


    async def test_manual_takeover_cancel_exception_does_not_resume_motion(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()

        def cancel_failure():
            raise RuntimeError("Nav2 cancel unavailable")

        self.map_api.ros_control.cancel_navigation = cancel_failure
        with self.assertRaisesRegex(RuntimeError, "Nav2 cancel unavailable"):
            await self.api.pause_for_manual()

        result = self.api.state_response()
        self.assertEqual("FAILED", result["navigation_state"])
        self.assertEqual("NAV_CANCEL_FAILED", result["last_error"])
        self.assertIsNotNone(result["active_goal"])
        self.assertTrue(any(
            command["type"] == "stop" and command.get("reason") == "manual_drive_takeover"
            for _, command in self.commands
        ))
        await self.confirm_pending_stop_for_test()
        self.assertFalse(self.api.motion_allowed())
        self.assertIsNone(self.api.motion_safety_context())

    async def test_manual_takeover_rejects_unconfirmed_nav2_cancel(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()

        def unconfirmed_cancel():
            return {"requested": True, "confirmed": False, "completed": False}

        self.map_api.ros_control.cancel_navigation = unconfirmed_cancel
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.pause_for_manual()
        self.assertEqual("NAV_CANCEL_FAILED", raised.exception.error_code)
        self.assertEqual("FAILED", self.api.state_response()["navigation_state"])
        self.assertTrue(any(c.get("reason") == "manual_drive_takeover" for _, c in self.commands))
        await self.confirm_pending_stop_for_test()
        self.assertFalse(self.api.motion_allowed())

    async def test_manual_takeover_success_race_retains_original_goal_stop_proof(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()

        def successful_terminal_race():
            self.map_api.ros_control.state = "SUCCEEDED"
            return {
                "requested": True, "confirmed": False,
                "completed": True, "terminal": "SUCCEEDED",
            }

        self.map_api.ros_control.cancel_navigation = successful_terminal_race
        response = await self.api.pause_for_manual()
        self.assertEqual("NAVIGATING", response["navigation_state"])
        self.assertIsNotNone(self.api._pending_nav_success)
        self.assertIsNotNone(response["active_goal"])
        self.assertFalse(self.api.motion_allowed())
        # Stable encoder STOP alone must not reopen motor authorization while
        # original Goal pose/yaw proof is still pending.
        await self.confirm_pending_stop_for_test()
        self.assertIsNotNone(self.api._pending_nav_success)
        self.assertFalse(self.api.motion_allowed())
        self.assertIsNone(self.api.motion_safety_context())

    async def test_late_manual_pause_stop_cannot_replace_estop_proof(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()

        entered = asyncio.Event()
        release = asyncio.Event()
        original_sender = self.api._send_robot_command

        async def delayed_stop(robot_id, command):
            if command.get("reason") == "manual_drive_takeover":
                entered.set()
                await release.wait()
            return await original_sender(robot_id, command)

        self.api._send_robot_command = delayed_stop
        pending = asyncio.create_task(self.api.pause_for_manual())
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        try:
            await self.api.emergency_stop("operator")
            with self.api._lock:
                baseline = (
                    self.api._stop_commanded_at,
                    self.api._stop_confirmation_after_sequence,
                    self.api._stop_encoder_ticks,
                )
        finally:
            release.set()
        result = await pending
        self.assertEqual("EMERGENCY_STOPPED", result["navigation_state"])
        with self.api._lock:
            self.assertEqual(
                baseline,
                (
                    self.api._stop_commanded_at,
                    self.api._stop_confirmation_after_sequence,
                    self.api._stop_encoder_ticks,
                ),
            )
        self.assertFalse(self.api.motion_allowed())

    async def test_resume_check_rejects_pi_reported_delivery_failure(self):
        await self.driving_ready()
        await self.api.emergency_stop("initial")
        original_sender = self.api._send_robot_command

        async def incomplete_check(robot_id, command):
            if command["type"] == "resume_safety_check":
                return {
                    "ok": True, "delivered": False,
                    "encoder_sequence": 2,
                    "safety_session": "test-pi",
                    "safety_epoch": self._fake_safety_epoch,
                }
            return await original_sender(robot_id, command)

        self.api._send_robot_command = incomplete_check
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.resume_navigation()
        self.assertEqual("STOP_NOT_CONFIRMED", raised.exception.error_code)
        self.assertTrue(self.api.state_response()["emergency_stop"])
        self.assertFalse(self.api.motion_allowed())

    async def test_resume_release_rejects_truthy_failure_response(self):
        await self.driving_ready()
        await self.api.emergency_stop("initial")
        original_sender = self.api._send_robot_command

        async def incomplete_release(robot_id, command):
            if command["type"] == "resume_navigation":
                return {"ok": False, "delivered": False}
            return await original_sender(robot_id, command)

        self.api._send_robot_command = incomplete_release
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.resume_navigation()
        self.assertEqual("PI_RESUME_FAILED", raised.exception.error_code)
        self.assertTrue(self.api.state_response()["emergency_stop"])
        self.assertFalse(self.api.motion_allowed())

    async def test_late_auto_motion_context_rejected_after_goal_canceled(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        # Establish the Pi safety identity before obtaining an auto-drive
        # permit; the legacy driving_ready fixture has no session ID.
        self.api.note_pi_status({
            "mode": "auto", "navigation_mode": "driving",
            "emergency_stop": False,
            "safety_session": "test-pi", "safety_epoch": 0,
        })
        safety = self.api.motion_safety_context()
        self.assertIsNotNone(safety)
        with self.api._lock:
            self.api._state["navigation_state"] = "CANCELED"
        self.assertFalse(self.api.note_authorized_motion(safety))
        self.assertIsNone(self.api.motion_safety_context())

    async def test_goal_generation_rejects_stale_motion_after_restart(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        self.api.note_pi_status({
            "mode": "auto", "navigation_mode": "driving",
            "emergency_stop": False,
            "safety_session": "test-pi", "safety_epoch": 0,
        })
        previous = self.api.motion_safety_context()
        self.assertIsNotNone(previous)
        with self.api._lock:
            # Later Goal starts with unchanged Pi session and safety epoch.
            self.api._start_navigation_progress_locked({"x": 0.0, "y": 0.0}, time.time())
        current = self.api.motion_safety_context()
        self.assertIsNotNone(current)
        self.assertNotEqual(previous["goal_generation"], current["goal_generation"])
        self.assertFalse(self.api.note_authorized_motion(previous))
        self.assertTrue(self.api.note_authorized_motion(current))

    async def test_nonactive_pose_update_does_not_invalidate_manual_motion_generation(self):
        with self.api._lock:
            before = self.api._goal_motion_generation
            self.api._state["navigation_state"] = "READY"
            self.api._note_navigation_progress_locked(
                {"x": 0.1, "y": 0.1}, time.time()
            )
            self.assertEqual(before, self.api._goal_motion_generation)

    async def test_robot_mode_change_rejects_cached_autodrive_context(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        self.api.note_pi_status({
            "mode": "auto", "navigation_mode": "driving",
            "emergency_stop": False, "safety_session": "test-pi",
            "safety_epoch": 0,
        })
        prior = self.api.motion_safety_context()
        self.assertIsNotNone(prior)
        self.api.note_pi_status({"mode": "manual", "navigation_mode": "driving"})
        self.assertEqual("manual", self.api._robot_mode)
        self.assertNotEqual(prior, self.api.motion_safety_context())
        self.assertFalse(self.api.note_authorized_motion(prior))

    async def test_auto_requires_verified_driving_before_pi_mode_request(self):
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.switch_patrol_mode({"mode": "auto"})
        self.assertEqual("DRIVING_NOT_READY", raised.exception.error_code)
        self.assertFalse(any(cmd["type"] == "mode" for _, cmd in self.commands))

    async def test_patrol_manual_pause_retains_goal_and_never_starts_motor(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        before = list(self.api.state_response()["planned_path"])
        result = await self.api.switch_patrol_mode({"mode": "manual"})
        self.assertEqual("manual", result["robot_mode"])
        self.assertEqual("DRIVING", result["navigation_mode"])
        self.assertEqual("READY", result["navigation_state"])
        self.assertEqual(before, result["planned_path"])
        self.assertTrue(result["active_goal"])
        self.assertFalse(any(cmd["type"] == "auto_drive" for _, cmd in self.commands))

    async def test_patrol_ack_failure_preserves_motion_gate(self):
        await self.driving_ready()
        original = self.api._send_robot_command

        async def fail_mode(robot_id, command):
            if command.get("type") == "mode":
                return False
            return await original(robot_id, command)

        self.api._send_robot_command = fail_mode
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.switch_patrol_mode({"mode": "manual"})
        self.assertEqual("PI_MODE_ACK_FAILED", raised.exception.error_code)
        self.assertFalse(self.api.motion_allowed())
        self.assertEqual("PI_MODE_ACK_FAILED",
                         self.api.state_response()["patrol_transition_error"])

    async def test_cancel_pending_maintains_goal_until_fresh_encoder_proof(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        first = await self.api.cancel_goal()
        self.assertFalse(first["cancel_completed"])
        self.assertEqual("CANCELING_STOP", first["navigation_state"])
        self.assertIsNotNone(first["active_goal"])
        self.assertTrue(first["planned_path"])
        duplicate = await self.api.cancel_goal()
        self.assertFalse(duplicate["cancel_completed"])
        self.assertEqual(1, sum(cmd["type"] == "stop" and
                                cmd.get("reason") == "navigation_goal_cancel"
                                for _, cmd in self.commands))
        await self.confirm_pending_stop_for_test()
        final = self.api.state_response()
        self.assertTrue(final["cancel_completed"])
        self.assertEqual("READY", final["navigation_state"])
        self.assertIsNone(final["active_goal"])
        self.assertEqual([], final["planned_path"])
        repeat = await self.api.cancel_goal()
        self.assertTrue(repeat["cancel_completed"])

    async def test_cancel_encoder_timeout_keeps_goal_and_blocks_motion(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        await self.api.cancel_goal()
        pending = self.api.state_response()["cancel_progress"]
        failed = self.api.state_response(
            now=pending["started_at"] + self.api.STOP_CONFIRM_TIMEOUT_SEC + 0.2
        )
        self.assertEqual("FAILED", failed["navigation_state"])
        self.assertEqual("STOP_NOT_CONFIRMED", failed["last_error"])
        self.assertIsNotNone(failed["active_goal"])
        self.assertFalse(failed["cancel_completed"])
        self.assertFalse(self.api.motion_allowed())

    async def test_replan_signal_recomputes_for_current_goal_not_ros_packet(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        before = self.map_api.ros_control.plan_calls
        self.api.note_nav2_replan_signal()
        for _ in range(100):
            if self.map_api.ros_control.plan_calls > before and not self.api._replan_refresh_inflight:
                break
            await asyncio.sleep(0.01)
        self.assertGreater(self.map_api.ros_control.plan_calls, before)
        self.assertIsNotNone(self.api.state_response()["active_goal"])

    async def test_late_owned_replan_cannot_overwrite_replaced_goal(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        entered = threading.Event()
        release = threading.Event()
        original = self.api._compute_path_for_route
        def blocked(goal, waypoints):
            entered.set()
            if not release.wait(1):
                raise RuntimeError("test timeout")
            return original(goal, waypoints)
        self.api._compute_path_for_route = blocked
        self.api.note_nav2_replan_signal()
        self.assertTrue(await asyncio.to_thread(entered.wait, 0.5))
        with self.api._lock:
            self.api._goal_motion_generation += 1
            previous_path = list(self.api._state["planned_path"])
        release.set()
        for _ in range(100):
            if not self.api._replan_refresh_inflight:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(previous_path, self.api.state_response()["planned_path"])

    async def test_repeated_patrol_request_never_masks_unconfirmed_stop(self):
        await self.driving_ready()
        with self.api._lock:
            self.api._robot_mode = "manual"
            self.api._patrol_transition_error = "PI_MODE_ACK_FAILED"
            self.api._stop_commanded_at = time.time()
            self.api._stop_confirmation_after_sequence = None
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.switch_patrol_mode({"mode": "manual"})
        self.assertEqual("STOP_NOT_CONFIRMED", raised.exception.error_code)

    async def test_cancel_cannot_clear_goal_without_stop_ack(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        with self.api._lock:
            self.api._pending_goal_cancel = {
                "started_at": time.time(), "stop_delivered": False,
            }
            self.api._state["navigation_state"] = "CANCELING_STOP"
            self.api._stop_confirmation_after_sequence = 1
            self.api._stop_confirmation_ticks = (0, 0, 0, 0)
            self.api._stop_confirmation_started_at = time.time() - 1.0
            self.api._stop_confirmation_last_at = time.time()
            self.api._stop_confirmation_samples = 5
        self.api.note_encoder({
            "sequence": 2, "left_front_ticks": 0, "left_rear_ticks": 0,
            "right_front_ticks": 0, "right_rear_ticks": 0,
        })
        self.assertIsNotNone(self.api.state_response()["active_goal"])
        self.assertFalse(self.api.state_response()["cancel_completed"])

    async def test_mapping_pi_mode_failure_preserves_gpu_truth_and_blocks_motion(self):
        await self.driving_ready()
        old_sender = self.api._send_robot_command

        async def reject_mapping(robot_id, command):
            if command.get("type") == "navigation_mode" and command.get("mode") == "mapping":
                return False
            return await old_sender(robot_id, command)

        self.api._send_robot_command = reject_mapping
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.switch_mode({"mode": "MAPPING"})
        self.assertEqual("PI_MODE_SYNC_FAILED", raised.exception.error_code)
        result = self.api.state_response()
        self.assertEqual("MAPPING", result["navigation_mode"])
        self.assertEqual("ERROR", result["navigation_state"])
        self.assertEqual("DRIVING", result["pi_navigation_mode"])
        self.assertFalse(result["patrol_ready"])
        self.assertFalse(self.api.motion_allowed())
        self.assertEqual([], result["planned_path"])

    async def test_cancel_success_winning_race_without_cancel_ack(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()

        def real_nav2_success_race():
            self.map_api.ros_control.state = "SUCCEEDED"
            return {
                "requested": True, "confirmed": False,
                "completed": True, "terminal": "SUCCEEDED",
            }

        self.map_api.ros_control.cancel_navigation = real_nav2_success_race
        canceled = await self.api.cancel_goal()
        self.assertEqual("NAVIGATING", canceled["navigation_state"])
        self.assertIsNotNone(canceled["active_goal"])
        self.assertTrue(canceled["stop_delivered"])
        self.assertFalse(canceled["cancel_completed"])
        self.assertIsNotNone(self.api._pending_nav_success)
        self.assertFalse(self.api.motion_allowed())

    async def test_estop_during_manual_pause_blocks_mode_command(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        original_pause = self.api.pause_for_manual

        async def interrupt_pause():
            result = await original_pause()
            with self.api._lock:
                self.api._state["emergency_stop"] = True
                self.api._pi_estop_latched = True
            result["emergency_stop"] = True
            return result

        self.api.pause_for_manual = interrupt_pause
        before = len([c for _, c in self.commands if c["type"] == "mode"])
        with self.assertRaises(NavigationControlError):
            await self.api.switch_patrol_mode({"mode": "manual"})
        after = len([c for _, c in self.commands if c["type"] == "mode"])
        self.assertEqual(before, after)

    async def test_manual_request_stops_orphaned_nav_goal_even_if_pi_already_manual(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        self.api.note_pi_status({
            "mode": "manual", "navigation_mode": "driving",
            "emergency_stop": False,
        })
        before = self.map_api.ros_control.cancel_calls
        result = await self.api.switch_patrol_mode({"mode": "manual"})
        self.assertGreater(self.map_api.ros_control.cancel_calls, before)
        self.assertEqual("manual", result["robot_mode"])
        self.assertEqual("READY", result["navigation_state"])
        self.assertIsNotNone(result["active_goal"])
        self.assertTrue(any(command["type"] == "stop" for _, command in self.commands))

    async def test_simultaneous_cancel_reuses_original_stop_execution(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        entered = threading.Event()
        released = threading.Event()
        original_cancel = self.map_api.ros_control.cancel_navigation

        def slow_cancel():
            entered.set()
            if not released.wait(1):
                raise RuntimeError("cancel test timed out")
            return original_cancel()

        self.map_api.ros_control.cancel_navigation = slow_cancel
        task = asyncio.create_task(self.api.cancel_goal())
        self.assertTrue(await asyncio.to_thread(entered.wait, 0.5))
        try:
            second = await self.api.cancel_goal()
            self.assertFalse(second["cancel_completed"])
            self.assertFalse(second["stop_delivered"])
            self.assertEqual("CANCELING", second["navigation_state"])
            self.assertEqual(1, sum(
                cmd.get("reason") == "navigation_goal_cancel"
                for _, cmd in self.commands if cmd["type"] == "stop"
            ))
        finally:
            released.set()
        first = await task
        self.assertEqual("CANCELING_STOP", first["navigation_state"])

    async def test_mode_ack_is_not_accepted_as_fresh_telemetry(self):
        await self.driving_ready()
        prior = self.api._pi_updated_at
        self.api.note_pi_status({
            "type": "ack", "command_id": "fake", "ok": True,
            "mode": "manual", "navigation_mode": "mapping",
            "emergency_stop": False,
        }, now=prior + 100)
        self.assertEqual(prior, self.api._pi_updated_at)
        self.assertEqual("DRIVING", self.api._pi_navigation_mode)

    async def test_navigation_telemetry_requires_sample_newer_than_request(self):
        await self.driving_ready()
        prior = self.api._pi_updated_at
        self.assertFalse(await self.api._await_pi_navigation_telemetry(
            "DRIVING", prior, timeout_sec=0.01
        ))
        self.api.note_pi_status({
            "mode": "auto", "navigation_mode": "driving",
            "emergency_stop": False,
        }, now=prior + 0.1)
        self.assertTrue(await self.api._await_pi_navigation_telemetry(
            "DRIVING", prior, timeout_sec=0.01
        ))

    async def test_cancel_stop_timeout_progresses_without_dashboard_polling(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        await self.api.cancel_goal()
        with self.api._lock:
            started_at = self.api._pending_goal_cancel["started_at"]
        await self.api.evaluate_watchdog(
            now=started_at + self.api.STOP_CONFIRM_TIMEOUT_SEC + 0.2
        )
        state = self.api.state_response()
        self.assertEqual("FAILED", state["navigation_state"])
        self.assertEqual("STOP_NOT_CONFIRMED", state["last_error"])
        self.assertIsNotNone(state["active_goal"])
        self.assertFalse(self.api.motion_allowed())

    async def test_mode_sync_failure_stays_motion_blocked_after_encoder_stop(self):
        await self.driving_ready()
        with self.api._lock:
            self.api._patrol_transition_error = "PI_MODE_ACK_FAILED"
            self.api._stop_commanded_at = time.time()
            self.api._stop_confirmation_after_sequence = self.api._encoder_sequence or 0
        await self.confirm_pending_stop_for_test()
        self.assertFalse(self.api.motion_allowed())
        self.assertIsNone(self.api.motion_safety_context())
        self.assertFalse(self.api.state_response()["patrol_ready"])

    async def test_mode_ack_failure_recovers_only_after_confirmed_stop_and_retry(self):
        await self.driving_ready()
        original = self.api._send_robot_command
        failed_once = True

        async def flaky_mode(robot_id, command):
            nonlocal failed_once
            if command.get("type") == "mode" and failed_once:
                failed_once = False
                # A lost ACK does not prove the Pi did not actually switch.
                self.api.note_pi_status({
                    "mode": command["mode"], "navigation_mode": "driving",
                    "emergency_stop": False,
                })
                return False
            return await original(robot_id, command)

        self.api._send_robot_command = flaky_mode
        with self.assertRaises(NavigationControlError) as first:
            await self.api.switch_patrol_mode({"mode": "manual"})
        self.assertEqual("PI_MODE_ACK_FAILED", first.exception.error_code)
        self.assertFalse(self.api.motion_allowed())
        with self.assertRaises(NavigationControlError) as early:
            await self.api.switch_patrol_mode({"mode": "manual"})
        self.assertEqual("STOP_NOT_CONFIRMED", early.exception.error_code)
        await self.confirm_pending_stop_for_test()
        self.assertFalse(self.api.motion_allowed())
        result = await self.api.switch_patrol_mode({"mode": "manual"})
        self.assertEqual("manual", result["robot_mode"])
        self.assertIsNone(result["patrol_transition_error"])

    def test_pi_ack_contract_rejects_truthy_error_payloads(self):
        accepted = self.api._pi_command_delivered
        self.assertTrue(accepted(True))
        self.assertTrue(accepted({"ok": True, "delivered": True}))
        for reply in (
            {"ok": True},
            {"ok": True, "delivered": None},
            {"ok": True, "delivered": 1},
            {"ok": True, "delivered": "true"},
            False, None, RuntimeError("disconnected"), "OK",
            {"ok": False, "delivered": True},
            {"ok": True, "delivered": False},
            {"ok": "false", "delivered": True},
            {"ok": True, "delivered": "false"},
            {"error": "Pi unavailable"},
        ):
            self.assertFalse(accepted(reply), repr(reply))

    async def test_cancel_dict_stop_failure_cannot_ack_encoder_stop(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        original_sender = self.api._send_robot_command

        async def reject_stop(robot_id, command):
            if command["type"] == "stop":
                return {"ok": False, "delivered": False, "error": "Pi UART timeout"}
            return await original_sender(robot_id, command)

        self.api._send_robot_command = reject_stop
        canceled = await self.api.cancel_goal()
        self.assertIs(canceled["stop_delivered"], False)
        self.assertEqual("FAILED", canceled["navigation_state"])
        self.assertEqual("STOP_NOT_DELIVERED", canceled["last_error"])
        self.assertIsNone(self.api._stop_confirmation_after_sequence)
        self.assertIsNotNone(canceled["active_goal"])
        self.assertFalse(self.api.motion_allowed())

    async def test_manual_dict_stop_failure_cannot_report_ready(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        original_sender = self.api._send_robot_command

        async def reject_stop(robot_id, command):
            if command.get("reason") == "manual_drive_takeover":
                return {"ok": True, "delivered": False}
            return await original_sender(robot_id, command)

        self.api._send_robot_command = reject_stop
        paused = await self.api.pause_for_manual()
        self.assertIs(paused["stop_delivered"], False)
        self.assertEqual("FAILED", paused["navigation_state"])
        self.assertEqual("STOP_NOT_DELIVERED", paused["last_error"])
        self.assertIsNone(self.api._stop_confirmation_after_sequence)
        self.assertIsNotNone(paused["active_goal"])
        self.assertFalse(self.api.motion_allowed())

    async def test_emergency_stop_dict_error_does_not_acknowledge_pi(self):
        await self.driving_ready()
        original_sender = self.api._send_robot_command

        async def reject_estop(robot_id, command):
            if command["type"] == "emergency_stop":
                return {"ok": False, "delivered": False}
            return await original_sender(robot_id, command)

        self.api._send_robot_command = reject_estop
        stopped = await self.api.emergency_stop("operator")
        self.assertTrue(stopped["emergency_stop"])
        self.assertEqual("EMERGENCY_STOPPED", stopped["navigation_state"])
        self.assertIs(stopped["stop_delivered"], False)
        self.assertFalse(self.api._estop_command_acknowledged)
        self.assertFalse(self.api.motion_allowed())

    async def test_ros_cancel_exception_keeps_emergency_stop_delivery(self):
        await self.driving_ready()

        def reject_nav_cancel():
            raise RuntimeError("ROS cancellation failed")

        self.map_api.ros_control.cancel_navigation = reject_nav_cancel
        stopped = await self.api.emergency_stop("operator")
        self.assertTrue(stopped["emergency_stop"])
        self.assertTrue(stopped["stop_delivered"])
        self.assertIn("ROS cancellation failed", stopped["cancel_error"])
        self.assertTrue(self.api._estop_command_acknowledged)
        self.assertFalse(self.api.motion_allowed())

    async def test_terminal_success_dict_stop_failure_is_not_success(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        original_sender = self.api._send_robot_command

        async def reject_stop(robot_id, command):
            if command.get("reason") == "navigation_goal_succeeded":
                return {"ok": True, "delivered": False}
            return await original_sender(robot_id, command)

        self.api._send_robot_command = reject_stop
        self.map_api.ros_control.state = "SUCCEEDED"
        self.assertEqual("NAVIGATING", self.api.state_response()["navigation_state"])
        self.assertIsNone(await self.api.evaluate_watchdog())
        result = self.api.state_response()
        self.assertEqual("FAILED", result["navigation_state"])
        self.assertEqual("STOP_NOT_DELIVERED", result["last_error"])
        self.assertIsNotNone(result["active_goal"])

    async def test_cancel_pi_stop_failure_retains_goal_and_fails_safely(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.start_navigation()
        original_sender = self.api._send_robot_command

        async def reject_physical_stop(robot_id, command):
            if command["type"] == "stop":
                return False
            return await original_sender(robot_id, command)

        self.api._send_robot_command = reject_physical_stop
        canceled = await self.api.cancel_goal()
        self.assertFalse(canceled["stop_delivered"])
        self.assertEqual("FAILED", canceled["navigation_state"])
        self.assertEqual("STOP_NOT_DELIVERED", canceled["last_error"])
        self.assertIsNotNone(canceled["active_goal"])
        self.assertFalse(canceled["emergency_stop"])
        self.assertFalse(self.api.motion_allowed())

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

    async def test_prior_encoder_estop_still_cancels_late_start_goal(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        with self.api._lock:
            self.api._stop_commanded_at = time.time() - 1
            self.api._stop_encoder_ticks = (0, 0, 0, 0)
        await self.confirm_pending_stop_for_test()
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
        self.api.note_encoder(dict.fromkeys((
            "left_front_ticks", "right_front_ticks", "left_rear_ticks", "right_rear_ticks"
        ), 4))
        self.assertEqual("ENCODER_MOVEMENT_AFTER_STOP", await self.api.evaluate_watchdog())
        release.set()
        with self.assertRaises(NavigationControlError):
            await start_task
        self.assertGreaterEqual(self.map_api.ros_control.cancel_calls, 2)
        self.assertEqual(2, sum(command[1]["type"] == "emergency_stop" for command in self.commands))

    async def test_estop_after_goal_acceptance_cannot_be_overwritten_by_start(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        self.api._get_live_pose = self._estop_during_progress_pose
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.start_navigation()
        self.assertEqual("NAVIGATION_START_INTERRUPTED", raised.exception.error_code)
        self.assertTrue(self.api.state_response()["emergency_stop"])
        with self.api._lock:
            self.assertIsNotNone(self.api._stop_commanded_at)

    async def test_estop_after_resume_goal_acceptance_cannot_be_overwritten(self):
        await self.driving_ready()
        await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        await self.api.emergency_stop("initial")
        self.api._get_live_pose = self._estop_during_progress_pose
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.resume_navigation()
        self.assertEqual("RESUME_INTERRUPTED", raised.exception.error_code)
        self.assertTrue(self.api.state_response()["emergency_stop"])

    def _estop_during_progress_pose(self):
        failures = []

        def stop_in_thread():
            try:
                asyncio.run(self.api.emergency_stop("operator"))
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=stop_in_thread)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], failures)
        return {"x": 0.25, "y": 0.5, "yaw": 0.4}


    async def test_rotated_map_bounds_are_enforced(self):
        self.map_api.saved_map.origin_x = 0.0
        self.map_api.saved_map.origin_y = 0.0
        self.map_api.saved_map.origin_yaw = math.pi / 2
        await self.driving_ready()
        await self.api.plan_goal({"x": -1.0, "y": 1.0, "yaw": 0.0})
        with self.assertRaises(NavigationControlError) as raised:
            await self.api.plan_goal({"x": 1.0, "y": 1.0, "yaw": 0.0})
        self.assertEqual("GOAL_OUT_OF_BOUNDS", raised.exception.error_code)

    async def test_speaker_volume_defaults_to_pico_state_and_updates_with_ack(self):
        self.api.note_pi_status({"speaker_volume_percent": 20})
        self.assertEqual(self.api.state_response()["speaker_volume_percent"], 20)
        updated = await self.api.set_speaker_volume({"volume_percent": 0})
        self.assertEqual(updated["volume_percent"], 0)
        self.assertEqual(self.commands[-1][1], {"type": "set_volume", "volume_percent": 0})
        self.assertEqual(self.api.state_response()["speaker_volume_percent"], 0)

        with self.assertRaises(NavigationControlError):
            await self.api.set_speaker_volume({"volume_percent": 101})
        with self.assertRaises(NavigationControlError):
            await self.api.set_speaker_volume({"volume_percent": True})
        with self.assertRaises(NavigationControlError):
            await self.api.set_speaker_volume({"volume_percent": 20.5})

    async def test_speaker_volume_requires_ack_and_refreshes_status_on_reconnect(self):
        self.api.note_pi_status({"speaker_volume_percent": 20})
        self.api._send_robot_command = lambda *_: asyncio.sleep(0, result=False)
        with self.assertRaises(NavigationControlError) as rejected:
            await self.api.set_speaker_volume({"volume_percent": 90})
        self.assertEqual(rejected.exception.error_code, "VOLUME_NOT_CONFIRMED")
        self.assertEqual(self.api.state_response()["speaker_volume_percent"], 20)
        self.api.note_pi_connection(False)
        self.assertIsNone(self.api.state_response()["speaker_volume_percent"])
        self.api.note_pi_status({"speaker_volume_percent": 20})
        self.assertEqual(self.api.state_response()["speaker_volume_percent"], 20)

    async def test_beep_led_and_warning_use_single_robot_command_path(self):
        beep = await self.api.beep({"duration_ms": 350})
        led = await self.api.led_test({"duration_ms": 500})
        warning = await self.api.warning({"text": "warning", "led_duration_ms": 700})
        self.assertTrue(beep["delivered"])
        self.assertTrue(led["delivered"])
        self.assertTrue(warning["delivered"])
        self.assertEqual(
            ["beep", "led", "warning"],
            [item[1]["type"] for item in self.commands[-3:]],
        )
        self.assertEqual(350, self.commands[-3][1]["duration_ms"])

    async def test_persistent_led_control_tracks_state(self):
        enabled = await self.api.set_led({"enabled": True})
        self.assertTrue(enabled["led_enabled"])
        self.assertEqual(
            {"type": "led", "enabled": True, "duration_ms": 0},
            self.commands[-1][1],
        )

        disabled = await self.api.set_led({"enabled": False})
        self.assertFalse(disabled["led_enabled"])
        self.assertEqual(
            {"type": "led", "enabled": False, "duration_ms": 0},
            self.commands[-1][1],
        )

        self.api.note_pi_status({"led_enabled": True})
        self.assertTrue(self.api.state_response()["led_enabled"])

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

        denied_volume = client.post(
            "/api/navigation/control/speaker-volume", json={"volume_percent": 0},
        )
        self.assertEqual(403, denied_volume.status_code)
        accepted_volume = client.post(
            "/api/navigation/control/speaker-volume",
            json={"volume_percent": 20},
            headers={"X-CSRF-Token": "test-token"},
        )
        self.assertEqual(200, accepted_volume.status_code)
        self.assertEqual(20, accepted_volume.json()["volume_percent"])

        denied_beep = client.post(
            "/api/navigation/control/beep",
            json={"duration_ms": 350},
        )
        self.assertEqual(403, denied_beep.status_code)
        accepted_beep = client.post(
            "/api/navigation/control/beep",
            json={"duration_ms": 350},
            headers={"X-CSRF-Token": "test-token"},
        )
        self.assertEqual(200, accepted_beep.status_code)
        self.assertTrue(accepted_beep.json()["delivered"])
        self.assertEqual(350, accepted_beep.json()["duration_ms"])

        denied = client.post("/api/navigation/control/led", json={"enabled": True})
        self.assertEqual(403, denied.status_code)
        accepted = client.post(
            "/api/navigation/control/led",
            json={"enabled": True},
            headers={"X-CSRF-Token": "test-token"},
        )
        self.assertEqual(200, accepted.status_code)
        self.assertTrue(accepted.json()["led_enabled"])

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


class NavigationRosRotationTelemetryTests(unittest.TestCase):
    @staticmethod
    def odom(yaw, stamp_ns=None):
        message = SimpleNamespace(pose=SimpleNamespace(pose=SimpleNamespace(
            orientation=SimpleNamespace(x=0.0, y=0.0, z=math.sin(yaw / 2), w=math.cos(yaw / 2)),
        )))
        if stamp_ns is not None:
            message.header = SimpleNamespace(stamp=SimpleNamespace(
                sec=stamp_ns // 1_000_000_000, nanosec=stamp_ns % 1_000_000_000,
            ))
        return message

    def test_nav2_command_and_odom_yaw_are_sampled_with_freshness(self):
        ros = NavigationRosControl()
        ros._on_nav_command(SimpleNamespace(
            linear=SimpleNamespace(x=0.0), angular=SimpleNamespace(z=0.4),
        ))
        ros._on_odom(self.odom(3.13))
        ros._on_odom(self.odom(-3.13))
        status = ros.rotation_status()
        self.assertEqual(2, status["odom_sequence"])
        self.assertAlmostEqual(-3.13, status["odom_yaw"])
        self.assertGreater(status["odom_yaw_delta"], 0.005)
        self.assertLess(status["command_age_sec"], 0.5)
        self.assertLess(status["odometry_age_sec"], 0.5)
        self.assertGreaterEqual(status["odom_interval_start_at"], status["command_since_at"])

        with ros._lock:
            ros._last_nav_command_at -= 1.0
            ros._last_odom_at -= 1.0
        stale = ros.rotation_status()
        self.assertGreater(stale["command_age_sec"], 0.5)
        self.assertGreater(stale["odometry_age_sec"], 0.5)

    def test_stale_and_replayed_odom_source_stamps_are_rejected(self):
        ros = NavigationRosControl()
        ros._node = SimpleNamespace(get_clock=lambda: SimpleNamespace(
            now=lambda: SimpleNamespace(nanoseconds=10_000_000_000),
        ))
        ros._on_odom(self.odom(0.0, 9_000_000_000))
        self.assertEqual(0, ros.rotation_status()["odom_sequence"])
        ros._on_odom(self.odom(0.0, 9_900_000_000))
        ros._on_odom(self.odom(0.4, 9_900_000_000))
        self.assertEqual(1, ros.rotation_status()["odom_sequence"])
        ros._on_odom(self.odom(0.4, 9_950_000_000))
        self.assertEqual(2, ros.rotation_status()["odom_sequence"])


if __name__ == "__main__":
    unittest.main()
