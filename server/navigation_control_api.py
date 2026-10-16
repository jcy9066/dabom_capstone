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
from server.navigation_motion_segments import annotate_path
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
            interval_sec=env_float("NAV_WATCHDOG_INTERVAL_SEC", minimum=0.05, maximum=0.5),
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
    NO_PROGRESS_NAV_STATES = frozenset({"NAVIGATING"})
    BLOCKED_TIMEOUT_SEC = 5.0
    PROGRESS_DISTANCE_M = 0.10
    ROTATION_DATA_MAX_AGE_SEC = 0.5
    ROTATION_MIN_YAW_DELTA_RAD = 0.005
    ROTATION_MAX_CUMULATIVE_SEC = 15.0
    STOP_CONFIRM_STABLE_SEC = 0.5
    STOP_CONFIRM_MAX_AGE_SEC = 0.5
    STOP_CONFIRM_TIMEOUT_SEC = 3.0
    ESTOP_RETRY_INTERVAL_SEC = 1.0
    ESTOP_MAX_ATTEMPTS = 3
    ESTOP_RETRY_WINDOW_SEC = 10.0

    def __init__(
        self,
        app: FastAPI,
        map_api: Any,
        process_control: Any,
        csrf_failure: Callable[[Request], JSONResponse | None],
        robot_id: str,
        get_live_map: Callable[[], dict[str, Any] | None],
        save_map: Callable[[dict[str, Any], str | None], dict[str, Any]],
        send_robot_command: Callable[[str, dict[str, Any]], Awaitable[bool | dict[str, Any]]],
        get_live_pose: Callable[[], dict[str, Any] | None] | None = None,
        clear_visualization: Callable[[], None] | None = None,
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
        self._get_live_pose = get_live_pose or (lambda: None)
        self._save_map = save_map
        self._send_robot_command = send_robot_command
        self._clear_visualization = clear_visualization
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
        self._pi_estop_latched: bool | None = None
        self._pi_safety_session: str | None = None
        self._pi_safety_epoch: int | None = None
        self._pi_seen_safety_sessions: set[str] = set()
        self._pi_session_stop_pending: str | None = None
        self._led_enabled: bool | None = None
        self._last_scan_at: float | None = None
        self._last_pose_at: float | None = None
        self._navigation_progress_pose: tuple[float, float] | None = None
        self._navigation_progress_at: float | None = None
        self._rotation_extension_sec = 0.0
        self._rotation_last_odom_sequence: int | None = None
        self._rotation_last_verified_yaw: float | None = None
        self._rotation_limit_reached = False
        self._blocked_failure_in_progress = False
        self._pending_nav_success: dict[str, Any] | None = None
        self._pending_terminal_stop: dict[str, Any] | None = None
        self._sensor_recovery: dict[str, Any] | None = None
        self._encoder_ticks: tuple[int, int, int, int] | None = None
        self._encoder_sequence: int | None = None
        self._stop_encoder_ticks: tuple[int, int, int, int] | None = None
        self._stop_commanded_at: float | None = None
        self._encoder_stop_violation = False
        self._stop_confirmation_after_sequence: int | None = None
        self._stop_confirmation_ticks: tuple[int, int, int, int] | None = None
        self._stop_confirmation_started_at: float | None = None
        self._stop_confirmation_last_at: float | None = None
        self._stop_confirmation_samples = 0
        self._stop_confirmation_episode = 0
        self._post_resume_motion_ready = False
        self._estop_generation = 0
        self._estop_attempt_reason: str | None = None
        self._estop_attempts = 0
        self._estop_last_attempt_at: float | None = None
        self._estop_command_acknowledged = False
        self._estop_delivery_token = 0
        self._state: dict[str, Any] = {
            "navigation_mode": env_text("NAVIGATION_DEFAULT_MODE").upper(),
            "navigation_state": "IDLE",
            "emergency_stop": False,
            "emergency_reason": None,
            "active_goal": None,
            "waypoints": [],
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
            # A WebSocket connection alone does not prove Pi safety.
            # Require fresh telemetry before authorizing motion.
            self._pi_updated_at = None
            self._pi_estop_latched = None
            if not connected:
                self._encoder_sequence = None
                self._stop_confirmation_after_sequence = None
                self._estop_generation += 1
                if not self._estop_command_acknowledged:
                    self._estop_attempts = 0
                    self._estop_last_attempt_at = None

    def note_pi_status(self, payload: dict[str, Any], now: float | None = None) -> None:
        if not isinstance(payload, dict):
            return
        received_at = now or time.time()
        safety_session = str(payload.get("safety_session") or "").strip()
        try:
            safety_epoch = int(payload["safety_epoch"]) if safety_session else None
        except (KeyError, TypeError, ValueError):
            safety_epoch = None
        with self._lock:
            new_safety_stop = False
            if safety_session and safety_epoch is not None and safety_epoch >= 0:
                if safety_session != self._pi_safety_session:
                    if safety_session in self._pi_seen_safety_sessions:
                        return
                    new_safety_stop = self._pi_safety_session is not None
                    self._pi_safety_session = safety_session
                    self._pi_safety_epoch = safety_epoch
                    self._pi_seen_safety_sessions.add(safety_session)
                    if new_safety_stop:
                        reason = (
                            "ENCODER_MOVEMENT_AFTER_STOP"
                            if self._encoder_stop_violation
                            else "PI_SAFETY_SESSION_CHANGED"
                        )
                        self._pi_session_stop_pending = reason
                        self._estop_attempt_reason = None
                        self._estop_attempts = 0
                        self._estop_last_attempt_at = None
                        self._estop_command_acknowledged = False
                        self._state["emergency_stop"] = True
                        self._state["emergency_reason"] = reason
                        self._state["navigation_state"] = "EMERGENCY_STOPPED"
                        if not self._encoder_stop_violation:
                            self._stop_commanded_at = received_at
                            self._stop_encoder_ticks = self._encoder_ticks
                elif self._pi_safety_epoch is not None and safety_epoch < self._pi_safety_epoch:
                    return
                elif self._pi_safety_epoch is None or safety_epoch > self._pi_safety_epoch:
                    self._pi_safety_epoch = safety_epoch
                    new_safety_stop = payload.get("emergency_stop") is True
            elif self._pi_safety_session is not None:
                if payload.get("emergency_stop") is True:
                    self._estop_generation += 1
                    self._pi_estop_latched = True
                    self._state["emergency_stop"] = True
                    self._state["navigation_state"] = "EMERGENCY_STOPPED"
                    self._touch_locked(received_at)
                return
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
                if new_safety_stop or (payload["emergency_stop"] and self._pi_estop_latched is False):
                    self._estop_generation += 1
                if (
                    not payload["emergency_stop"]
                    and self._pi_estop_latched is not False
                    and self._state["emergency_stop"]
                    and not self._estop_command_acknowledged
                ):
                    self._estop_attempts = 0
                    self._estop_last_attempt_at = None
                self._pi_estop_latched = payload["emergency_stop"]
                if payload["emergency_stop"]:
                    self._state["emergency_stop"] = True
                    self._state["navigation_state"] = "EMERGENCY_STOPPED"
                elif not self._state["emergency_stop"]:
                    self._state["emergency_stop"] = False
            if isinstance(payload.get("led_enabled"), bool):
                self._led_enabled = payload["led_enabled"]
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
                self._note_navigation_progress_locked(payload, received_at)

    def note_replanned_path(self, path: Any) -> None:
        normalized = self._normalized_path(path)
        if len(normalized) < 2:
            return
        with self._lock:
            if (
                self._state["navigation_state"] not in {"NAVIGATING", "RESUMING"}
                or not self._state["active_goal"]
            ):
                return
            self._state["planned_path"] = normalized
            self._touch_locked()

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
        received_at = time.time() if now is None else now
        try:
            sequence = int(payload["sequence"])
        except (KeyError, TypeError, ValueError):
            sequence = None
        with self._lock:
            if sequence is not None:
                if self._encoder_sequence is not None and sequence <= self._encoder_sequence:
                    return
                self._encoder_sequence = sequence
            self._encoder_ticks = ticks
            marker = self._stop_confirmation_after_sequence
            if marker is not None and sequence is not None and sequence > marker:
                baseline = self._stop_confirmation_ticks
                if baseline is None or ticks != baseline:
                    self._stop_confirmation_ticks = ticks
                    self._stop_confirmation_started_at = received_at
                    self._stop_confirmation_samples = 1
                    self._stop_confirmation_episode += 1
                else:
                    self._stop_confirmation_samples += 1
                self._stop_confirmation_last_at = received_at
            if (
                self._stop_commanded_at is not None
                and received_at - self._stop_commanded_at >= self._watchdog.stop_encoder_grace_sec
                and self._stop_encoder_ticks is not None
                and max(
                    abs(current - baseline)
                    for current, baseline in zip(ticks, self._stop_encoder_ticks)
                ) >= self._watchdog.stop_encoder_tick_threshold
            ):
                if not self._encoder_stop_violation:
                    self._encoder_stop_violation = True
                    self._estop_generation += 1

    def _motion_allowed_locked(self, now: float) -> bool:
        if self._state["emergency_stop"] or self._encoder_stop_violation:
            return False
        if self._sensor_recovery is not None:
            return False
        if self._state["navigation_state"] == "REPLACING_GOAL":
            return False

        if self._stop_commanded_at is None:
            return True

        if self._post_resume_motion_ready:
            return (
                self._connected
                and self._age(now, self._pi_updated_at)
                <= self._watchdog.pi_timeout_sec
            )

        return bool(
            self._stop_confirmation_after_sequence is not None
            and self._stop_confirmed_locked(now)
        )

    def motion_safety_context(self) -> dict[str, Any] | None:
        """Issue current Pi safety identity for an authorized motion command."""
        with self._lock:
            now = time.time()
            if not self._motion_allowed_locked(now):
                return None
            # No Nav2 auto-drive command is authorized once its Goal has
            # FAILED, CANCELED or SUCCEEDED. Manual mode stays independent.
            if (
                self._robot_mode == "auto"
                and self._state["navigation_state"] not in self.ACTIVE_NAV_STATES
            ):
                return None
            if not self._connected:
                return None
            if self._age(now, self._pi_updated_at) > self._watchdog.pi_timeout_sec:
                return None
            if self._pi_estop_latched is not False:
                return None
            if not self._pi_safety_session:
                return None
            if type(self._pi_safety_epoch) is not int or self._pi_safety_epoch < 0:
                return None
            return {
                "safety_session": self._pi_safety_session,
                "safety_epoch": self._pi_safety_epoch,
                "estop_generation": self._estop_generation,
            }

    def note_authorized_motion(
        self, expected_safety: dict[str, Any] | None = None
    ) -> bool:
        with self._lock:
            now = time.time()
            if not self._motion_allowed_locked(now):
                return False
            if expected_safety is not None and (
                not self._connected
                or self._age(now, self._pi_updated_at) > self._watchdog.pi_timeout_sec
                or self._pi_estop_latched is not False
                or self._pi_safety_session != expected_safety.get("safety_session")
                or self._pi_safety_epoch != expected_safety.get("safety_epoch")
                or self._estop_generation != expected_safety.get("estop_generation")
            ):
                return False

            self._stop_commanded_at = None
            self._stop_encoder_ticks = None
            self._stop_confirmation_after_sequence = None
            self._post_resume_motion_ready = False
            return True

    def motion_allowed(self) -> bool:
        with self._lock:
            return self._motion_allowed_locked(time.time())

    def _arm_pending_stop_locked(self) -> None:
        self._post_resume_motion_ready = False
        self._stop_commanded_at = time.time()
        self._stop_encoder_ticks = self._encoder_ticks
        self._stop_confirmation_after_sequence = None
        self._stop_confirmation_ticks = None
        self._stop_confirmation_started_at = None
        self._stop_confirmation_last_at = None
        self._stop_confirmation_samples = 0

    def motion_requires_ack(self) -> bool:
        with self._lock:
            return self._stop_commanded_at is not None

    def _estop_retry_window_due_locked(self, now: float) -> bool:
        return bool(
            self._estop_attempts >= self.ESTOP_MAX_ATTEMPTS
            and not self._estop_command_acknowledged
            and self._pi_estop_latched is False
            and self._connected
            and self._age(now, self._pi_updated_at) <= self._watchdog.pi_timeout_sec
            and self._estop_last_attempt_at is not None
            and now - self._estop_last_attempt_at >= self.ESTOP_RETRY_WINDOW_SEC
        )

    def _sync_ros_navigation_terminal_locked(
        self,
        ros_nav: dict[str, Any],
        current: float,
        *,
        allow_during_blocked_failure: bool = False,
    ) -> str | None:
        if self._state["navigation_state"] != "NAVIGATING":
            return None
        # A fault-triggered Nav2 cancel must not become a user Goal cancel.
        if self._sensor_recovery is not None:
            return None
        if self._blocked_failure_in_progress and not allow_during_blocked_failure:
            return None
        terminal = str(ros_nav.get("state", "")).upper()
        if terminal not in {"SUCCEEDED", "FAILED", "CANCELED"}:
            return None
        if terminal == "SUCCEEDED":
            # Nav2 success proves the pose/yaw goal, not physical motor STOP.
            # Gate any late velocity commands until fresh encoder samples
            # establish a stable STOP for the configured confirmation window.
            if self._pending_nav_success is None:
                self._arm_pending_stop_locked()
                self._pending_nav_success = {"started_at": current, "processing": False}
                self._touch_locked(current)
            return None
        self._pending_nav_success = None
        # A Nav2 terminal state is not evidence that Pi motor output stopped.
        # Arm the output gate immediately, even if this state is first read by
        # the dashboard; the async watchdog must deliver physical STOP.
        self._arm_pending_stop_locked()
        self._pending_terminal_stop = {
            "reason": "nav2_failed" if terminal == "FAILED" else "nav2_canceled",
            "attempts": 0,
            "last_attempt_at": None,
            "processing": False,
        }
        self._state["navigation_state"] = terminal
        if terminal == "CANCELED":
            self._state["active_goal"] = None
            self._state["waypoints"] = []
            self._state["planned_path"] = []
        self._reset_navigation_progress_locked()
        self._touch_locked(current)
        return terminal

    def state_response(self, now: float | None = None) -> dict[str, Any]:
        current = now or time.time()
        try:
            ros_nav = self._ros.navigation_status()
        except Exception:
            ros_nav = {"state": "UNAVAILABLE"}
        with self._lock:
            self._sync_ros_navigation_terminal_locked(ros_nav, current)
            payload = {
                "ok": True,
                "robot_id": self._robot_id,
                "connected": self._connected,
                "robot_mode": self._robot_mode,
                "pi_navigation_mode": self._pi_navigation_mode,
                # Pi telemetry is distinct from the GPU's local E-STOP latch.
                # Do not report a stale or disconnected Pi as safely stopped.
                "pi_safety": {
                    "fresh": bool(
                        self._connected
                        and self._pi_updated_at is not None
                        and self._age(current, self._pi_updated_at) <= self._watchdog.pi_timeout_sec
                    ),
                    "emergency_stop": (
                        self._pi_estop_latched
                        if self._connected
                        and self._pi_updated_at is not None
                        and self._age(current, self._pi_updated_at) <= self._watchdog.pi_timeout_sec
                        else None
                    ),
                    "safety_session": (
                        self._pi_safety_session
                        if self._connected
                        and self._pi_updated_at is not None
                        and self._age(current, self._pi_updated_at) <= self._watchdog.pi_timeout_sec
                        else None
                    ),
                    "safety_epoch": (
                        self._pi_safety_epoch
                        if self._connected
                        and self._pi_updated_at is not None
                        and self._age(current, self._pi_updated_at) <= self._watchdog.pi_timeout_sec
                        else None
                    ),
                    "updated_at": self._pi_updated_at,
                },
                "led_enabled": self._led_enabled,
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
                await asyncio.to_thread(
                    self._process.transition,
                    "MAPPING",
                    None,
                    bool(payload.get("restart")),
                )
            except Exception as exc:
                with self._lock:
                    self._state["navigation_state"] = "ERROR"
                    self._state["last_error"] = str(exc)
                    self._touch_locked()
                raise

            if self._clear_visualization is not None:
                self._clear_visualization()

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
                        "waypoints": [],
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
            return self.state_response()

        source = str(payload.get("source", "existing")).strip().lower()
        map_name = payload.get("map_name")
        initial_pose_payload = payload.get("initial_pose")

        if source in {"current", "save_current"}:
            live_map = self._get_live_map()
            if not live_map:
                raise NavigationControlError(
                    "MAP_UNAVAILABLE",
                    "The current mapping result is unavailable.",
                    404,
                )

            if source == "current":
                live_mode = str(live_map.get("navigation_mode") or "").strip().lower()
                if live_mode not in {"", "mapping"}:
                    raise NavigationControlError(
                        "CURRENT_MAPPING_REQUIRED",
                        "Direct Driving transition requires the current Mapping result.",
                        409,
                    )

            if initial_pose_payload is None:
                initial_pose_payload = self._current_mapping_initial_pose()
            if initial_pose_payload is None:
                raise NavigationControlError(
                    "INITIAL_POSE_REQUIRED",
                    "A current Mapping pose is required before switching directly to Driving.",
                    409,
                )

            # Nav2 map_server needs a map file. Snapshot the current in-memory
            # Mapping result automatically so the operator does not have to
            # manually save and reload it before setting a Goal.
            saved = await asyncio.to_thread(self._save_map, dict(live_map), map_name)
            map_name = saved.get("map_name")
        elif source == "existing":
            if initial_pose_payload is None:
                raise NavigationControlError(
                    "INITIAL_POSE_REQUIRED",
                    "Initial pose is required when loading a saved map.",
                    400,
                )
        else:
            raise NavigationControlError(
                "INVALID_SOURCE",
                "source must be existing, current, or save_current.",
                400,
            )

        selected = self._map_api.resolve_map(map_name)
        try:
            await asyncio.to_thread(self._process.transition, "DRIVING", str(selected.yaml_path))
            loaded = await self._activate_map_when_ready(
                {"map_name": selected.map_name, "initial_pose": initial_pose_payload},
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
                    "waypoints": [],
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
        return self.state_response()

    def _current_mapping_initial_pose(self) -> dict[str, float] | None:
        pose = self._get_live_pose()
        if not isinstance(pose, dict):
            return None
        try:
            x = float(pose["x"])
            y = float(pose["y"])
            yaw = float(pose.get("yaw", 0.0))
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if not all(math.isfinite(value) for value in (x, y, yaw)):
            return None
        normalized_yaw = math.atan2(math.sin(yaw), math.cos(yaw))
        return {
            "x": x,
            "y": y,
            "yaw_degrees": math.degrees(normalized_yaw),
        }

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

    async def _stop_for_goal_replacement(self) -> None:
        """Cancel the prior Nav2 Goal and verify Pi STOP before replanning.

        Called only by plan_goal while the API operation lock serializes
        dashboard mutations. Changing a Goal never bypasses a pending
        emergency stop or reuses encoder samples recorded before STOP.
        """
        with self._lock:
            self._sensor_recovery = None
            self._pending_nav_success = None
            self._pending_terminal_stop = None
            self._arm_pending_stop_locked()
            self._state["navigation_state"] = "REPLACING_GOAL"
            self._touch_locked()

        cancel_result, stop_result = await asyncio.gather(
            asyncio.to_thread(self._ros.cancel_navigation),
            self._send_robot_command(
                self._robot_id,
                {"type": "stop", "reason": "navigation_goal_replace"},
            ),
            return_exceptions=True,
        )
        delivered = (
            bool(stop_result.get("ok")) and bool(stop_result.get("delivered", True))
            if isinstance(stop_result, dict)
            else bool(stop_result) if not isinstance(stop_result, BaseException)
            else False
        )
        cancel_ok = (
            isinstance(cancel_result, dict)
            and cancel_result.get("confirmed") is True
        )
        error_code = (
            "GOAL_REPLACEMENT_STOP_NOT_DELIVERED" if not delivered
            else "GOAL_REPLACEMENT_CANCEL_FAILED" if not cancel_ok
            else None
        )
        with self._lock:
            if self._state["emergency_stop"] or self._encoder_stop_violation:
                raise NavigationControlError(
                    "EMERGENCY_STOP_ACTIVE",
                    "Emergency stop interrupted Goal replacement.", 409,
                )
            if self._state["navigation_state"] != "REPLACING_GOAL":
                raise NavigationControlError(
                    "GOAL_REPLACEMENT_INTERRUPTED",
                    "Goal replacement was interrupted.", 409,
                )
            self._stop_commanded_at = time.time()
            self._stop_encoder_ticks = self._encoder_ticks
            self._stop_confirmation_after_sequence = (
                (self._encoder_sequence or 0) if delivered else None
            )
            self._stop_confirmation_ticks = None
            self._stop_confirmation_started_at = None
            self._stop_confirmation_last_at = None
            self._stop_confirmation_samples = 0
            if error_code is not None:
                self._state["navigation_state"] = "FAILED"
                self._state["last_error"] = error_code
                self._reset_navigation_progress_locked()
                self._touch_locked()
                raise NavigationControlError(
                    error_code, "Previous Goal could not be safely stopped.", 409,
                )

        deadline = time.monotonic() + self.STOP_CONFIRM_TIMEOUT_SEC
        while time.monotonic() < deadline:
            with self._lock:
                if self._state["emergency_stop"] or self._encoder_stop_violation:
                    raise NavigationControlError(
                        "EMERGENCY_STOP_ACTIVE",
                        "Emergency stop interrupted Goal replacement.", 409,
                    )
                if self._state["navigation_state"] != "REPLACING_GOAL":
                    raise NavigationControlError(
                        "GOAL_REPLACEMENT_INTERRUPTED",
                        "Goal replacement was interrupted.", 409,
                    )
                if self._stop_confirmed_locked(time.time()):
                    # Reuse the existing post-stop motion permit only after
                    # proven stationary encoders. A later Start still checks
                    # fresh Pi safety identity and watchdog health.
                    self._post_resume_motion_ready = True
                    return
            await asyncio.sleep(0.02)

        with self._lock:
            if self._state["navigation_state"] == "REPLACING_GOAL":
                self._state["navigation_state"] = "FAILED"
                self._state["last_error"] = "GOAL_REPLACEMENT_STOP_NOT_CONFIRMED"
                self._reset_navigation_progress_locked()
                self._touch_locked()
        raise NavigationControlError(
            "GOAL_REPLACEMENT_STOP_NOT_CONFIRMED",
            "Fresh encoder STOP confirmation was not received within 3 seconds.",
            409,
        )

    async def plan_goal(self, payload: dict[str, Any]) -> dict[str, Any]:
        goal = self._validated_goal(payload.get("goal", payload))
        waypoints = self._validated_waypoints(payload.get("waypoints"), goal)
        with self._lock:
            if self._state["navigation_mode"] != "DRIVING":
                raise NavigationControlError("DRIVING_MODE_REQUIRED", "Goal poses require DRIVING mode.")
            if not self._state["localization_ready"] or not self._state["nav2_ready"]:
                raise NavigationControlError("NAVIGATION_NOT_READY", "Localization and Nav2 must be ready.")
            if self._state["emergency_stop"]:
                raise NavigationControlError("EMERGENCY_STOP_ACTIVE", "Resume before planning a new goal.")
            replacing = self._state["navigation_state"] in self.ACTIVE_NAV_STATES

        # Reject an invalid destination/route before interrupting an active Goal.
        self._validate_goal_in_active_map(goal)
        for intermediate in waypoints:
            self._validate_goal_in_active_map(intermediate)
        if replacing:
            await self._stop_for_goal_replacement()

        try:
            if replacing:
                # Destination validation occurred before cancel; confirm the
                # live sensor and Pi health again after the physical STOP.
                self._assert_watchdogs_healthy()
            # Nav2's ComputePathToPose(use_start=False) starts at the latest
            # robot pose, not the old Goal or the old planned path origin.
            path = await asyncio.to_thread(self._compute_path_for_route, goal, waypoints)
            normalized_path = self._normalized_path(path)
            if len(normalized_path) < 2:
                raise NavigationControlError(
                    "PATH_NOT_FOUND", "Nav2 did not return a usable path.", 422,
                )
        except Exception as exc:
            if replacing:
                with self._lock:
                    if (
                        self._state["navigation_state"] == "REPLACING_GOAL"
                        and not self._state["emergency_stop"]
                    ):
                        self._state["navigation_state"] = "FAILED"
                        self._state["last_error"] = (
                            "GOAL_REPLACEMENT_PLAN_FAILED"
                        )
                        self._touch_locked()
            raise

        with self._lock:
            if self._state["emergency_stop"] or self._encoder_stop_violation:
                raise NavigationControlError(
                    "EMERGENCY_STOP_ACTIVE", "Emergency stop interrupted Goal planning.", 409,
                )
            if replacing and self._state["navigation_state"] != "REPLACING_GOAL":
                raise NavigationControlError(
                    "GOAL_REPLACEMENT_INTERRUPTED", "Goal replacement was interrupted.", 409,
                )
            self._state["active_goal"] = goal
            self._state["waypoints"] = [dict(point) for point in waypoints]
            self._state["planned_path"] = normalized_path
            self._state["navigation_state"] = "PATH_READY"
            self._state["last_error"] = None
            self._touch_locked()
        return self.state_response()

    async def start_navigation(self) -> dict[str, Any]:
        with self._lock:
            goal = dict(self._state["active_goal"] or {})
            waypoints = [dict(p) for p in self._state["waypoints"]]
            path_ready = len(self._state["planned_path"]) >= 2
            estop_generation = self._estop_generation
            self._assert_resume_safety_locked(require_estop=False)
            if not self._motion_allowed_locked(time.time()):
                raise NavigationControlError(
                    "STOP_NOT_CONFIRMED",
                    "Previous stop has not been confirmed.",
                    409,
                )
            admitted_stop_at = self._stop_commanded_at
            admitted_stop_episode = self._stop_confirmation_episode
            if not goal or not path_ready:
                raise NavigationControlError("PATH_REQUIRED", "Plan a goal before starting navigation.")
        self._assert_watchdogs_healthy()
        result = await asyncio.to_thread(self._navigate_route, goal, waypoints)
        try:
            progress_pose = self._get_live_pose()
        except Exception:
            progress_pose = None
        progress_started_at = time.time()
        with self._lock:
            interrupted = (
                self._estop_generation != estop_generation
                or self._state["emergency_stop"]
                or self._encoder_stop_violation
                or (
                    self._stop_commanded_at is not None
                    and (
                        self._stop_commanded_at != admitted_stop_at
                        or self._stop_confirmation_episode != admitted_stop_episode
                    )
                )
            )
            if not interrupted:
                self._state["navigation_state"] = "NAVIGATING"
                self._state["last_error"] = None
                # Only an accepted run supersedes the previous stop baseline.
                self._stop_commanded_at = None
                self._stop_encoder_ticks = None
                self._stop_confirmation_after_sequence = None
                self._post_resume_motion_ready = False
                self._start_navigation_progress_locked(progress_pose, progress_started_at)
                self._touch_locked()
        if interrupted:
            await self.emergency_stop("NAVIGATION_START_INTERRUPTED", automatic=True, force=True)
            raise NavigationControlError(
                "NAVIGATION_START_INTERRUPTED",
                "An emergency stop interrupted navigation startup.",
                409,
            )
        return {**self.state_response(), "navigation": result}

    async def cancel_goal(self) -> dict[str, Any]:
        with self._lock:
            self._pending_nav_success = None
            self._sensor_recovery = None
            self._arm_pending_stop_locked()
        # Nav2 action cancellation and physical STOP must proceed concurrently.
        # Pending-stop is armed before either call to gate late motor commands.
        cancel_result, stop_result = await asyncio.gather(
            asyncio.to_thread(self._ros.cancel_navigation),
            self._send_robot_command(
                self._robot_id,
                {"type": "stop", "reason": "navigation_goal_cancel"},
            ),
            return_exceptions=True,
        )
        cancel_error = cancel_result if isinstance(cancel_result, BaseException) else None
        cancel = None if cancel_error is not None else cancel_result
        delivered = bool(stop_result) if not isinstance(stop_result, BaseException) else False
        with self._lock:
            self._stop_commanded_at = time.time()
            self._stop_encoder_ticks = self._encoder_ticks
            self._stop_confirmation_after_sequence = (
                self._encoder_sequence if delivered else None
            )
            self._stop_confirmation_ticks = None
            self._stop_confirmation_started_at = None
            self._stop_confirmation_last_at = None
            self._stop_confirmation_samples = 0
            if cancel_error is None:
                terminal = self._cancel_terminal(cancel)
                self._reset_navigation_progress_locked()
                if self._state["emergency_stop"]:
                    self._state["navigation_state"] = "EMERGENCY_STOPPED"
                elif not delivered:
                    # A canceled Nav2 action does not prove that the Pi has
                    # stopped the wheels. Retain the Goal for operator Retry.
                    self._state["navigation_state"] = "FAILED"
                    self._state["last_error"] = "STOP_NOT_DELIVERED"
                elif terminal is not None:
                    self._apply_cancel_terminal_locked(terminal)
                else:
                    self._state["navigation_state"] = "READY"
                    self._state["active_goal"] = None
                    self._state["waypoints"] = []
                    self._state["planned_path"] = []
            self._touch_locked()
        if cancel_error is not None:
            raise cancel_error
        return {**self.state_response(), "cancel": cancel, "stop_delivered": delivered}

    async def pause_for_manual(self) -> dict[str, Any]:
        """Stop the active ROS action while retaining the goal for manual arrival."""
        with self._lock:
            self._sensor_recovery = None
            self._arm_pending_stop_locked()
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
            self._stop_confirmation_after_sequence = (
                self._encoder_sequence if delivered else None
            )
            self._stop_confirmation_ticks = None
            self._stop_confirmation_started_at = None
            self._stop_confirmation_last_at = None
            self._stop_confirmation_samples = 0
            if cancel_error is None:
                terminal = self._cancel_terminal(cancel)
                if self._state["emergency_stop"]:
                    self._state["navigation_state"] = "EMERGENCY_STOPPED"
                elif terminal is not None:
                    self._apply_cancel_terminal_locked(terminal)
                elif self._state["navigation_mode"] == "MAPPING":
                    self._state["navigation_state"] = "IDLE"
                else:
                    self._state["navigation_state"] = "READY"
                if terminal != "FAILED":
                    self._state["last_error"] = None
                self._reset_navigation_progress_locked()
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

    async def emergency_stop(self, reason: str, automatic: bool = False, force: bool = False) -> dict[str, Any]:
        normalized_reason = (str(reason).strip() or "dashboard_emergency_stop")[:96]
        with self._lock:
            self._post_resume_motion_ready = False
            if self._encoder_stop_violation:
                normalized_reason = "ENCODER_MOVEMENT_AFTER_STOP"
            now = time.time()
            if self._estop_attempt_reason != normalized_reason:
                self._estop_attempt_reason = normalized_reason
                self._estop_attempts = 0
                self._estop_last_attempt_at = None
                self._estop_command_acknowledged = False
            if automatic and self._estop_retry_window_due_locked(now):
                self._estop_attempts = 0
            if automatic and not force and self._state["emergency_stop"] and self._state["emergency_reason"] == normalized_reason and (
                self._estop_command_acknowledged
                or self._estop_attempts >= self.ESTOP_MAX_ATTEMPTS
                or (self._estop_last_attempt_at is not None and now - self._estop_last_attempt_at < self.ESTOP_RETRY_INTERVAL_SEC)
            ):
                return {**self.state_response(), "cancel": None, "stop_delivered": None, "automatic": True}
            was_stopped = self._state["emergency_stop"]
            self._estop_attempts += 1
            self._estop_last_attempt_at = now
            self._estop_command_acknowledged = False
            self._state["emergency_stop"] = True
            self._state["emergency_reason"] = normalized_reason
            self._state["navigation_state"] = "EMERGENCY_STOPPED"
            self._sensor_recovery = None
            self._pending_terminal_stop = None
            if not was_stopped and not self._encoder_stop_violation:
                self._stop_commanded_at = time.time()
                self._stop_encoder_ticks = self._encoder_ticks
            self._estop_generation += 1
            self._estop_delivery_token += 1
            delivery_token = self._estop_delivery_token
            self._touch_locked()
        cancel, delivered = await asyncio.gather(
            asyncio.to_thread(self._ros.cancel_navigation),
            self._send_robot_command(
                self._robot_id,
                {"type": "emergency_stop", "reason": normalized_reason, "automatic": automatic},
            ),
        )
        with self._lock:
            if self._estop_delivery_token == delivery_token:
                self._estop_command_acknowledged = bool(delivered)
        return {**self.state_response(), "cancel": cancel, "stop_delivered": delivered, "automatic": automatic}

    async def resume_navigation(self) -> dict[str, Any]:
        with self._lock:
            goal = dict(self._state["active_goal"] or {})
            waypoints = [dict(p) for p in self._state["waypoints"]]
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
            path = self._normalized_path(
                await asyncio.to_thread(self._compute_path_for_route, goal, waypoints)
            )
            if len(path) < 2:
                raise NavigationControlError(
                    "PATH_NOT_FOUND",
                    "A safe resume path could not be calculated.",
                    422,
                )
        prepared = await self._send_robot_command(self._robot_id, {"type": "resume_safety_check"})
        if not isinstance(prepared, dict) or prepared.get("ok") is not True:
            raise NavigationControlError("STOP_NOT_CONFIRMED", "The Pi did not confirm a latched safety stop.", 409)
        try:
            marker = int(prepared["encoder_sequence"])
            safety_session = str(prepared["safety_session"]).strip()
            safety_epoch = int(prepared["safety_epoch"])
        except (KeyError, TypeError, ValueError):
            raise NavigationControlError("STOP_NOT_CONFIRMED", "The Pi did not provide encoder and safety generations.", 409) from None
        if not safety_session or safety_epoch < 0:
            raise NavigationControlError("STOP_NOT_CONFIRMED", "The Pi safety generation is invalid.", 409)
        with self._lock:
            if (
                self._estop_generation != estop_generation
                or not self._state["emergency_stop"]
                or self._pi_safety_session != safety_session
                or self._pi_safety_epoch != safety_epoch
            ):
                interrupted = True
            else:
                interrupted = False
                self._stop_confirmation_after_sequence = marker
                self._stop_confirmation_ticks = None
                self._stop_confirmation_started_at = None
                self._stop_confirmation_last_at = None
                self._stop_confirmation_samples = 0
        if interrupted:
            await self.emergency_stop("RESUME_INTERRUPTED", automatic=True, force=True)
            raise NavigationControlError("RESUME_INTERRUPTED", "A newer emergency stop interrupted resume.", 409)
        try:
            await self._wait_for_stop_confirmation(estop_generation)
            with self._lock:
                interrupted = self._estop_generation != estop_generation or not self._stop_confirmed_locked(time.time())
                confirmation_episode = self._stop_confirmation_episode
            if interrupted:
                raise NavigationControlError("RESUME_INTERRUPTED", "A newer stop or encoder movement interrupted resume.", 409)
            delivered = await self._send_robot_command(self._robot_id, {
                "type": "resume_navigation",
                "safety_session": safety_session,
                "safety_epoch": safety_epoch,
            })
            if not delivered:
                await self.emergency_stop("RESUME_RELEASE_FAILED", automatic=True, force=True)
                raise NavigationControlError("PI_RESUME_FAILED", "The Pi did not accept the resume command.", 409)
        except Exception:
            with self._lock:
                self._stop_confirmation_after_sequence = None
            raise
        with self._lock:
            if (
                self._estop_generation != estop_generation
                or not self._state["emergency_stop"]
                or self._pi_estop_latched is not False
                or self._pi_safety_session != safety_session
                or self._pi_safety_epoch != safety_epoch
                or self._stop_confirmation_episode != confirmation_episode
                or not self._stop_confirmed_locked(time.time())
            ):
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
                self._reset_navigation_progress_locked()
                self._encoder_stop_violation = False
                self._stop_encoder_ticks = self._encoder_ticks
                self._stop_commanded_at = time.time() - self._watchdog.stop_encoder_grace_sec
                self._touch_locked()
            self._stop_confirmation_after_sequence = None
            self._post_resume_motion_ready = (
                not retry_stop and not restart_navigation
            )
        if retry_stop:
            await self.emergency_stop("RESUME_INTERRUPTED", automatic=True, force=True)
            raise NavigationControlError("RESUME_INTERRUPTED", "A newer emergency stop interrupted resume.", 409)
        if not restart_navigation:
            return {**self.state_response(), "replanned": False}
        try:
            navigation = await asyncio.to_thread(self._navigate_route, goal, waypoints)
        except Exception:
            await self.emergency_stop("RESUME_NAVIGATION_FAILED", automatic=True, force=True)
            raise
        try:
            progress_pose = self._get_live_pose()
        except Exception:
            progress_pose = None
        progress_started_at = time.time()
        with self._lock:
            interrupted = (
                self._estop_generation != estop_generation
                or self._state["emergency_stop"]
                or self._encoder_stop_violation
            )
            if not interrupted:
                self._state["navigation_state"] = "NAVIGATING"
                self._stop_commanded_at = None
                self._stop_encoder_ticks = None
                self._start_navigation_progress_locked(progress_pose, progress_started_at)
                self._touch_locked()
        if interrupted:
            await self.emergency_stop("RESUME_INTERRUPTED", automatic=True, force=True)
            raise NavigationControlError(
                "RESUME_INTERRUPTED",
                "A newer emergency stop interrupted navigation restart.",
                409,
            )
        return {**self.state_response(), "navigation": navigation, "replanned": True}

    def _stop_confirmed_locked(self, now: float) -> bool:
        started = self._stop_confirmation_started_at
        last = self._stop_confirmation_last_at
        return bool(
            self._connected
            and started is not None
            and last is not None
            and self._stop_confirmation_samples >= 2
            and last - started >= self.STOP_CONFIRM_STABLE_SEC
            and 0 <= now - last <= self.STOP_CONFIRM_MAX_AGE_SEC
            and self._age(now, self._pi_updated_at) <= self._watchdog.pi_timeout_sec
        )

    async def _wait_for_stop_confirmation(self, estop_generation: int) -> None:
        deadline = time.monotonic() + self.STOP_CONFIRM_TIMEOUT_SEC
        while time.monotonic() < deadline:
            with self._lock:
                if self._estop_generation != estop_generation or not self._state["emergency_stop"]:
                    raise NavigationControlError("RESUME_INTERRUPTED", "A newer emergency stop interrupted resume.", 409)
                if self._stop_confirmed_locked(time.time()):
                    return
            await asyncio.sleep(0.02)
        raise NavigationControlError("STOP_NOT_CONFIRMED", "Fresh, stable encoder samples were not received after the stop.", 409)

    async def beep(self, payload: dict[str, Any]) -> dict[str, Any]:
        duration_ms = self._bounded_int(
            payload.get("duration_ms", 350),
            50,
            2000,
            "duration_ms",
        )
        delivered = await self._send_robot_command(
            self._robot_id,
            {"type": "beep", "duration_ms": duration_ms},
        )
        if not delivered:
            raise NavigationControlError(
                "PI_OFFLINE",
                "The beep command was not delivered.",
                409,
            )
        return {"ok": True, "delivered": True, "duration_ms": duration_ms}

    async def set_led(self, payload: dict[str, Any]) -> dict[str, Any]:
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise NavigationControlError("INVALID_REQUEST", "enabled must be boolean.", 400)

        delivered = await self._send_robot_command(
            self._robot_id,
            {"type": "led", "enabled": enabled, "duration_ms": 0},
        )
        if not delivered:
            raise NavigationControlError("PI_OFFLINE", "The LED command was not delivered.", 409)

        with self._lock:
            self._led_enabled = enabled
            self._touch_locked()

        return self.state_response()

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
        duration_ms = self._bounded_int(payload.get("led_duration_ms", 10000), 0, 10000, "led_duration_ms")
        delivered = await self._send_robot_command(
            self._robot_id,
            {"type": "warning", "text": text, "led_duration_ms": duration_ms},
        )
        if not delivered:
            raise NavigationControlError("PI_OFFLINE", "The warning command was not delivered.", 409)
        return {"ok": True, "delivered": True, "led_duration_ms": duration_ms}

    async def evaluate_watchdog(self, now: float | None = None) -> str | None:
        current = now or time.time()
        try:
            ros_nav = self._ros.navigation_status()
        except Exception:
            ros_nav = {"state": "UNAVAILABLE"}
        try:
            rotation = self._ros.rotation_status()
        except Exception:
            rotation = {}

        # The dashboard may have observed a Nav2 FAILED/CANCELED status
        # first. Deliver the queued Pi STOP even though navigation is already
        # terminal and the regular active-navigation watchdog would return.
        with self._lock:
            self._sync_ros_navigation_terminal_locked(ros_nav, current)
            pending_terminal_stop = self._pending_terminal_stop is not None
            hard_fault = bool(self._encoder_stop_violation or self._pi_session_stop_pending)
            recovering = self._sensor_recovery is not None
        if pending_terminal_stop and not hard_fault:
            await self._deliver_terminal_stop()
        if recovering and not hard_fault:
            await self._advance_sensor_recovery(current)
            return None

        blocked = False
        with self._lock:
            retry_pending_stop = (
                self._state["emergency_stop"]
                and self._state["emergency_reason"] in {
                    "ENCODER_MOVEMENT_AFTER_STOP", "PI_SAFETY_SESSION_CHANGED"
                }
                and not self._estop_command_acknowledged
                and (
                    self._estop_retry_window_due_locked(current)
                    or (
                        self._estop_attempts < self.ESTOP_MAX_ATTEMPTS
                        and (self._estop_last_attempt_at is None or current - self._estop_last_attempt_at >= self.ESTOP_RETRY_INTERVAL_SEC)
                    )
                )
            )
            if self._pi_session_stop_pending:
                issue = self._pi_session_stop_pending
                self._pi_session_stop_pending = None
            elif self._encoder_stop_violation and (retry_pending_stop or not (
                self._state["emergency_stop"]
                and self._state["emergency_reason"] == "ENCODER_MOVEMENT_AFTER_STOP"
            )):
                issue = "ENCODER_MOVEMENT_AFTER_STOP"
            elif self._state["emergency_reason"] == "PI_SAFETY_SESSION_CHANGED" and retry_pending_stop:
                issue = "PI_SAFETY_SESSION_CHANGED"
            else:
                terminal = self._sync_ros_navigation_terminal_locked(ros_nav, current)
                if terminal is not None:
                    return None
                if self._state["navigation_state"] not in self.ACTIVE_NAV_STATES:
                    return None
                issue = self._watchdog_issue_locked(current)
                # A terminal Nav2 success may still have moving wheels.
                # Treat the ensuing bounded STOP confirmation as navigation,
                # not as another no-progress / blockage recovery attempt.
                waiting_for_stop = self._pending_nav_success is not None
                if (
                    issue is None and not waiting_for_stop
                    and self._state["navigation_state"] in self.NO_PROGRESS_NAV_STATES
                ):
                    self._note_rotation_progress_locked(rotation, current)
                blocked = (
                    issue is None
                    and not waiting_for_stop
                    and self._state["navigation_state"] in self.NO_PROGRESS_NAV_STATES
                    and self._navigation_progress_timed_out_locked(current)
                )
                if blocked:
                    self._blocked_failure_in_progress = True
        if issue:
            if issue in {"ENCODER_MOVEMENT_AFTER_STOP", "PI_SAFETY_SESSION_CHANGED"}:
                await self.emergency_stop(issue, automatic=True)
            else:
                await self._begin_sensor_recovery(issue, current)
            return issue
        if not blocked:
            await self._resolve_pending_nav_success()
        if blocked:
            # User navigation mutations (Cancel / Manual / Resume) use the same
            # operation lock. If one wins the race, re-checking inside
            # _fail_blocked_navigation() turns this watchdog attempt into a no-op.
            async with self._operation_lock:
                failed = await self._fail_blocked_navigation()
            return "BLOCKED_TIMEOUT" if failed else None
        return None

    async def _begin_sensor_recovery(self, issue: str, current: float) -> None:
        """STOP immediately and allow a bounded 5s sensor recovery episode.

        Ordinary data/communication loss is not a hardware E-STOP. Cancelling
        the old ROS action and arming the Pi STOP gate prevents stale driving.
        """
        with self._lock:
            if (
                self._state["navigation_state"] not in self.ACTIVE_NAV_STATES
                or self._state["emergency_stop"]
                or self._sensor_recovery is not None
            ):
                return
            self._arm_pending_stop_locked()
            recovery = {
                "reason": issue, "started_at": current, "stable_since": None,
                "stop_delivered": False, "stop_attempt_at": None,
                "replanning": False,
                "goal": dict(self._state["active_goal"] or {}),
                "waypoints": [dict(p) for p in self._state["waypoints"]],
            }
            self._sensor_recovery = recovery
            self._reset_navigation_progress_locked()
            self._touch_locked(current)
        cancel, stop = await asyncio.gather(
            asyncio.to_thread(self._ros.cancel_navigation),
            self._send_robot_command(
                self._robot_id,
                {"type": "stop", "reason": f"navigation_sensor_{issue.lower()}"},
            ),
            return_exceptions=True,
        )
        with self._lock:
            if self._sensor_recovery is not recovery:
                return
            if isinstance(cancel, BaseException):
                recovery["cancel_error"] = str(cancel)
                self._state["navigation_state"] = "FAILED"
                self._state["last_error"] = "SENSOR_RECOVERY_CANCEL_FAILED"
                self._sensor_recovery = None
                self._touch_locked()
                return
            recovery["stop_attempt_at"] = time.monotonic()
            delivered = (
                bool(stop.get("ok")) and bool(stop.get("delivered", True))
                if isinstance(stop, dict) else
                bool(stop) if not isinstance(stop, BaseException) else False
            )
            if delivered:
                self._record_sensor_stop_ack_locked(recovery)
            self._touch_locked()

    def _record_sensor_stop_ack_locked(self, recovery: dict[str, Any]) -> None:
        recovery["stop_delivered"] = True
        self._stop_commanded_at = time.time()
        self._stop_encoder_ticks = self._encoder_ticks
        self._stop_confirmation_after_sequence = self._encoder_sequence
        self._stop_confirmation_ticks = None
        self._stop_confirmation_started_at = None
        self._stop_confirmation_last_at = None
        self._stop_confirmation_samples = 0

    def _safe_live_pose(self) -> dict[str, Any] | None:
        try:
            return self._get_live_pose()
        except Exception:
            return None

    async def _advance_sensor_recovery(self, current: float) -> None:
        with self._lock:
            recovery = self._sensor_recovery
            if recovery is None or self._state["emergency_stop"]:
                return
            if current - recovery["started_at"] >= 5.0:
                self._sensor_recovery = None
                self._state["navigation_state"] = "FAILED"
                self._state["last_error"] = f"SENSOR_RECOVERY_TIMEOUT:{recovery['reason']}"
                self._arm_pending_stop_locked()
                self._touch_locked(current)
                return
            if recovery["replanning"]:
                return
            issue = self._watchdog_issue_locked(current)
            healthy = issue is None and self._pi_estop_latched is False
            if healthy:
                if recovery["stable_since"] is None:
                    recovery["stable_since"] = current
            else:
                recovery["stable_since"] = None
            ready = bool(
                healthy and recovery["stop_delivered"]
                and recovery["stable_since"] is not None
                and current - recovery["stable_since"] >= 1.0
                and self._stop_confirmed_locked(time.time())
                and recovery["goal"]
            )
            retry_stop = bool(
                not recovery["stop_delivered"]
                and (
                    recovery["stop_attempt_at"] is None
                    or time.monotonic() - recovery["stop_attempt_at"] >= 1.0
                )
            )
            if retry_stop:
                recovery["stop_attempt_at"] = time.monotonic()
            if ready:
                recovery["replanning"] = True
        if retry_stop:
            try:
                stop = await self._send_robot_command(
                    self._robot_id,
                    {"type": "stop", "reason": "navigation_sensor_recovery_retry"},
                )
                delivered = (
                    bool(stop.get("ok")) and bool(stop.get("delivered", True))
                    if isinstance(stop, dict) else bool(stop)
                )
            except Exception:
                delivered = False
            with self._lock:
                if self._sensor_recovery is recovery and delivered:
                    self._record_sensor_stop_ack_locked(recovery)
        if not ready:
            return

        # Do not hold the thread lock during ROS actions. The identity of
        # the episode is checked again after each asynchronous boundary.
        async with self._operation_lock:
            with self._lock:
                if (
                    self._sensor_recovery is not recovery
                    or self._state["emergency_stop"]
                    or self._state["navigation_state"] != "NAVIGATING"
                ):
                    return
                self._state["navigation_state"] = "RESUMING"
                self._touch_locked()
            try:
                path = self._normalized_path(
                    await asyncio.to_thread(
                        self._compute_path_for_route, recovery["goal"], recovery["waypoints"]
                    )
                )
                if len(path) < 2:
                    raise NavigationControlError(
                        "PATH_NOT_FOUND", "Recovery planner found no safe path.",
                    )
                with self._lock:
                    if (
                        self._sensor_recovery is not recovery
                        or self._state["emergency_stop"]
                        or self._state["navigation_state"] != "RESUMING"
                    ):
                        return
                    # Sensor data may be lost again while the planner runs.
                    # Re-arm the 1s stable window instead of leaving a
                    # permanently stuck RESUMING/replanning=True episode.
                    if self._watchdog_issue_locked(time.time()) is not None:
                        recovery["replanning"] = False
                        recovery["stable_since"] = None
                        self._state["navigation_state"] = "NAVIGATING"
                        self._touch_locked()
                        return
                    if time.time() - recovery["started_at"] >= 5.0:
                        raise NavigationControlError(
                            "SENSOR_RECOVERY_TIMEOUT",
                            "Sensor recovery deadline expired during planning.",
                        )
                navigation = await asyncio.to_thread(
                    self._navigate_route, recovery["goal"], recovery["waypoints"]
                )
                if not isinstance(navigation, dict) or navigation.get("accepted") is not True:
                    raise NavigationControlError(
                        "NAVIGATION_REJECTED", "Nav2 rejected recovered Goal.",
                    )
            except Exception:
                with self._lock:
                    if self._sensor_recovery is recovery:
                        self._sensor_recovery = None
                        self._state["navigation_state"] = "FAILED"
                        self._state["last_error"] = "SENSOR_RECOVERY_REPLAN_FAILED"
                        self._arm_pending_stop_locked()
                        self._touch_locked()
                return
            with self._lock:
                interrupted = (
                    self._sensor_recovery is not recovery
                    or self._state["emergency_stop"]
                    or self._state["navigation_state"] != "RESUMING"
                    or time.time() - recovery["started_at"] >= 5.0
                    or self._watchdog_issue_locked(time.time()) is not None
                )
                if not interrupted:
                    self._sensor_recovery = None
                    self._state["planned_path"] = path
                    self._state["navigation_state"] = "NAVIGATING"
                    self._state["last_error"] = None
                    self._stop_commanded_at = None
                    self._stop_encoder_ticks = None
                    self._stop_confirmation_after_sequence = None
                    self._start_navigation_progress_locked(
                        self._safe_live_pose(), time.time(),
                    )
                    self._touch_locked()
            if interrupted:
                # Cancellation, timeout or new sensor failure may have won
                # after NavigateToPose accepted its action.
                await asyncio.to_thread(self._ros.cancel_navigation)
                with self._lock:
                    if (
                        self._sensor_recovery is recovery
                        and not self._state["emergency_stop"]
                        and self._state["navigation_state"] == "RESUMING"
                    ):
                        recovery["replanning"] = False
                        recovery["stable_since"] = None
                        recovery["stop_delivered"] = False
                        recovery["stop_attempt_at"] = None
                        self._state["navigation_state"] = "NAVIGATING"
                        # A canceled late action must be followed by a fresh
                        # Pi STOP before retrying any subsequent Goal.
                        self._arm_pending_stop_locked()
                        self._touch_locked()

    async def _deliver_terminal_stop(self) -> bool:
        """Best-effort physical STOP after Nav2 fails or cancels itself.

        At most three UART delivery attempts, spaced by 1s. Hardware safety
        faults still take priority. Missing STOP acknowledgement permanently
        holds the pending STOP gate and reports FAILED, not a synthetic E-STOP.
        """
        with self._lock:
            pending = self._pending_terminal_stop
            if (
                pending is None
                or pending["processing"]
                or self._state["emergency_stop"]
            ):
                return False
            now = time.monotonic()
            if (
                pending["last_attempt_at"] is not None
                and now - pending["last_attempt_at"] < 1.0
            ):
                return False
            if pending["attempts"] >= 3:
                return False
            pending["processing"] = True
            pending["attempts"] += 1
            pending["last_attempt_at"] = now
        delivered = False
        try:
            try:
                response = await self._send_robot_command(
                    self._robot_id,
                    {"type": "stop", "reason": pending["reason"]},
                )
                delivered = (
                    response.get("ok", False) and response.get("delivered", True)
                    if isinstance(response, dict)
                    else bool(response)
                )
            except Exception:
                delivered = False
            with self._lock:
                if self._pending_terminal_stop is not pending:
                    return False
                if delivered:
                    self._stop_commanded_at = time.time()
                    self._stop_encoder_ticks = self._encoder_ticks
                    self._stop_confirmation_after_sequence = self._encoder_sequence
                    self._stop_confirmation_ticks = None
                    self._stop_confirmation_started_at = None
                    self._stop_confirmation_last_at = None
                    self._stop_confirmation_samples = 0
                    self._pending_terminal_stop = None
                elif pending["attempts"] >= 3:
                    self._state["navigation_state"] = "FAILED"
                    self._state["last_error"] = "STOP_NOT_DELIVERED"
                    self._touch_locked()
                return delivered
        finally:
            with self._lock:
                if self._pending_terminal_stop is pending:
                    pending["processing"] = False

    async def _resolve_pending_nav_success(self) -> None:
        """Finalize Nav2 success only after STOP delivery and stable encoders."""
        with self._lock:
            pending = self._pending_nav_success
            if (
                pending is None
                or pending["processing"]
                or self._state["navigation_state"] != "NAVIGATING"
                or self._state["emergency_stop"]
                or self._encoder_stop_violation
            ):
                return
            pending["processing"] = True

        delivered = False
        try:
            try:
                delivered = bool(await self._send_robot_command(
                    self._robot_id,
                    {"type": "stop", "reason": "navigation_goal_succeeded"},
                ))
            except Exception:
                delivered = False

            with self._lock:
                if self._pending_nav_success is not pending:
                    return
                self._stop_commanded_at = time.time()
                self._stop_encoder_ticks = self._encoder_ticks
                # A success requires ACK delivery followed by *new*
                # encoder samples. Never accept pre-STOP telemetry.
                self._stop_confirmation_after_sequence = (
                    (self._encoder_sequence or 0) if delivered else None
                )
                self._stop_confirmation_ticks = None
                self._stop_confirmation_started_at = None
                self._stop_confirmation_last_at = None
                self._stop_confirmation_samples = 0

            deadline = time.monotonic() + self.STOP_CONFIRM_TIMEOUT_SEC
            while delivered and time.monotonic() < deadline:
                with self._lock:
                    if self._pending_nav_success is not pending:
                        return
                    if self._state["emergency_stop"] or self._encoder_stop_violation:
                        return
                    if self._stop_confirmed_locked(time.time()):
                        self._state["navigation_state"] = "SUCCEEDED"
                        self._state["active_goal"] = None
                        self._state["waypoints"] = []
                        self._state["planned_path"] = []
                        self._state["last_error"] = None
                        self._reset_navigation_progress_locked()
                        self._touch_locked()
                        return
                await asyncio.sleep(0.02)

            with self._lock:
                if self._pending_nav_success is pending and not self._state["emergency_stop"]:
                    self._state["navigation_state"] = "FAILED"
                    self._state["last_error"] = (
                        "STOP_NOT_CONFIRMED" if delivered else "STOP_NOT_DELIVERED"
                    )
                    # Preserve the active Goal for an explicit operator Retry.
                    self._reset_navigation_progress_locked()
                    self._touch_locked()
        finally:
            with self._lock:
                if self._pending_nav_success is pending:
                    self._pending_nav_success = None

    async def _fail_blocked_navigation(self) -> bool:
        try:
            try:
                ros_nav = self._ros.navigation_status()
            except Exception:
                ros_nav = {"state": "UNAVAILABLE"}
            with self._lock:
                terminal = self._sync_ros_navigation_terminal_locked(
                    ros_nav,
                    time.time(),
                    allow_during_blocked_failure=True,
                )
                if terminal is not None:
                    return False
                if self._state["navigation_state"] not in self.NO_PROGRESS_NAV_STATES:
                    return False

            cancel_result: dict[str, Any] | None = None
            cancel_error = None
            try:
                raw_cancel_result = await asyncio.to_thread(self._ros.cancel_navigation)
                if isinstance(raw_cancel_result, dict):
                    cancel_result = raw_cancel_result
            except Exception as exc:
                cancel_error = exc

            terminal = str((cancel_result or {}).get("terminal") or "").upper()
            if terminal in {"SUCCEEDED", "FAILED", "CANCELED"}:
                with self._lock:
                    self._sync_ros_navigation_terminal_locked(
                        {"state": terminal},
                        time.time(),
                        allow_during_blocked_failure=True,
                    )
                return False

            delivered = await self._send_robot_command(
                self._robot_id,
                {"type": "stop", "reason": "navigation_blocked_timeout"},
            )
            failed = False
            with self._lock:
                self._stop_commanded_at = time.time()
                self._stop_encoder_ticks = self._encoder_ticks
                if (
                    cancel_error is None
                    and self._state["navigation_state"] in self.NO_PROGRESS_NAV_STATES
                    and not self._state["emergency_stop"]
                ):
                    self._state["navigation_state"] = "FAILED"
                    self._state["last_error"] = "BLOCKED_TIMEOUT"
                    self._reset_navigation_progress_locked()
                    self._touch_locked()
                    failed = True

            if cancel_error is not None:
                # A concurrent E-stop or another completed state transition can
                # make Nav2 reject a duplicate cancel. Do not reinterpret that
                # already-safe transition as a new automatic E-stop.
                with self._lock:
                    superseded = (
                        self._state["navigation_state"] not in self.NO_PROGRESS_NAV_STATES
                        or self._state["emergency_stop"]
                    )
                if superseded:
                    return False
                # A Nav2 cancellation exception does not establish a
                # physically blocked route. The Pi STOP was already sent;
                # keep its command gate armed and report FAILED, not E-STOP.
                with self._lock:
                    self._arm_pending_stop_locked()
                    self._state["navigation_state"] = "FAILED"
                    self._state["last_error"] = "NAV_CANCEL_FAILED"
                    self._reset_navigation_progress_locked()
                    self._touch_locked()
                return True
            if not delivered:
                # Nav2 is canceled, so the route is no longer producing commands.
                # Keep the blocked outcome as FAILED rather than converting it to E-stop.
                return failed
            return failed
        finally:
            with self._lock:
                self._blocked_failure_in_progress = False

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

    @staticmethod
    def _cancel_terminal(cancel: Any) -> str | None:
        if not isinstance(cancel, dict):
            return None
        terminal = str(cancel.get("terminal") or "").upper()
        return terminal if terminal in {"SUCCEEDED", "FAILED", "CANCELED"} else None

    def _apply_cancel_terminal_locked(self, terminal: str) -> None:
        if terminal == "SUCCEEDED":
            # A cancel/success race cannot bypass physical-stop verification.
            if self._pending_nav_success is None:
                self._pending_nav_success = {
                    "started_at": time.time(), "processing": False,
                }
            return
        self._state["navigation_state"] = terminal
        if terminal == "CANCELED":
            self._state["active_goal"] = None
            self._state["waypoints"] = []
            self._state["planned_path"] = []

    def _reset_navigation_progress_locked(self) -> None:
        self._pending_nav_success = None
        self._navigation_progress_pose = None
        self._navigation_progress_at = None
        self._rotation_extension_sec = 0.0
        self._rotation_last_odom_sequence = None
        self._rotation_last_verified_yaw = None
        self._rotation_limit_reached = False

    def _start_navigation_progress_locked(
        self,
        payload: dict[str, Any] | None,
        received_at: float,
    ) -> None:
        self._reset_navigation_progress_locked()
        self._navigation_progress_at = received_at
        if not isinstance(payload, dict):
            return
        try:
            x = float(payload["x"])
            y = float(payload["y"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return
        if math.isfinite(x) and math.isfinite(y):
            self._navigation_progress_pose = (x, y)

    def _note_navigation_progress_locked(
        self,
        payload: dict[str, Any] | None,
        received_at: float,
    ) -> None:
        if self._state["navigation_state"] not in self.ACTIVE_NAV_STATES:
            self._reset_navigation_progress_locked()
            return
        if not isinstance(payload, dict):
            return
        try:
            x = float(payload["x"])
            y = float(payload["y"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return
        if not math.isfinite(x) or not math.isfinite(y):
            return

        previous = self._navigation_progress_pose
        if previous is None:
            self._navigation_progress_pose = (x, y)
            if self._navigation_progress_at is None:
                self._navigation_progress_at = received_at
            return
        if self._navigation_progress_at is None:
            self._navigation_progress_at = received_at
            return

        moved = math.hypot(x - previous[0], y - previous[1]) >= self.PROGRESS_DISTANCE_M
        if moved:
            self._navigation_progress_pose = (x, y)
            self._navigation_progress_at = received_at
            self._rotation_extension_sec = 0.0
            self._rotation_last_verified_yaw = None
            self._rotation_limit_reached = False

    def _note_rotation_progress_locked(self, rotation: dict[str, Any], now: float) -> None:
        if self._navigation_progress_at is None or self._rotation_limit_reached:
            return
        sequence = rotation.get("odom_sequence")
        if not isinstance(sequence, int) or sequence == self._rotation_last_odom_sequence:
            return
        self._rotation_last_odom_sequence = sequence
        try:
            command_age = float(rotation["command_age_sec"])
            odom_age = float(rotation["odometry_age_sec"])
            linear = float(rotation["command_linear_x"])
            angular = float(rotation["command_angular_z"])
            yaw = float(rotation["odom_yaw"])
            yaw_delta = float(rotation["odom_yaw_delta"])
            command_since = float(rotation["command_since_at"])
            odom_interval_start = float(rotation["odom_interval_start_at"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return
        if not all(math.isfinite(value) for value in (
            command_age, odom_age, linear, angular, yaw, yaw_delta, command_since, odom_interval_start,
        )):
            return
        if self._rotation_last_verified_yaw is not None:
            since_verified = math.atan2(
                math.sin(yaw - self._rotation_last_verified_yaw),
                math.cos(yaw - self._rotation_last_verified_yaw),
            )
            if abs(since_verified) < self.ROTATION_MIN_YAW_DELTA_RAD or angular * since_verified <= 0:
                return
        if (
            not 0 <= command_age <= self.ROTATION_DATA_MAX_AGE_SEC
            or not 0 <= odom_age <= self.ROTATION_DATA_MAX_AGE_SEC
            or abs(linear) > self._ros.ROTATION_MAX_LINEAR_MPS
            or abs(angular) < self._ros.ROTATION_MIN_ANGULAR_RPS
            or abs(yaw_delta) < self.ROTATION_MIN_YAW_DELTA_RAD
            or angular * yaw_delta <= 0
            or odom_interval_start < command_since
        ):
            return
        extension = max(0.0, now - self._navigation_progress_at)
        if self._rotation_extension_sec + extension >= self.ROTATION_MAX_CUMULATIVE_SEC:
            self._rotation_limit_reached = True
            return
        self._rotation_extension_sec += extension
        self._rotation_last_verified_yaw = yaw
        self._navigation_progress_at = now

    def _navigation_progress_timed_out_locked(self, now: float) -> bool:
        return (
            self._rotation_limit_reached
            or (
                self._navigation_progress_at is not None
                and now - self._navigation_progress_at >= self.BLOCKED_TIMEOUT_SEC
            )
        )

    def _assert_resume_safety_locked(self, require_estop: bool) -> None:
        if self._encoder_stop_violation:
            raise NavigationControlError(
                "ENCODER_MOVEMENT_AFTER_STOP",
                "Unresolved encoder movement after stop blocks navigation.",
            )
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

    def _validated_waypoints(
        self, raw: Any, final_goal: dict[str, float]
    ) -> list[dict[str, float]]:
        """Intermediate points need XY only; route headings are planning hints."""
        if raw is None:
            return []
        if not isinstance(raw, list) or len(raw) > 24:
            raise NavigationControlError(
                "INVALID_WAYPOINTS", "waypoints must be a list of at most 24 points.", 400,
            )
        points: list[dict[str, float]] = []
        for value in raw:
            if not isinstance(value, dict) or isinstance(value.get("x"), bool) or isinstance(value.get("y"), bool):
                raise NavigationControlError(
                    "INVALID_WAYPOINTS", "Each waypoint requires numeric x and y.", 400,
                )
            try:
                x, y = float(value["x"]), float(value["y"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise NavigationControlError(
                    "INVALID_WAYPOINTS", "Each waypoint requires finite x and y.", 400,
                ) from exc
            if not math.isfinite(x) or not math.isfinite(y):
                raise NavigationControlError(
                    "INVALID_WAYPOINTS", "Waypoint coordinates must be finite.", 400,
                )
            points.append({"x": x, "y": y})
        for i, point in enumerate(points):
            following = points[i + 1] if i + 1 < len(points) else final_goal
            dx, dy = following["x"] - point["x"], following["y"] - point["y"]
            # Humble planner requires a PoseStamped yaw even though arrival at
            # intermediate points is determined exclusively by 0.50m XY.
            point["yaw"] = math.atan2(dy, dx) if math.hypot(dx, dy) > 1e-6 else final_goal["yaw"]
        return points

    def _compute_path_for_route(
        self, final_goal: dict[str, float], waypoints: list[dict[str, float]]
    ) -> Any:
        if waypoints:
            return self._ros.compute_path_through_poses([*waypoints, final_goal])
        return self._ros.compute_path(final_goal)

    def _navigate_route(
        self, final_goal: dict[str, float], waypoints: list[dict[str, float]]
    ) -> Any:
        if waypoints:
            return self._ros.navigate_through_poses([*waypoints, final_goal])
        return self._ros.navigate_to_pose(final_goal)

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
    def _normalized_path(path: Any) -> list[dict[str, float | str]]:
        if not isinstance(path, list):
            return []
        # Keep the 32-heading lattice's pose orientations and zero-distance
        # rotation primitives. XY-only paths from legacy mocks remain valid.
        return annotate_path(path)

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
            "waypoints": [dict(p) for p in self._state["waypoints"]],
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
        self._state["waypoints"] = []
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

        @self._app.post("/api/navigation/control/beep")
        async def play_beep(request: Request):
            return await self._mutation(request, self.beep)

        @self._app.post("/api/navigation/control/led")
        async def control_led(request: Request):
            return await self._mutation(request, self.set_led)

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
