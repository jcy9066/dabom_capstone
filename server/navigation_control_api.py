"""Server-authoritative navigation/control state and FastAPI contract."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from server.navigation_map_service import NavigationMapError
from server.navigation_process_control import NavigationProcessError
from server.navigation_ros_control import NavigationRosError
from server.env_config import env_float, env_int, env_text


class NavigationControlError(RuntimeError):
    def __init__(self, error_code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


@dataclass(frozen=True)
class NavigationWatchdogConfig:
    interval_sec: float
    lidar_timeout_sec: float
    odometry_timeout_sec: float
    tf_timeout_sec: float
    localization_timeout_sec: float
    pi_timeout_sec: float
    stop_encoder_grace_sec: float
    stop_encoder_tick_threshold: int

    @classmethod
    def from_env(cls) -> "NavigationWatchdogConfig":
        return cls(
            interval_sec=env_float("NAV_WATCHDOG_INTERVAL_SEC", minimum=0.05),
            lidar_timeout_sec=env_float("NAV_WATCHDOG_LIDAR_TIMEOUT_SEC", minimum=0.1),
            odometry_timeout_sec=env_float("NAV_WATCHDOG_ODOM_TIMEOUT_SEC", minimum=0.1),
            tf_timeout_sec=env_float("NAV_WATCHDOG_TF_TIMEOUT_SEC", minimum=0.1),
            localization_timeout_sec=env_float("NAV_WATCHDOG_LOCALIZATION_TIMEOUT_SEC", minimum=0.1),
            pi_timeout_sec=env_float("NAV_WATCHDOG_PI_TIMEOUT_SEC", minimum=0.1),
            stop_encoder_grace_sec=env_float("NAV_WATCHDOG_STOP_ENCODER_GRACE_SEC", minimum=0.0),
            stop_encoder_tick_threshold=env_int("NAV_WATCHDOG_STOP_ENCODER_TICKS", minimum=1),
        )


class NavigationControlApi:
    MODES = frozenset({"MAPPING", "DRIVING"})
    ACTIVE_NAV_STATES = frozenset({"NAVIGATING", "RESUMING"})

    def __init__(
        self,
        app: FastAPI,
        map_api: Any,
        process_control: Any,
        csrf_failure: Callable[[Request], JSONResponse | None],
        robot_id: str,
        get_live_map: Callable[[], dict[str, Any] | None],
        save_map: Callable[[dict[str, Any], str | None], dict[str, Any]],
        send_robot_command: Callable[[str, dict[str, Any]], Awaitable[bool]],
        on_mode_changed: Callable[[str], None] | None = None,
        watchdog: NavigationWatchdogConfig | None = None,
        motor_output_enabled: bool = False,
        estop_cooldown_sec: float | None = None,
        goal_reached_tolerance_m: float | None = None,
    ) -> None:
        self._app = app
        self._map_api = map_api
        self._process = process_control
        self._ros = map_api.ros_control
        self._csrf_failure = csrf_failure
        self._robot_id = robot_id
        self._get_live_map = get_live_map
        self._save_map = save_map
        self._send_robot_command = send_robot_command
        self._on_mode_changed = on_mode_changed
        self._watchdog = watchdog or NavigationWatchdogConfig.from_env()
        self._motor_output_enabled = bool(motor_output_enabled)
        self._driving_ready_timeout_sec = env_float(
            "NAV_DRIVING_READY_TIMEOUT_SEC", minimum=1.0
        )
        self._estop_cooldown_sec = (
            env_float("DASHBOARD_ESTOP_COOLDOWN_SEC", minimum=0.0)
            if estop_cooldown_sec is None
            else float(estop_cooldown_sec)
        )
        self._goal_reached_tolerance_m = (
            env_float("DASHBOARD_GOAL_REACHED_TOLERANCE_M", minimum=0.0)
            if goal_reached_tolerance_m is None
            else float(goal_reached_tolerance_m)
        )
        if not math.isfinite(self._estop_cooldown_sec) or self._estop_cooldown_sec < 0:
            raise ValueError("estop_cooldown_sec must be a non-negative finite number.")
        if (
            not math.isfinite(self._goal_reached_tolerance_m)
            or self._goal_reached_tolerance_m < 0
        ):
            raise ValueError("goal_reached_tolerance_m must be a non-negative finite number.")
        self._lock = threading.RLock()
        self._operation_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._connected = False
        self._pi_updated_at: float | None = None
        self._robot_mode = "manual"
        self._pi_navigation_mode: str | None = None
        self._last_scan_at: float | None = None
        self._last_pose_at: float | None = None
        self._encoder_ticks: tuple[int, int, int, int] | None = None
        self._stop_encoder_ticks: tuple[int, int, int, int] | None = None
        self._stop_commanded_at: float | None = None
        self._encoder_stop_violation = False
        self._estop_generation = 0
        self._state: dict[str, Any] = {
            "navigation_mode": env_text("NAVIGATION_DEFAULT_MODE").upper(),
            "navigation_state": "IDLE",
            "emergency_stop": False,
            "emergency_reason": None,
            "active_goal": None,
            "planned_path": [],
            "active_map": None,
            "localization_ready": False,
            "nav2_ready": False,
            "last_error": None,
            "updated_at": time.time(),
        }
        if self._state["navigation_mode"] not in self.MODES:
            self._state["navigation_mode"] = "MAPPING"
        if hasattr(self._map_api, "set_active_listener"):
            self._map_api.set_active_listener(self.note_active_map)
        if hasattr(self._map_api, "set_control_loader"):
            self._map_api.set_control_loader(self.load_existing_map)
        self._attach_routes()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._watchdog_loop(), name="navigation-control-watchdog")

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def note_pi_connection(self, connected: bool, now: float | None = None) -> None:
        with self._lock:
            self._connected = bool(connected)
            if connected:
                self._pi_updated_at = now or time.time()

    def note_pi_status(self, payload: dict[str, Any], now: float | None = None) -> None:
        if not isinstance(payload, dict):
            return
        received_at = now or time.time()
        with self._lock:
            self._connected = True
            self._pi_updated_at = received_at
            reported_mode = str(payload.get("mode", "")).strip().lower()
            if reported_mode in {"auto", "manual"}:
                self._robot_mode = reported_mode
            reported_navigation = str(payload.get("navigation_mode", "")).strip().upper()
            if reported_navigation in self.MODES:
                self._pi_navigation_mode = reported_navigation
                self._state["navigation_mode"] = reported_navigation
            if isinstance(payload.get("emergency_stop"), bool):
                if payload["emergency_stop"]:
                    self._state["emergency_stop"] = True
                    self._state["navigation_state"] = "EMERGENCY_STOPPED"
                elif not self._state["emergency_stop"]:
                    self._state["emergency_stop"] = False
            self._touch_locked(received_at)

    def note_navigation_sample(
        self,
        sample: str,
        now: float | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        received_at = now or time.time()
        with self._lock:
            if sample == "scan":
                self._last_scan_at = received_at
            elif sample == "pose":
                self._last_pose_at = received_at
                self._complete_manual_goal_locked(payload)

    def note_active_map(self, active_map: dict[str, Any]) -> None:
        if not isinstance(active_map, dict):
            return
        with self._lock:
            self._state["active_map"] = dict(active_map)
            self._state["localization_ready"] = True
            self._touch_locked()

    def note_encoder(self, payload: dict[str, Any], now: float | None = None) -> None:
        keys = ("left_front_ticks", "right_front_ticks", "left_rear_ticks", "right_rear_ticks")
        try:
            ticks = tuple(int(payload[key]) for key in keys)
        except (KeyError, TypeError, ValueError):
            return
        with self._lock:
            self._encoder_ticks = ticks
            if (
                self._stop_commanded_at is not None
                and (now or time.time()) - self._stop_commanded_at >= self._watchdog.stop_encoder_grace_sec
                and self._stop_encoder_ticks is not None
                and max(
                    abs(current - baseline)
                    for current, baseline in zip(ticks, self._stop_encoder_ticks)
                ) >= self._watchdog.stop_encoder_tick_threshold
            ):
                self._encoder_stop_violation = True

    def state_response(self, now: float | None = None) -> dict[str, Any]:
        current = now or time.time()
        try:
            ros_nav = self._ros.navigation_status()
        except Exception:
            ros_nav = {"state": "UNAVAILABLE"}
        try:
            live_path = (
                self._normalized_path(self._ros.latest_planned_path())
                if hasattr(self._ros, "latest_planned_path")
                else []
            )
        except Exception:
            live_path = []
        with self._lock:
            if (
                self._state["navigation_state"] in self.ACTIVE_NAV_STATES
                and self._state["active_goal"] is not None
                and len(live_path) >= 2
            ):
                self._state["planned_path"] = live_path
            if self._state["navigation_state"] == "NAVIGATING":
                terminal = str(ros_nav.get("state", "")).upper()
                if terminal in {"SUCCEEDED", "FAILED", "CANCELED"}:
                    self._state["navigation_state"] = terminal
                    if terminal in {"SUCCEEDED", "CANCELED"}:
                        self._state["active_goal"] = None
                        self._state["planned_path"] = []
                    self._touch_locked(current)
            payload = {
                "ok": True,
                "robot_id": self._robot_id,
                "connected": self._connected,
                "robot_mode": self._robot_mode,
                "pi_navigation_mode": self._pi_navigation_mode,
                "motor_output_enabled": self._motor_output_enabled,
                "dry_run": not self._motor_output_enabled,
                **self._copy_state_locked(),
                "ros_navigation": ros_nav,
                "watchdog": self._watchdog_snapshot_locked(current),
                "dashboard_config": {
                    "estop_cooldown_sec": self._estop_cooldown_sec,
                    "goal_reached_tolerance_m": self._goal_reached_tolerance_m,
                },
            }
        return payload

    async def switch_mode(self, payload: dict[str, Any], user: str = "unknown") -> dict[str, Any]:
        mode = str(payload.get("mode", "")).strip().upper()
        if mode not in self.MODES:
            raise NavigationControlError("INVALID_MODE", "mode must be MAPPING or DRIVING.", 400)

        if mode == "MAPPING":
            with self._lock:
                navigation_in_progress = self._state["navigation_state"] in {
                    "NAVIGATING",
                    "RESUMING",
                }
            if navigation_in_progress and payload.get("confirm_stop") is not True:
                raise NavigationControlError(
                    "NAVIGATION_STOP_CONFIRMATION_REQUIRED",
                    "Confirm emergency stop before switching to MAPPING mode.",
                    409,
                )
            if navigation_in_progress:
                await self.emergency_stop("MAPPING_MODE_TRANSITION")
            else:
                await asyncio.to_thread(self._ros.cancel_navigation)
            try:
                await asyncio.to_thread(self._process.transition, "MAPPING", None)
            except Exception as exc:
                with self._lock:
                    self._state["navigation_state"] = "ERROR"
                    self._state["last_error"] = str(exc)
                    self._touch_locked()
                raise
            delivered = await self._send_robot_command(
                self._robot_id,
                {"type": "navigation_mode", "mode": "mapping"},
            )
            with self._lock:
                self._state.update(
                    {
                        "navigation_mode": "MAPPING",
                        "navigation_state": (
                            "EMERGENCY_STOPPED" if self._state["emergency_stop"] else "IDLE"
                        ),
                        "active_goal": None,
                        "planned_path": [],
                        "active_map": None,
                        "localization_ready": False,
                        "nav2_ready": False,
                        "last_error": None,
                    }
                )
                if self._connected and not delivered:
                    self._state["last_error"] = "PI_MODE_SYNC_FAILED"
                self._touch_locked()
            self._notify_mode_changed("MAPPING")
            return self.state_response()

        source = str(payload.get("source", "existing")).strip().lower()
        map_name = payload.get("map_name")
        if source == "save_current":
            live_map = self._get_live_map()
            if not live_map:
                raise NavigationControlError("MAP_UNAVAILABLE", "The current mapping result is unavailable.", 404)
            saved = await asyncio.to_thread(self._save_map, dict(live_map), map_name)
            map_name = saved.get("map_name")
        elif source != "existing":
            raise NavigationControlError("INVALID_SOURCE", "source must be existing or save_current.", 400)

        selected = self._map_api.resolve_map(map_name)
        try:
            await asyncio.to_thread(self._process.transition, "DRIVING", str(selected.yaml_path))
            loaded = await self._activate_map_when_ready(
                {"map_name": selected.map_name, "initial_pose": payload.get("initial_pose")},
                user,
            )
            await self._wait_for_nav2_ready()
        except Exception as exc:
            try:
                await asyncio.to_thread(self._process.stop, "DRIVING")
            except Exception:
                pass
            with self._lock:
                self._state["navigation_state"] = "ERROR"
                self._state["last_error"] = str(exc)
                self._state["localization_ready"] = False
                self._state["nav2_ready"] = False
                self._touch_locked()
            raise
        delivered = await self._send_robot_command(
            self._robot_id,
            {"type": "navigation_mode", "mode": "driving"},
        )
        with self._lock:
            self._state.update(
                {
                    "navigation_mode": "DRIVING",
                    "navigation_state": (
                        "EMERGENCY_STOPPED" if self._state["emergency_stop"] else "READY"
                    ),
                    "active_goal": None,
                    "planned_path": [],
                    "active_map": loaded.get("active_map"),
                    "localization_ready": True,
                    "nav2_ready": True,
                    "last_error": None,
                }
            )
            if self._connected and not delivered:
                self._state["last_error"] = "PI_MODE_SYNC_FAILED"
            self._touch_locked()
        self._notify_mode_changed("DRIVING")
        return self.state_response()

    async def load_existing_map(
        self,
        payload: dict[str, Any],
        user: str = "unknown",
    ) -> dict[str, Any]:
        request = {
            "mode": "DRIVING",
            "source": "existing",
            "map_name": payload.get("map_name"),
            "initial_pose": payload.get("initial_pose"),
        }
        async with self._operation_lock:
            return await self.switch_mode(request, user=user)

    async def plan_goal(self, payload: dict[str, Any]) -> dict[str, Any]:
        goal = self._validated_goal(payload.get("goal", payload))
        with self._lock:
            if self._state["navigation_mode"] != "DRIVING":
                raise NavigationControlError("DRIVING_MODE_REQUIRED", "Goal poses require DRIVING mode.")
            if not self._state["localization_ready"] or not self._state["nav2_ready"]:
                raise NavigationControlError("NAVIGATION_NOT_READY", "Localization and Nav2 must be ready.")
            if self._state["emergency_stop"]:
                raise NavigationControlError("EMERGENCY_STOP_ACTIVE", "Resume before planning a new goal.")
        self._validate_goal_in_active_map(goal)
        path = await asyncio.to_thread(self._ros.compute_path, goal)
        normalized_path = self._normalized_path(path)
        if len(normalized_path) < 2:
            raise NavigationControlError("PATH_NOT_FOUND", "Nav2 did not return a usable path.", 422)
        with self._lock:
            self._state["active_goal"] = goal
            self._state["planned_path"] = normalized_path
            self._state["navigation_state"] = (
                "EMERGENCY_STOPPED" if self._state["emergency_stop"] else "PATH_READY"
            )
            self._state["last_error"] = None
            self._touch_locked()
        return self.state_response()

    async def start_navigation(self) -> dict[str, Any]:
        with self._lock:
            goal = dict(self._state["active_goal"] or {})
            path_ready = len(self._state["planned_path"]) >= 2
            estop_generation = self._estop_generation
            self._assert_resume_safety_locked(require_estop=False)
            if not goal or not path_ready:
                raise NavigationControlError("PATH_REQUIRED", "Plan a goal before starting navigation.")
        self._assert_watchdogs_healthy()
        result = await asyncio.to_thread(self._ros.navigate_to_pose, goal)
        with self._lock:
            interrupted = (
                self._estop_generation != estop_generation
                or self._state["emergency_stop"]
            )
        if interrupted:
            await self.emergency_stop("NAVIGATION_START_INTERRUPTED", automatic=True)
            raise NavigationControlError(
                "NAVIGATION_START_INTERRUPTED",
                "An emergency stop interrupted navigation startup.",
                409,
            )
        with self._lock:
            self._state["navigation_state"] = "NAVIGATING"
            self._state["last_error"] = None
            self._touch_locked()
        return {**self.state_response(), "navigation": result}

    async def cancel_goal(self) -> dict[str, Any]:
        cancel = None
        cancel_error = None
        try:
            cancel = await asyncio.to_thread(self._ros.cancel_navigation)
        except Exception as exc:
            cancel_error = exc
        delivered = await self._send_robot_command(
            self._robot_id,
            {"type": "stop", "reason": "navigation_goal_cancel"},
        )
        with self._lock:
            self._stop_commanded_at = time.time()
            self._stop_encoder_ticks = self._encoder_ticks
            if cancel_error is None:
                self._state["navigation_state"] = (
                    "EMERGENCY_STOPPED" if self._state["emergency_stop"] else "READY"
                )
                self._state["active_goal"] = None
                self._state["planned_path"] = []
            self._touch_locked()
        if cancel_error is not None:
            raise cancel_error
        return {**self.state_response(), "cancel": cancel, "stop_delivered": delivered}

    async def pause_for_manual(self) -> dict[str, Any]:
        """Stop the active ROS action while retaining the goal for manual arrival."""
        with self._lock:
            previous_navigation_state = self._state["navigation_state"]
            self._state["navigation_state"] = "PAUSING_FOR_MANUAL"
            self._touch_locked()
        cancel = None
        cancel_error = None
        try:
            cancel = await asyncio.to_thread(self._ros.cancel_navigation)
        except Exception as exc:
            cancel_error = exc
        delivered = await self._send_robot_command(
            self._robot_id,
            {"type": "stop", "reason": "manual_drive_takeover"},
        )
        with self._lock:
            self._stop_commanded_at = time.time()
            self._stop_encoder_ticks = self._encoder_ticks
            if cancel_error is None:
                if self._state["emergency_stop"]:
                    self._state["navigation_state"] = "EMERGENCY_STOPPED"
                elif self._state["navigation_mode"] == "MAPPING":
                    self._state["navigation_state"] = "IDLE"
                else:
                    self._state["navigation_state"] = "READY"
                self._state["last_error"] = None
            else:
                self._state["navigation_state"] = previous_navigation_state
                self._state["last_error"] = str(cancel_error)
            self._touch_locked()
        if cancel_error is not None:
            raise cancel_error
        return {
            **self.state_response(),
            "cancel": cancel,
            "stop_delivered": delivered,
            "goal_retained": self._state["active_goal"] is not None,
        }

    async def emergency_stop(self, reason: str, automatic: bool = False) -> dict[str, Any]:
        normalized_reason = (str(reason).strip() or "dashboard_emergency_stop")[:96]
        with self._lock:
            self._state["emergency_stop"] = True
            self._state["emergency_reason"] = normalized_reason
            self._state["navigation_state"] = "EMERGENCY_STOPPED"
            self._stop_commanded_at = time.time()
            self._stop_encoder_ticks = self._encoder_ticks
            self._encoder_stop_violation = False
            self._estop_generation += 1
            self._touch_locked()
        cancel, delivered = await asyncio.gather(
            asyncio.to_thread(self._ros.cancel_navigation),
            self._send_robot_command(
                self._robot_id,
                {"type": "emergency_stop", "reason": normalized_reason, "automatic": automatic},
            ),
        )
        return {**self.state_response(), "cancel": cancel, "stop_delivered": delivered, "automatic": automatic}

    async def resume_navigation(self) -> dict[str, Any]:
        with self._lock:
            goal = dict(self._state["active_goal"] or {})
            estop_generation = self._estop_generation
            if not self._state["emergency_stop"]:
                raise NavigationControlError(
                    "EMERGENCY_STOP_NOT_ACTIVE",
                    "Navigation is not emergency-stopped.",
                )
            if not self._connected:
                raise NavigationControlError(
                    "PI_OFFLINE",
                    "The Pi must be connected before releasing emergency stop.",
                )
            restart_navigation = bool(
                goal
                and self._state["navigation_mode"] == "DRIVING"
                and self._state["localization_ready"]
                and self._state["nav2_ready"]
                and self._robot_mode == "auto"
                and self._pi_navigation_mode == "DRIVING"
            )
        path: list[dict[str, float]] = []
        if restart_navigation:
            self._assert_watchdogs_healthy(ignore_estop=True)
            path = self._normalized_path(await asyncio.to_thread(self._ros.compute_path, goal))
            if len(path) < 2:
                raise NavigationControlError(
                    "PATH_NOT_FOUND",
                    "A safe resume path could not be calculated.",
                    422,
                )
        delivered = await self._send_robot_command(self._robot_id, {"type": "resume_navigation"})
        if not delivered:
            raise NavigationControlError("PI_RESUME_FAILED", "The Pi did not accept the resume command.", 409)
        with self._lock:
            if self._estop_generation != estop_generation or not self._state["emergency_stop"]:
                retry_stop = True
            else:
                retry_stop = False
                if restart_navigation:
                    self._state["planned_path"] = path
                self._state["emergency_stop"] = False
                self._state["emergency_reason"] = None
                self._state["navigation_state"] = (
                    "RESUMING"
                    if restart_navigation
                    else ("READY" if self._state["navigation_mode"] == "DRIVING" else "IDLE")
                )
                self._stop_commanded_at = None
                self._stop_encoder_ticks = None
                self._touch_locked()
        if retry_stop:
            await self.emergency_stop("RESUME_INTERRUPTED", automatic=True)
            raise NavigationControlError("RESUME_INTERRUPTED", "A newer emergency stop interrupted resume.", 409)
        if not restart_navigation:
            return {**self.state_response(), "replanned": False}
        try:
            navigation = await asyncio.to_thread(self._ros.navigate_to_pose, goal)
        except Exception:
            await self.emergency_stop("RESUME_NAVIGATION_FAILED", automatic=True)
            raise
        with self._lock:
            interrupted = (
                self._estop_generation != estop_generation
                or self._state["emergency_stop"]
            )
        if interrupted:
            await self.emergency_stop("RESUME_INTERRUPTED", automatic=True)
            raise NavigationControlError(
                "RESUME_INTERRUPTED",
                "A newer emergency stop interrupted navigation restart.",
                409,
            )
        with self._lock:
            self._state["navigation_state"] = "NAVIGATING"
            self._touch_locked()
        return {**self.state_response(), "navigation": navigation, "replanned": True}

    async def led_test(self, payload: dict[str, Any]) -> dict[str, Any]:
        duration_ms = self._bounded_int(payload.get("duration_ms", 1000), 0, 10000, "duration_ms")
        delivered = await self._send_robot_command(
            self._robot_id,
            {"type": "led", "enabled": True, "duration_ms": duration_ms},
        )
        if not delivered:
            raise NavigationControlError("PI_OFFLINE", "The LED test command was not delivered.", 409)
        return {"ok": True, "delivered": True, "duration_ms": duration_ms}

    async def warning(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("text") or "경고합니다. 즉시 물러나십시오.").strip()[:240]
        duration_ms = self._bounded_int(payload.get("led_duration_ms", 3000), 0, 10000, "led_duration_ms")
        delivered = await self._send_robot_command(
            self._robot_id,
            {"type": "warning", "text": text, "led_duration_ms": duration_ms},
        )
        if not delivered:
            raise NavigationControlError("PI_OFFLINE", "The warning command was not delivered.", 409)
        return {"ok": True, "delivered": True, "led_duration_ms": duration_ms}

    async def evaluate_watchdog(self, now: float | None = None) -> str | None:
        current = now or time.time()
        with self._lock:
            if self._encoder_stop_violation:
                issue = "ENCODER_MOVEMENT_AFTER_STOP"
            elif self._state["navigation_state"] not in self.ACTIVE_NAV_STATES:
                return None
            else:
                issue = self._watchdog_issue_locked(current)
        if issue:
            await self.emergency_stop(issue, automatic=True)
        return issue

    async def _activate_map_when_ready(self, payload: dict[str, Any], user: str) -> dict[str, Any]:
        deadline = time.monotonic() + self._driving_ready_timeout_sec
        retry_codes = {"MAP_SERVER_UNAVAILABLE", "LOCALIZATION_NOT_ACTIVE", "ROS_UNAVAILABLE"}
        while True:
            try:
                return await self._map_api.activate_map(payload, user=user)
            except NavigationRosError as exc:
                if exc.error_code not in retry_codes or time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.5)

    async def _wait_for_nav2_ready(self) -> None:
        if not hasattr(self._ros, "navigation_ready"):
            return
        deadline = time.monotonic() + self._driving_ready_timeout_sec
        while not await asyncio.to_thread(self._ros.navigation_ready):
            if time.monotonic() >= deadline:
                raise NavigationControlError(
                    "NAV2_NOT_READY",
                    "Nav2 planning and navigation actions did not become ready.",
                    503,
                )
            await asyncio.sleep(0.25)

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(self._watchdog.interval_sec)
            try:
                await self.evaluate_watchdog()
            except asyncio.CancelledError:
                raise
            except Exception:
                # State reads remain available even if a health adapter temporarily fails.
                continue

    def _notify_mode_changed(self, mode: str) -> None:
        callback = self._on_mode_changed
        if callback is None:
            return
        try:
            callback(mode)
        except Exception:
            # Visualization state must never make a navigation transition fail.
            return

    def _assert_resume_safety_locked(self, require_estop: bool) -> None:
        if self._state["navigation_mode"] != "DRIVING":
            raise NavigationControlError("DRIVING_MODE_REQUIRED", "Navigation requires DRIVING mode.")
        if not self._state["localization_ready"] or not self._state["nav2_ready"]:
            raise NavigationControlError("NAVIGATION_NOT_READY", "Localization and Nav2 are not ready.")
        if not self._connected:
            raise NavigationControlError("PI_OFFLINE", "The Pi must be connected before navigation.")
        if self._robot_mode != "auto":
            raise NavigationControlError("AUTO_MODE_REQUIRED", "The Pi must report AUTO mode before navigation.")
        if self._pi_navigation_mode != "DRIVING":
            raise NavigationControlError("PI_DRIVING_MODE_REQUIRED", "The Pi must report DRIVING mode before navigation.")
        if require_estop and not self._state["emergency_stop"]:
            raise NavigationControlError("EMERGENCY_STOP_NOT_ACTIVE", "Navigation is not emergency-stopped.")
        if not require_estop and self._state["emergency_stop"]:
            raise NavigationControlError("EMERGENCY_STOP_ACTIVE", "Resume before starting navigation.")

    def _assert_watchdogs_healthy(self, ignore_estop: bool = False) -> None:
        with self._lock:
            issue = self._watchdog_issue_locked(time.time(), ignore_estop=ignore_estop)
        if issue:
            raise NavigationControlError("WATCHDOG_NOT_READY", f"Unsafe navigation health: {issue}.")

    def _watchdog_issue_locked(self, now: float, ignore_estop: bool = False) -> str | None:
        if not ignore_estop and self._state["emergency_stop"]:
            return "EMERGENCY_STOP_ACTIVE"
        if not self._connected or self._age(now, self._pi_updated_at) > self._watchdog.pi_timeout_sec:
            return "PI_CONNECTION_LOSS"
        try:
            health = self._ros.watchdog_status(now=now)
        except Exception:
            health = {}
        ros_scan_age = self._age_value(health.get("scan_age_sec"))
        if health.get("available") is True and math.isfinite(ros_scan_age):
            lidar_age = ros_scan_age
        else:
            lidar_age = self._age(now, self._last_scan_at)
        if lidar_age > self._watchdog.lidar_timeout_sec:
            return "LIDAR_DATA_LOSS"
        if self._age_value(health.get("odometry_age_sec")) > self._watchdog.odometry_timeout_sec:
            return "ODOMETRY_LOSS"
        if health.get("tf_ok") is not True or self._age_value(health.get("tf_age_sec")) > self._watchdog.tf_timeout_sec:
            return "REQUIRED_TF_FAILURE"
        if health.get("localization_ok") is not True or self._age_value(health.get("localization_age_sec")) > self._watchdog.localization_timeout_sec:
            return "LOCALIZATION_LOST"
        return None

    def _watchdog_snapshot_locked(self, now: float) -> dict[str, Any]:
        try:
            ros = self._ros.watchdog_status(now=now)
        except Exception:
            ros = {"available": False}
        return {
            "config": {
                "lidar_timeout_sec": self._watchdog.lidar_timeout_sec,
                "odometry_timeout_sec": self._watchdog.odometry_timeout_sec,
                "tf_timeout_sec": self._watchdog.tf_timeout_sec,
                "localization_timeout_sec": self._watchdog.localization_timeout_sec,
                "pi_timeout_sec": self._watchdog.pi_timeout_sec,
                "stop_encoder_grace_sec": self._watchdog.stop_encoder_grace_sec,
                "stop_encoder_tick_threshold": self._watchdog.stop_encoder_tick_threshold,
            },
            "pi_age_sec": self._public_age(now, self._pi_updated_at),
            "lidar_age_sec": self._public_age(now, self._last_scan_at),
            "pose_age_sec": self._public_age(now, self._last_pose_at),
            "ros": self._json_safe(ros),
        }

    def _validated_goal(self, payload: Any) -> dict[str, float]:
        if not isinstance(payload, dict):
            raise NavigationControlError("INVALID_GOAL", "goal must be an object.", 400)
        try:
            goal = {"x": float(payload["x"]), "y": float(payload["y"]), "yaw": float(payload["yaw"])}
        except (KeyError, TypeError, ValueError) as exc:
            raise NavigationControlError("INVALID_GOAL", "goal x, y, and yaw must be finite numbers.", 400) from exc
        if not all(math.isfinite(value) for value in goal.values()):
            raise NavigationControlError("INVALID_GOAL", "goal values must be finite.", 400)
        goal["yaw"] = math.atan2(math.sin(goal["yaw"]), math.cos(goal["yaw"]))
        return goal

    def _validate_goal_in_active_map(self, goal: dict[str, float]) -> None:
        with self._lock:
            active = self._state.get("active_map") or {}
        map_name = active.get("map_name")
        if not map_name:
            raise NavigationControlError("ACTIVE_MAP_REQUIRED", "Select an active saved map first.")
        selected = self._map_api.resolve_map(map_name)
        dx = goal["x"] - selected.origin_x
        dy = goal["y"] - selected.origin_y
        yaw = float(getattr(selected, "origin_yaw", 0.0))
        local_x = math.cos(yaw) * dx + math.sin(yaw) * dy
        local_y = -math.sin(yaw) * dx + math.cos(yaw) * dy
        if not (0.0 <= local_x < selected.width * selected.resolution and 0.0 <= local_y < selected.height * selected.resolution):
            raise NavigationControlError("GOAL_OUT_OF_BOUNDS", "The goal is outside the active map.", 422)

    @staticmethod
    def _normalized_path(path: Any) -> list[dict[str, float]]:
        if not isinstance(path, list):
            return []
        output = []
        for point in path[:5000]:
            if not isinstance(point, dict):
                continue
            try:
                x, y = float(point["x"]), float(point["y"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(x) and math.isfinite(y):
                output.append({"x": x, "y": y})
        return output

    @staticmethod
    def _bounded_int(value: Any, minimum: int, maximum: int, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise NavigationControlError("INVALID_REQUEST", f"{field} must be an integer.", 400)
        if isinstance(value, str) and not value.strip().lstrip("+-").isdigit():
            raise NavigationControlError("INVALID_REQUEST", f"{field} must be an integer.", 400)
        try:
            normalized = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise NavigationControlError("INVALID_REQUEST", f"{field} must be an integer.", 400) from exc
        if normalized < minimum or normalized > maximum:
            raise NavigationControlError("INVALID_REQUEST", f"{field} must be between {minimum} and {maximum}.", 400)
        return normalized

    def _copy_state_locked(self) -> dict[str, Any]:
        return {
            **self._state,
            "active_goal": dict(self._state["active_goal"]) if self._state["active_goal"] else None,
            "planned_path": [dict(point) for point in self._state["planned_path"]],
            "active_map": dict(self._state["active_map"]) if self._state["active_map"] else None,
        }

    def _complete_manual_goal_locked(self, payload: dict[str, Any] | None) -> None:
        goal = self._state.get("active_goal")
        if (
            self._robot_mode != "manual"
            or self._state.get("navigation_mode") != "DRIVING"
            or self._state.get("emergency_stop")
            or not isinstance(goal, dict)
            or not isinstance(payload, dict)
        ):
            return
        try:
            robot_x = float(payload["x"])
            robot_y = float(payload["y"])
            goal_x = float(goal["x"])
            goal_y = float(goal["y"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return
        if not all(math.isfinite(value) for value in (robot_x, robot_y, goal_x, goal_y)):
            return
        if math.hypot(robot_x - goal_x, robot_y - goal_y) > self._goal_reached_tolerance_m:
            return
        self._state["active_goal"] = None
        self._state["planned_path"] = []
        self._state["navigation_state"] = "SUCCEEDED"
        self._state["last_error"] = None
        self._touch_locked()

    def _touch_locked(self, value: float | None = None) -> None:
        self._state["updated_at"] = value or time.time()

    @staticmethod
    def _age(now: float, value: float | None) -> float:
        return math.inf if value is None else max(0.0, now - value)

    @staticmethod
    def _public_age(now: float, value: float | None) -> float | None:
        return None if value is None else max(0.0, now - value)

    @staticmethod
    def _age_value(value: Any) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return math.inf
        return result if math.isfinite(result) else math.inf

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: cls._json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._json_safe(item) for item in value]
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value

    def _attach_routes(self) -> None:
        @self._app.get("/api/navigation/control/state")
        async def get_navigation_control_state(request: Request):
            denied = self._access(request)
            return denied if denied else await asyncio.to_thread(self.state_response)

        @self._app.post("/api/navigation/control/mode")
        async def set_navigation_control_mode(request: Request):
            return await self._mutation(request, self.switch_mode, include_user=True)

        @self._app.post("/api/navigation/control/goal")
        async def set_navigation_goal(request: Request):
            return await self._mutation(request, self.plan_goal)

        @self._app.post("/api/navigation/control/start")
        async def start_navigation(request: Request):
            return await self._mutation(request, lambda _: self.start_navigation())

        @self._app.post("/api/navigation/control/cancel")
        async def cancel_navigation(request: Request):
            return await self._mutation(request, lambda _: self.cancel_goal())

        @self._app.post("/api/navigation/control/pause-for-manual")
        async def pause_navigation_for_manual(request: Request):
            return await self._mutation(request, lambda _: self.pause_for_manual())

        @self._app.post("/api/navigation/control/emergency-stop")
        async def stop_navigation(request: Request):
            async def action(payload: dict[str, Any]):
                return await self.emergency_stop(payload.get("reason", "dashboard_emergency_stop"))
            return await self._mutation(request, action, serialize=False)

        @self._app.post("/api/navigation/control/resume")
        async def resume_navigation(request: Request):
            return await self._mutation(request, lambda _: self.resume_navigation())

        @self._app.post("/api/navigation/control/led-test")
        async def test_led(request: Request):
            return await self._mutation(request, self.led_test)

        @self._app.post("/api/navigation/control/warning")
        async def send_warning(request: Request):
            return await self._mutation(request, self.warning)

    async def _mutation(
        self,
        request: Request,
        action: Callable[..., Awaitable[dict[str, Any]]],
        include_user: bool = False,
        serialize: bool = True,
    ):
        denied = self._access(request, csrf=True)
        if denied:
            return denied
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return self._error("INVALID_REQUEST", "JSON object required.", 400)
        try:
            async def invoke():
                if include_user:
                    user = request.session.get("user") or {}
                    identity = str(user.get("user_id") or user.get("email") or "unknown")
                    return await action(payload, user=identity)
                return await action(payload)

            if serialize:
                async with self._operation_lock:
                    return await invoke()
            return await invoke()
        except (NavigationControlError, NavigationMapError, NavigationRosError) as exc:
            return self._error(exc.error_code, str(exc), exc.status_code)
        except NavigationProcessError as exc:
            return self._error("NAVIGATION_PROCESS_FAILED", str(exc), 503)
        except Exception as exc:
            return self._error("NAVIGATION_CONTROL_FAILED", str(exc), 500)

    def _access(self, request: Request, csrf: bool = False):
        if not request.session.get("user"):
            return self._error("AUTH_REQUIRED", "Login is required.", 401)
        return self._csrf_failure(request) if csrf else None

    @staticmethod
    def _error(error_code: str, message: str, status_code: int):
        return JSONResponse({"ok": False, "error_code": error_code, "error": message}, status_code=status_code)
