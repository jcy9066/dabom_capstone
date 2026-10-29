"""Server-authoritative navigation/control state and FastAPI contract."""

from __future__ import annotations

import asyncio
import logging
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
from server.auto_initial_pose import estimate_initial_pose, AutoPoseUncertain
from server.scan_pose_fusion import fuse_stationary_scans
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
    # Initial Smac planning is not physical motion; wait for first Nav2 Twist.
    CONTROLLER_COMMAND_START_TIMEOUT_SEC = 15.0
    # Keep A/B disjoint even when both endpoints have 0.55m XY tolerance.
    LOOP_MIN_SPACING_M = 1.20
    LOOP_STAGE_PENDING_TIMEOUT_SEC = 8.0
    PROGRESS_DISTANCE_M = 0.10
    # Low-speed curved motion can cover less XY distance than a straight
    # drive while still turning and translating. Accept a smaller verified
    # XY displacement ONLY with fresh, directed wheel-odometry yaw motion.
    # Stationary wheels / stale commands / straight runs keep the strict
    # 0.10m-in-5s timeout.
    CURVED_PROGRESS_DISTANCE_M = 0.02
    # Match Nav2's configured 0.212 m/s nominal linear command ceiling:
    # a valid curved drive at ~0.12m/s must not bypass odom-verified progress
    # simply because the previous gate only admitted <=0.10m/s.
    CURVED_PROGRESS_MAX_LINEAR_MPS = 0.212
    CURVED_PROGRESS_MIN_ANGULAR_RPS = 0.15
    CURVED_PROGRESS_MIN_ODOM_LINEAR_MPS = 0.005
    ROTATION_DATA_MAX_AGE_SEC = 0.5
    ROTATION_MIN_YAW_DELTA_RAD = 0.005
    # A verified in-place 180-degree turn can take 25-30s at the measured
    # wheel/PWM limits. Cap prolonged rotation while preserving the separate
    # 5s NO-PROGRESS timeout, live odom proof and swept-footprint checks.
    ROTATION_MAX_CUMULATIVE_SEC = 40.0
    STOP_CONFIRM_STABLE_SEC = 0.5
    STOP_CONFIRM_MAX_AGE_SEC = 0.5
    STOP_CONFIRM_TIMEOUT_SEC = 3.0
    # Bound Nav2 cancel + Pi STOP dispatch, independently of encoder settling.
    CANCEL_REQUEST_TIMEOUT_SEC = 15.0
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
        get_live_scan: Callable[[], dict[str, Any] | None] | None = None,
        clear_visualization: Callable[[], None] | None = None,
        clear_goal_visualization: Callable[[], None] | None = None,
        watchdog: NavigationWatchdogConfig | None = None,
        motor_output_enabled: bool = False,
        estop_cooldown_sec: float | None = None,
        goal_reached_tolerance_m: float | None = None,
        get_live_scans: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self._app = app
        self._map_api = map_api
        self._process = process_control
        self._ros = map_api.ros_control
        self._csrf_failure = csrf_failure
        self._robot_id = robot_id
        self._get_live_map = get_live_map
        self._get_live_pose = get_live_pose or (lambda: None)
        self._get_live_scan = get_live_scan or (lambda: None)
        self._get_live_scans = get_live_scans or (lambda: [])
        self._save_map = save_map
        self._send_robot_command = send_robot_command
        self._clear_visualization = clear_visualization
        self._clear_goal_visualization = clear_goal_visualization
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
        self._patrol_transition: str | None = None
        self._patrol_transition_error: str | None = None
        self._pi_navigation_mode: str | None = None
        self._pi_estop_latched: bool | None = None
        self._pi_safety_session: str | None = None
        self._pi_safety_epoch: int | None = None
        self._pi_seen_safety_sessions: set[str] = set()
        self._pi_session_stop_pending: str | None = None
        self._led_enabled: bool | None = None
        self._speaker_volume_percent: int | None = None
        self._volume_override_until = 0.0
        self._last_scan_at: float | None = None
        self._last_pose_at: float | None = None
        self._navigation_progress_pose: tuple[float, float] | None = None
        self._navigation_progress_odom_pose: tuple[float, float] | None = None
        self._navigation_progress_at: float | None = None
        self._navigation_command_baseline: int | None = None
        self._navigation_command_wait_started_at: float | None = None
        self._rotation_extension_sec = 0.0
        self._rotation_last_odom_sequence: int | None = None
        # Curve translation and in-place rotation are independent proofs;
        # the watchdog may sample odom before the pose subscriber does.
        self._curve_last_odom_sequence: int | None = None
        self._rotation_last_verified_yaw: float | None = None
        self._rotation_limit_reached = False
        self._blocked_failure_in_progress = False
        self._last_blocked_diagnostic: dict[str, Any] | None = None
        self._pending_nav_success: dict[str, Any] | None = None
        self._pending_goal_cancel: dict[str, Any] | None = None
        self._last_goal_cancel: dict[str, Any] | None = None
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
        # Opt-in, mutually exclusive recorded replay; inert in PR #93 flows.
        self._recorded_motor_active = False
        self._recorded_nav2_active = False
        self._recorded_cancel_callback: Callable[[], Awaitable[dict[str, Any]]] | None = None
        # Only the owner task may start or replace Nav2 Goals during a
        # multi-Goal LOOP. External Goal requests remain cancellable via Stop.
        self._loop_session_owner: asyncio.Task | None = None
        self._loop_session_path: list[dict[str, float]] | None = None
        self._loop_session_cancel_token = 0
        self._estop_generation = 0
        # Pi safety_epoch does not distinguish successive GPU Goals.
        self._goal_motion_generation = 0
        # A/B loop is server-owned. An epoch invalidates any in-flight leg plan.
        self._loop_goals: list[dict[str, float]] = []
        self._loop_leg_index = 0
        self._loop_started = False
        self._loop_pending = False
        self._loop_pending_since: float | None = None
        self._loop_epoch = 0
        self._loop_route_size = 0
        self._loop_route_start_index = 0
        self._loop_refresh_inflight = False
        self._loop_recovery: dict[str, Any] | None = None
        self._loop_reverse_used = False
        self._loop_stage: str | None = None
        self._loop_completed_legs = 0
        self._loop_next_turn_clockwise = True
        self._loop_turn_target: dict[str, float] | None = None
        self._loop_turn_requested: float | None = None
        # Session-only, per-direction physical path cache. Goal index 1=A->B;
        # index 0=B->A. Never reuse across a different map or new Loop.
        self._loop_path_cache: dict[int, list[dict[str, float]]] = {}
        self._loop_active_path: list[dict[str, float]] = []
        self._loop_follow_active = False
        self._loop_failed_follow_retry = False
        self._loop_validity_last_at = 0.0
        self._loop_cache_hits = 0
        self._loop_replans = 0
        self._replan_refresh_started_at = 0.0
        self._replan_refresh_inflight = False
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
            self._speaker_volume_percent = None
            self._volume_override_until = 0.0
            # A replaced Pi socket must establish a fresh encoder baseline.
            self._encoder_sequence = None
            if not connected:
                self._stop_confirmation_after_sequence = None
                self._estop_generation += 1
                if not self._estop_command_acknowledged:
                    self._estop_attempts = 0
                    self._estop_last_attempt_at = None

    def note_pi_status(self, payload: dict[str, Any], now: float | None = None) -> None:
        if not isinstance(payload, dict) or payload.get("type") == "ack":
            # Command ACK is not telemetry. Never use it to refresh Pi status.
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
                # Pi telemetry cannot override the GPU-owned active Nav2 mode.
                self._pi_navigation_mode = reported_navigation
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
            reported_volume = payload.get("speaker_volume_percent")
            if type(reported_volume) is int and 0 <= reported_volume <= 100:
                if (
                    reported_volume == self._speaker_volume_percent
                    or received_at >= self._volume_override_until
                ):
                    self._speaker_volume_percent = reported_volume
                    self._volume_override_until = 0.0
            elif received_at >= self._volume_override_until:
                self._speaker_volume_percent = None
            self._touch_locked(received_at)

    def note_pi_command_ack(
        self, robot_id: str, command: dict[str, Any], ack: dict[str, Any],
    ) -> None:
        """Synchronize STOP safety epoch from a command_id-matched Pi ACK.

        This is called only after RobotConnectionManager has matched the ACK
        to the current Pi WebSocket and pending command_id. Never infer a
        successful physical STOP from missing/malformed/negative ACK data.
        """
        if robot_id != self._robot_id or not isinstance(ack, dict):
            return
        command_type = command.get("type") if isinstance(command, dict) else None
        if command_type == "auto_drive" and ack.get("ok") is not True:
            detail = str(ack.get("error") or "negative Pi ACK")[:200]
            logging.getLogger(__name__).warning(
                "Pi rejected auto_drive command: %s", detail,
            )
            return
        if command_type != "stop" or ack.get("ok") is not True:
            return
        session = ack.get("safety_session")
        epoch = ack.get("safety_epoch")
        with self._lock:
            # Do not refresh status, release a STOP gate, or adopt an unknown
            # Pi session. Only advance the monotonically increasing epoch
            # for the already established live Pi identity.
            if (
                self._connected
                and isinstance(session, str)
                and session == self._pi_safety_session
                and type(epoch) is int
                and epoch >= 0
                and type(self._pi_safety_epoch) is int
                and epoch >= self._pi_safety_epoch
            ):
                self._pi_safety_epoch = epoch

    def note_navigation_sample(
        self,
        sample: str,
        now: float | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        received_at = now or time.time()
        rotation = None
        if sample == "pose":
            try:
                rotation = self._ros.rotation_status()
            except Exception:
                rotation = None
        with self._lock:
            if sample == "scan":
                self._last_scan_at = received_at
            elif sample == "pose":
                self._last_pose_at = received_at
                self._complete_manual_goal_locked(payload)
                self._note_navigation_progress_locked(
                    payload, received_at, rotation=rotation,
                )

    def note_nav2_replan_signal(self) -> None:
        """Replan signal is anonymous; recompute for our own active Goal.

        Never apply the incoming ROS /plan payload. A new, separately
        requested ComputePath result is bound to the current Goal generation
        and checked again before it can replace the approved path.
        """
        with self._lock:
            now = time.monotonic()
            if (self._state["navigation_mode"] != "DRIVING"
                or self._state["navigation_state"] not in {"NAVIGATING", "RESUMING"}
                or self._pending_goal_cancel is not None
                or self._state["emergency_stop"]
                or self._replan_refresh_inflight
                # A/B loop owns its cached path independently. Anonymous
                # /plan publications must not replan a still-valid loop leg.
                or bool(self._loop_goals and self._loop_started)
                or now - self._replan_refresh_started_at < 1.0):
                return
            goal = self._state["active_goal"]
            if not isinstance(goal, dict):
                return
            generation = self._goal_motion_generation
            waypoints = [dict(item) for item in self._state["waypoints"]]
            self._replan_refresh_inflight = True
            self._replan_refresh_started_at = now
            target = dict(goal)
        threading.Thread(
            target=self._refresh_approved_replan,
            args=(generation, target, waypoints),
            name="nav-goal-owned-path-refresh",
            daemon=True,
        ).start()

    def _refresh_approved_replan(
        self, generation: int, goal: dict[str, Any],
        waypoints: list[dict[str, Any]],
    ) -> None:
        try:
            # Current pose, map and goal come from the current Nav2 server.
            # Never label or trust the ROS /plan message itself.
            result = self._normalized_path(self._compute_path_for_route(goal, waypoints))
            with self._lock:
                if (len(result) >= 2
                    and self._goal_motion_generation == generation
                    and self._state["active_goal"] == goal
                    and self._state["navigation_mode"] == "DRIVING"
                    and self._state["navigation_state"] in {"NAVIGATING", "RESUMING"}
                    and self._pending_goal_cancel is None
                    and not self._state["emergency_stop"]
                    and result != self._state["planned_path"]):
                    self._state["planned_path"] = result
                    self._touch_locked()
        except Exception:
            # Failed refresh never authorizes an unknown or stale path.
            pass
        finally:
            with self._lock:
                self._replan_refresh_inflight = False

    def note_replanned_path(self, path: Any) -> None:
        """Unowned Nav2 /plan messages cannot overwrite a Goal-bound path.

        The approved replan is computed using our active Goal generation in
        _refresh_approved_replan; ROS topic payloads have no ownership token.
        """
        return

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
        cancel_completed = False
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
                    # Record evidence without changing the stop decision.
                    import logging
                    logging.getLogger(__name__).warning(
                        "encoder stop violation: baseline=%s current=%s "
                        "elapsed=%.3f threshold=%s grace=%.3f sequence=%s",
                        self._stop_encoder_ticks, ticks,
                        received_at - self._stop_commanded_at,
                        self._watchdog.stop_encoder_tick_threshold,
                        self._watchdog.stop_encoder_grace_sec, sequence,
                    )
                    self._encoder_stop_violation = True
                    self._estop_generation += 1
            # A delayed encoder sample after the cancel deadline is not success.
            self._expire_pending_cancel_locked(received_at)
            # An ACK is not wheel stop. Finalize only with fresh stable encoders.
            if (self._pending_goal_cancel is not None
                and self._pending_goal_cancel.get("stop_delivered") is True
                and self._stop_confirmation_after_sequence is not None
                and not self._state["emergency_stop"]
                and not self._encoder_stop_violation
                and self._stop_confirmed_locked(received_at)):
                self._pending_goal_cancel = None
                self._last_goal_cancel = {
                    "completed": True,
                    "nav_cancel_confirmed": True,
                    "stop_delivered": True,
                    "physical_stop_confirmed": True,
                }
                self._state["navigation_state"] = "READY"
                self._state["active_goal"] = None
                self._state["waypoints"] = []
                self._state["planned_path"] = []
                self._state["last_error"] = None
                self._reset_navigation_progress_locked()
                self._touch_locked(received_at)
                cancel_completed = True
        if cancel_completed and self._clear_goal_visualization is not None:
            self._clear_goal_visualization()

    def _expire_pending_cancel_locked(self, now: float) -> None:
        pending = self._pending_goal_cancel
        if pending is None:
            return
        stop_started_at = pending.get("started_at")
        if (stop_started_at is not None
            and now - stop_started_at < self.STOP_CONFIRM_TIMEOUT_SEC):
            return
        if stop_started_at is None:
            requested_at = pending.get("requested_at")
            within_request_window = (
                isinstance(requested_at, (int, float))
                and not isinstance(requested_at, bool)
                and math.isfinite(requested_at)
                and 0 <= now - requested_at < self.CANCEL_REQUEST_TIMEOUT_SEC
            )
            if (within_request_window
                and self._state["navigation_state"] == "CANCELING"):
                return
            error = (
                "NAV_CANCEL_TIMEOUT" if within_request_window is False
                else "CANCEL_STATE_INCONSISTENT"
            )
            # An unresolved Nav2 cancel / STOP dispatch is never physical
            # proof of safety. Reissue STOP through the terminal STOP worker.
            self._arm_pending_stop_locked()
            self._queue_cancel_failure_stop_locked("navigation_goal_cancel_unconfirmed")
        else:
            if now - stop_started_at < self.STOP_CONFIRM_TIMEOUT_SEC:
                return
            error = "STOP_NOT_CONFIRMED"
        self._pending_goal_cancel = None
        self._state["navigation_state"] = "FAILED"
        self._state["last_error"] = error
        self._last_goal_cancel = {
            **(self._last_goal_cancel or {}),
            "completed": False,
            "nav_cancel_confirmed": pending.get("nav_cancel_confirmed") is True,
            "stop_delivered": pending.get("stop_delivered") is True,
            "physical_stop_confirmed": False,
            "error": error,
        }
        # Keep active_goal and path for diagnosis; never clear the STOP gate.
        self._touch_locked(now)

    def _queue_cancel_failure_stop_locked(self, reason: str) -> None:
        if self._pending_terminal_stop is None and not self._state["emergency_stop"]:
            self._pending_terminal_stop = {
                "reason": reason,
                "attempts": 0,
                "last_attempt_at": None,
                "processing": False,
            }

    def _motion_allowed_locked(self, now: float) -> bool:
        if (self._state["emergency_stop"] or self._encoder_stop_violation
                or self._patrol_transition is not None
                or self._patrol_transition_error is not None
                or self._pending_nav_success is not None
                or self._state["navigation_state"] in {
                    "FAILED", "CANCELING", "CANCELING_STOP", "PAUSING_FOR_MANUAL",
                }):
            # A failed/ambiguous Pi mode transition cannot be repaired by
            # encoder STOP alone; require an explicit verified new transition.
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

    @staticmethod
    def _pi_command_delivered(response: Any) -> bool:
        """A nonempty error response is never a physical Pi acknowledgment.

        The local command sender historically returns bool; the WebSocket
        acknowledgment sender may return {"ok": bool, "delivered": bool}.
        Treat malformed/partial/exception values as failure, never truthiness.
        """
        if type(response) is bool:
            return response
        if not isinstance(response, dict):
            return False
        # A dict must explicitly report BOTH success and delivery.
        # Merely {"ok": true} is ambiguous and cannot release a STOP
        # or authorize a safety resume. Legacy boolean senders still
        # follow the explicitly handled branch above.
        return (
            response.get("ok") is True
            and response.get("delivered") is True
        )

    def _recorded_idle_locked(self) -> bool:
        """Recorded playback may start after a completed PR93 Set Goal.

        Preserve the legacy SUCCEEDED state; do not overwrite it with READY.
        Never treat an unfinished Goal, cancel, or pending STOP as idle.
        """
        return bool(
            self._state["navigation_state"] in {"READY", "SUCCEEDED"}
            and self._state["active_goal"] is None
            and self._pending_nav_success is None
            and self._pending_terminal_stop is None
            and self._pending_goal_cancel is None
        )

    def activate_recorded_motion(self) -> None:
        """Reserve motor ownership without mutating the normal Nav2 Goal state."""
        with self._lock:
            if self._recorded_motor_active or self._recorded_nav2_active:
                raise NavigationControlError("RECORDING_ALREADY_ACTIVE", "Replay or Nav2 already owns motors")
            self._assert_resume_safety_locked(require_estop=False)
            if (not self._motor_output_enabled or not self._recorded_idle_locked()
                or self._state["active_goal"] is not None or self._loop_goals
                or self._pending_goal_cancel is not None or self._patrol_transition is not None
                or self._watchdog_issue_locked(time.time()) is not None):
                raise NavigationControlError("RECORDED_NOT_READY", "Recorded replay requires idle healthy Nav2")
            if self._ros.navigation_status().get("state") in {"NAVIGATING", "RESUMING", "CANCELING"}:
                raise NavigationControlError("NAV2_BUSY", "Stop all Nav2 actions first")
            now = time.time()
            if not self._motion_allowed_locked(now):
                # A prior successfully completed PR93 Goal can have expired
                # encoder STOP proof. Reserve ownership only; the Recorded
                # executor MUST issue fresh Pi STOP and await stable encoder
                # evidence before its first motor packet.
                stale_success_only = (
                    self._state["navigation_state"] == "SUCCEEDED"
                    and self._stop_commanded_at is not None
                    and self._sensor_recovery is None
                    and self._patrol_transition_error is None
                    and not self._encoder_stop_violation
                    and not self._state["emergency_stop"]
                    and self._recorded_idle_locked()
                )
                if not stale_success_only:
                    raise NavigationControlError(
                        "STOP_NOT_CONFIRMED", "Previous motor stop not confirmed")
            self._goal_motion_generation += 1
            self._recorded_motor_active = True

    def set_recorded_cancel_callback(
        self, callback: Callable[[], Awaitable[dict[str, Any]]]
    ) -> None:
        """Register the recorded-loop STOP handler after both APIs initialize."""
        with self._lock:
            self._recorded_cancel_callback = callback

    async def _stop_recorded_before_transition(self) -> None:
        """Physical STOP and complete ownership release precede mode changes."""
        with self._lock:
            active = self._recorded_motor_active or self._recorded_nav2_active
            handler = self._recorded_cancel_callback
        if handler is None:
            if active:
                raise NavigationControlError(
                    "RECORDED_STOP_UNAVAILABLE", "Recorded Loop cannot be safely stopped")
            return
        # Even without a motor-owner bit, a replay Task may be STOPPING
        # between a Nav2 return and its physical encoder proof. Always ask
        # its idempotent stop handler before changing legacy control modes.
        result = await handler()
        with self._lock:
            if (not isinstance(result, dict) or result.get("ok") is not True
                or self._recorded_motor_active or self._recorded_nav2_active):
                raise NavigationControlError(
                    "RECORDED_STOP_UNCONFIRMED", "Stop Recorded Loop before changing controls")

    def recorded_nav2_takeover(self) -> None:
        """Transfer exclusive replay control to Nav2 after physical STOP."""
        with self._lock:
            if (not self._recorded_motor_active or self._recorded_nav2_active
                or not self._stop_confirmed_locked(time.time())
                or self._watchdog_issue_locked(time.time()) is not None):
                raise NavigationControlError("RECORDED_HANDOFF_DENIED",
                                             "Nav2 handoff requires confirmed STOP")
            self._recorded_motor_active = False
            self._recorded_nav2_active = True
            self._goal_motion_generation += 1

    def recorded_nav2_return(self) -> None:
        with self._lock:
            if (not self._recorded_nav2_active
                or not self._stop_confirmed_locked(time.time())
                or self._state["emergency_stop"]
                or self._watchdog_issue_locked(time.time()) is not None):
                raise NavigationControlError("RECORDED_REJOIN_DENIED",
                                             "Nav2 return requires confirmed STOP")
            self._recorded_nav2_active = False
            self._recorded_motor_active = True
            self._goal_motion_generation += 1

    def suspend_recorded_motion(self) -> bool:
        """Revoke any in-flight recorded motion generation immediately."""
        with self._lock:
            active = bool(self._recorded_motor_active or self._recorded_nav2_active)
            self._recorded_motor_active = False
            self._recorded_nav2_active = False
            if active:
                self._goal_motion_generation += 1
            return active

    def recorded_nav2_health(self) -> bool:
        with self._lock:
            return bool(
                self._recorded_nav2_active
                and self._robot_mode == "auto"
                and self._pi_navigation_mode == "DRIVING"
                and self._state["navigation_mode"] == "DRIVING"
                and not self._state["emergency_stop"]
                and self._recorded_idle_locked()
                and self._state["active_goal"] is None
                and not self._loop_goals
                and self._watchdog_issue_locked(time.time()) is None
                and self._connected and self._pi_estop_latched is False
            )

    def release_recorded_motion(self) -> None:
        with self._lock:
            self._recorded_motor_active = False
            self._recorded_nav2_active = False
            self._goal_motion_generation += 1

    def recorded_safety_context(self) -> dict[str, Any] | None:
        """Separate, opt-in auto-drive permit; existing motion API remains unchanged."""
        with self._lock:
            now = time.time()
            if (not self._recorded_motor_active or not self._motor_output_enabled
                or self._robot_mode != "auto" or self._pi_navigation_mode != "DRIVING"
                or self._state["navigation_mode"] != "DRIVING"
                or not self._recorded_idle_locked()
                or self._state["emergency_stop"] or self._loop_goals
                or self._state["active_goal"] is not None
                or not self._connected or self._pi_estop_latched is not False
                or not self._pi_safety_session or type(self._pi_safety_epoch) is not int
                or self._age(now, self._pi_updated_at) > self._watchdog.pi_timeout_sec
                or not self._motion_allowed_locked(now)
                or self._watchdog_issue_locked(now) is not None):
                return None
            return {"safety_session": self._pi_safety_session,
                    "safety_epoch": self._pi_safety_epoch,
                    "goal_generation": self._goal_motion_generation,
                    "estop_generation": self._estop_generation}

    def recorded_motion_delivered(self, expected: dict[str, Any]) -> bool:
        with self._lock:
            if self.recorded_safety_context() != expected:
                return False
            self._stop_commanded_at = None
            self._stop_encoder_ticks = None
            self._stop_confirmation_after_sequence = None
            self._post_resume_motion_ready = False
            return True

    def recorded_stop_begin(self) -> None:
        with self._lock:
            self._arm_pending_stop_locked()

    def recorded_stop_acknowledged(self, accepted: bool) -> None:
        with self._lock:
            if accepted and not self._state["emergency_stop"]:
                self._stop_confirmation_after_sequence = self._encoder_sequence
                self._stop_confirmation_ticks = None
                self._stop_confirmation_started_at = None
                self._stop_confirmation_last_at = None
                self._stop_confirmation_samples = 0

    def recorded_stop_proven(self) -> bool:
        with self._lock:
            return self._stop_confirmed_locked(time.time())

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
                and not self.recorded_nav2_health()
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
                "goal_generation": self._goal_motion_generation,
                "robot_mode": self._robot_mode,
                "pi_navigation_mode": self._pi_navigation_mode,
            }

    def note_authorized_motion(
        self, expected_safety: dict[str, Any] | None = None
    ) -> bool:
        with self._lock:
            now = time.time()
            if not self._motion_allowed_locked(now):
                return False
            # Motion safety is re-evaluated at the last server authorization
            # boundary. A cached context fetched before a Goal terminates
            # must not authorize a delayed auto-drive after CANCELED/FAILED.
            if (
                self._robot_mode == "auto"
                and self._state["navigation_state"] not in self.ACTIVE_NAV_STATES
                and not self.recorded_nav2_health()
            ):
                return False
            if expected_safety is not None and (
                not self._connected
                or self._age(now, self._pi_updated_at) > self._watchdog.pi_timeout_sec
                or self._pi_estop_latched is not False
                or self._pi_safety_session != expected_safety.get("safety_session")
                or self._pi_safety_epoch != expected_safety.get("safety_epoch")
                or self._estop_generation != expected_safety.get("estop_generation")
                or self._goal_motion_generation != expected_safety.get("goal_generation")
                or self._robot_mode != expected_safety.get("robot_mode")
                or self._pi_navigation_mode != expected_safety.get("pi_navigation_mode")
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

    def _sync_waypoint_progress_locked(self, ros_nav: dict[str, Any]) -> None:
        # Humble NavigateThroughPoses publishes remaining pose count after
        # RemovePassedGoals removes an intermediate within 0.50m XY.
        # Keep the final Goal separate and never trim it from a route.
        if (
            self._state["navigation_state"] != "NAVIGATING"
            or ros_nav.get("action") != "navigate_through_poses"
            or not self._state["waypoints"]
        ):
            return
        count = ros_nav.get("number_of_poses_remaining")
        if isinstance(count, bool) or not isinstance(count, int):
            return
        outstanding = len(self._state["waypoints"])
        if not 1 <= count <= outstanding + 1:
            return
        passed = outstanding + 1 - count
        if passed > 0:
            self._state["waypoints"] = self._state["waypoints"][passed:]
            if self._loop_goals and self._loop_route_size:
                self._loop_leg_index = (
                    self._loop_route_start_index + self._loop_route_size - count
                ) % 2
            self._touch_locked()

    def _sync_ros_navigation_terminal_locked(
        self,
        ros_nav: dict[str, Any],
        current: float,
        *,
        allow_during_blocked_failure: bool = False,
    ) -> str | None:
        self._sync_waypoint_progress_locked(ros_nav)
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
        recovering_pivot = (
            terminal == "FAILED"
            and self._loop_stage == "turn"
            and self._loop_recovery is None
            and self._arm_loop_pivot_recovery_locked(ros_nav, current)
        )
        # Cached FollowPath can abort before the periodic obstacle monitor
        # gets its next IsPathValid result. Give that specific leg ONE
        # forward-only replan, but ONLY after an actual Pi/encoder STOP.
        # Never retry a Spin, backup, operator cancel, or general Nav2 fault.
        recovering_cached = (
            terminal == "FAILED"
            and not recovering_pivot
            and self._loop_started and self._loop_stage == "drive"
            and self._loop_follow_active and not self._loop_failed_follow_retry
            and ros_nav.get("action") == "follow_path"
            and self._pending_goal_cancel is None
            and not self._state["emergency_stop"]
        )
        if recovering_cached:
            self._loop_failed_follow_retry = True
            self._loop_path_cache.pop(self._loop_leg_index, None)
            self._loop_active_path = []
            self._loop_follow_active = False
            self._loop_replans += 1
            self._loop_stage = "drive_pending"
            self._loop_pending = True
            self._loop_pending_since = current
            self._state["active_goal"] = None
            self._state["planned_path"] = []
        elif not recovering_pivot:
            # A failed backup/reapproach/spin is terminal: no second reverse.
            self._clear_loop_locked()
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
        self._state["navigation_state"] = (
            "LOOP_RECOVERY_STOPPING" if recovering_pivot
            else ("READY" if recovering_cached
            else ("CANCELING_STOP" if terminal == "CANCELED" else terminal))
        )
        if terminal == "CANCELED":
            self._pending_goal_cancel = {
                "started_at": current,
                "nav_cancel_requested": False,
                "nav_cancel_confirmed": True,
                "nav_terminal": "CANCELED",
                "stop_delivered": False,
                "physical_stop_confirmed": False,
            }
            self._last_goal_cancel = {
                "completed": False, "nav_cancel_confirmed": True,
                "stop_delivered": False, "physical_stop_confirmed": False,
            }
        self._reset_navigation_progress_locked()
        self._touch_locked(current)
        return terminal

    def state_response(self, now: float | None = None) -> dict[str, Any]:
        current = now or time.time()
        try:
            ros_nav = self._ros.navigation_status()
        except Exception:
            ros_nav = {"state": "UNAVAILABLE"}
        try:
            applied = self._ros.applied_follow_path()
        except (AttributeError, RuntimeError):
            applied = None
        with self._lock:
            self._sync_ros_navigation_terminal_locked(ros_nav, current)
            self._expire_pending_cancel_locked(current)
            payload = {
                "ok": True,
                "robot_id": self._robot_id,
                "connected": self._connected,
                "robot_mode": self._robot_mode,
                "pi_navigation_mode": self._pi_navigation_mode,
                "cancel_progress": (
                    dict(self._pending_goal_cancel)
                    if self._pending_goal_cancel is not None else None
                ),
                "last_cancel": dict(self._last_goal_cancel) if self._last_goal_cancel else None,
                "cancel_completed": bool(
                    self._last_goal_cancel and self._last_goal_cancel.get("completed")
                ),
                "goal_generation": self._goal_motion_generation,
                "loop_goals": [dict(p) for p in self._loop_goals],
                "loop_leg_index": self._loop_leg_index if self._loop_goals else None,
                "loop_pending": self._loop_pending,
                "loop_refreshing": self._loop_refresh_inflight,
                "loop_recovery_phase": (
                    self._loop_recovery["phase"] if self._loop_recovery else None
                ),
                "loop_reverse_used": self._loop_reverse_used,
                "loop_active": bool(self._loop_goals and self._loop_started),
                "loop_stage": self._loop_stage,
                "loop_completed_legs": self._loop_completed_legs,
                "loop_cached_directions": sorted(self._loop_path_cache),
                "loop_cache_hits": self._loop_cache_hits,
                "loop_replans": self._loop_replans,
                "patrol_transition": self._patrol_transition,
                "patrol_transition_error": self._patrol_transition_error,
                "last_blocked_diagnostic": (
                    dict(self._last_blocked_diagnostic)
                    if self._last_blocked_diagnostic is not None else None
                ),
                "patrol_ready": bool(
                    self._patrol_transition is None
                    and self._connected and self._pi_updated_at is not None
                    and self._age(current, self._pi_updated_at) <= self._watchdog.pi_timeout_sec
                    and self._robot_mode == "auto" and self._pi_navigation_mode == "DRIVING"
                    and self._state["navigation_mode"] == "DRIVING"
                    and self._state["localization_ready"] and self._state["nav2_ready"]
                    and self._pi_estop_latched is False
                    and self._patrol_transition_error is None
                    and self._motion_allowed_locked(current)
                    and not self._state["emergency_stop"]
                ),
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
                "speaker_volume_percent": (
                    self._speaker_volume_percent
                    if self._connected
                    and self._pi_updated_at is not None
                    and self._age(current, self._pi_updated_at) <= self._watchdog.pi_timeout_sec
                    else None
                ),
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
            payload["executed_path"] = self._current_executed_path_locked(ros_nav, applied)
        return payload

    def _current_executed_path_locked(
        self, ros_nav: dict[str, Any], applied: Any,
    ) -> list[dict[str, float | str]]:
        """Only actual FollowPath-installed paths matching current Nav2 action."""
        if (self._state["navigation_mode"] != "DRIVING"
            or self._state["navigation_state"] not in {"NAVIGATING", "RESUMING"}
            or self._state["emergency_stop"] or self._encoder_stop_violation
            or self._pending_goal_cancel is not None
            or ros_nav.get("state") != "NAVIGATING"
            or not isinstance(applied, dict)
            or type(applied.get("route_generation")) is not int
            or applied["route_generation"] != ros_nav.get("route_generation")):
            return []
        goal = self._state.get("active_goal")
        path = applied.get("path")
        if (not isinstance(goal, dict) or not isinstance(path, list)
            or not 2 <= len(path) <= 5000 or not isinstance(path[-1], dict)):
            return []
        terminal = path[-1]
        try:
            dx = float(goal["x"]) - float(terminal["x"])
            dy = float(goal["y"]) - float(terminal["y"])
            d_yaw = math.atan2(
                math.sin(float(goal["yaw"]) - float(terminal["yaw"])),
                math.cos(float(goal["yaw"]) - float(terminal["yaw"])),
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            return []
        if (not all(math.isfinite(v) for v in (dx, dy, d_yaw))
            or math.hypot(dx, dy) > 0.20 or abs(d_yaw) > 0.20):
            return []
        return [dict(point) for point in path]

    async def _await_pi_navigation_telemetry(
        self, expected: str, after: float | None, timeout_sec: float | None = None
    ) -> bool:
        """Require fresh Pi telemetry, bounded by the configured watchdog."""
        # Status reports are periodic and may not arrive within 1.5 seconds.
        # Never loosen the freshness check itself.
        wait = max(1.5, self._watchdog.pi_timeout_sec) if timeout_sec is None else timeout_sec
        deadline = time.monotonic() + wait
        while True:
            with self._lock:
                confirmed = (
                    self._connected
                    and self._pi_updated_at is not None
                    and (after is None or self._pi_updated_at > after)
                    and self._age(time.time(), self._pi_updated_at) <= self._watchdog.pi_timeout_sec
                    and self._pi_navigation_mode == expected
                    # Mode configuration is permitted while physically
                    # E-stopped; only motion authorization requires release.
                    # Never accept an unknown/mismatched stop state.
                    and (self._pi_estop_latched is False and not self._state["emergency_stop"]
                         or self._pi_estop_latched is True and self._state["emergency_stop"])
                )
            if confirmed:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)

    async def switch_patrol_mode(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Switch Pi AUTO/MANUAL authority without changing navigation mode."""
        target = str(payload.get("mode") or "").strip().lower()
        if target not in {"auto", "manual"}:
            raise NavigationControlError("INVALID_PATROL_MODE", "mode must be auto or manual.", 400)
        await self._stop_recorded_before_transition()
        if target == "manual":
            with self._lock:
                self._clear_loop_locked()
        with self._lock:
            if self._patrol_transition is not None:
                raise NavigationControlError("PATROL_TRANSITION_BUSY", "Mode change in progress.", 409)
            if not self._connected or self._age(time.time(), self._pi_updated_at) > self._watchdog.pi_timeout_sec:
                raise NavigationControlError("PI_CONNECTION_LOSS", "Fresh Pi status is required.", 503)
            if self._state["emergency_stop"] or self._pi_estop_latched is not False:
                raise NavigationControlError("EMERGENCY_STOP_ACTIVE", "Release the safety stop first.", 409)
            # AUTO/MANUAL remains independent of MAPPING/DRIVING.
            # Motion authorization still requires Nav2, localization and Pi readiness.
            if (self._robot_mode == target and not (
                target == "manual"
                and self._state["navigation_state"] in {"NAVIGATING", "RESUMING"}
            )):
                # Duplicates are success only when safety is settled. An
                # ambiguous earlier ACK may have switched the Pi, but must
                # be explicitly reverified after a fresh physical STOP.
                if self._patrol_transition_error is None:
                    if not self._motion_allowed_locked(time.time()):
                        raise NavigationControlError(
                            "STOP_NOT_CONFIRMED", "The Pi stop is not safely settled.", 409
                        )
                    return self.state_response()
                if not self._stop_confirmed_locked(time.time()):
                    raise NavigationControlError(
                        "STOP_NOT_CONFIRMED", "Verify a fresh encoder STOP before retrying mode sync.", 409
                    )
            expected_navigation_mode = self._state["navigation_mode"]
            self._patrol_transition = target
            self._patrol_transition_error = None
            self._touch_locked()
        try:
            if target == "manual" and expected_navigation_mode == "DRIVING":
                paused = await self.pause_for_manual()
                if (paused.get("stop_delivered") is not True
                    or paused.get("emergency_stop") is True
                    or paused.get("navigation_state") in {
                        "FAILED", "NAVIGATING", "RESUMING", "EMERGENCY_STOPPED"
                    }):
                    raise NavigationControlError("MANUAL_STOP_FAILED", "Navigation could not be safely paused.", 409)
            with self._lock:
                # E-STOP is intentionally not serialized behind dashboard
                # mutations, so it may interrupt the pause or Nav2 checks.
                if self._state["emergency_stop"] or self._pi_estop_latched is not False:
                    raise NavigationControlError(
                        "EMERGENCY_STOP_ACTIVE", "Emergency stop interrupted the mode switch.", 409
                    )
            with self._lock:
                pre_mode_telemetry = self._pi_updated_at
            ack = await self._send_robot_command(
                self._robot_id, {"type": "mode", "mode": target}
            )
            if not self._pi_command_delivered(ack):
                raise NavigationControlError("PI_MODE_ACK_FAILED", "Pi did not acknowledge mode.", 503)
            deadline = time.monotonic() + max(1.5, self._watchdog.pi_timeout_sec)
            while True:
                with self._lock:
                    confirmed = (
                        self._connected and self._pi_updated_at is not None
                        and (pre_mode_telemetry is None or self._pi_updated_at > pre_mode_telemetry)
                        and self._age(time.time(), self._pi_updated_at) <= self._watchdog.pi_timeout_sec
                        and self._robot_mode == target and self._pi_estop_latched is False
                        and not self._state["emergency_stop"]
                    )
                if confirmed:
                    break
                if time.monotonic() >= deadline:
                    raise NavigationControlError("PI_MODE_TELEMETRY_TIMEOUT", "Pi mode telemetry unconfirmed.", 504)
                await asyncio.sleep(0.05)
        except Exception as exc:
            with self._lock:
                self._patrol_transition_error = getattr(exc, "error_code", type(exc).__name__)
                self._arm_pending_stop_locked()
                self._touch_locked()
            # Ambiguous ACK failures can still mean the Pi changed mode.
            # Independently STOP and require *subsequent* encoder proof.
            stop_ack = await self._send_robot_command(
                self._robot_id, {"type": "stop", "reason": "patrol_mode_sync_failed"}
            )
            with self._lock:
                if (self._pi_command_delivered(stop_ack)
                    and not self._state["emergency_stop"]):
                    self._stop_commanded_at = time.time()
                    self._stop_encoder_ticks = self._encoder_ticks
                    self._stop_confirmation_after_sequence = self._encoder_sequence or 0
                    self._stop_confirmation_ticks = None
                    self._stop_confirmation_started_at = None
                    self._stop_confirmation_last_at = None
                    self._stop_confirmation_samples = 0
                    self._touch_locked()
            raise
        finally:
            with self._lock:
                self._patrol_transition = None
                self._touch_locked()
        return self.state_response()

    async def switch_mode(self, payload: dict[str, Any], user: str = "unknown") -> dict[str, Any]:
        mode = str(payload.get("mode", "")).strip().upper()
        if mode not in self.MODES:
            raise NavigationControlError("INVALID_MODE", "mode must be MAPPING or DRIVING.", 400)

        if mode == "MAPPING":
            await self._stop_recorded_before_transition()
            with self._lock:
                self._clear_loop_locked()
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
                mapping_process_status = await asyncio.to_thread(
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

            # Re-selecting an existing Mapping process is not a new SLAM
            # session: preserve its map, source timestamps and TF evidence.
            if (self._clear_visualization is not None
                and not mapping_process_status.get("reused_session", False)):
                self._clear_visualization()

            with self._lock:
                pre_mode_telemetry = self._pi_updated_at
            delivered = await self._send_robot_command(
                self._robot_id,
                {"type": "navigation_mode", "mode": "mapping"},
            )
            pi_confirmed = (
                self._pi_command_delivered(delivered)
                and await self._await_pi_navigation_telemetry("MAPPING", pre_mode_telemetry)
            )
            if not self._pi_command_delivered(delivered) or not pi_confirmed:
                # The GPU process is already in MAPPING. Preserve that truth
                # and expose Pi divergence explicitly, never as DRIVING ready.
                with self._lock:
                    self._arm_pending_stop_locked()
                    self._state.update({
                        "navigation_mode": "MAPPING",
                        "navigation_state": "ERROR",
                        "active_goal": None,
                        "waypoints": [],
                        "planned_path": [],
                        "active_map": None,
                        "localization_ready": False,
                        "nav2_ready": False,
                        "last_error": "PI_MODE_SYNC_FAILED",
                    })
                    self._touch_locked()
                await self._send_robot_command(
                    self._robot_id,
                    {"type": "stop", "reason": "mapping_pi_mode_sync_failed"},
                )
                raise NavigationControlError("PI_MODE_SYNC_FAILED", "Pi did not confirm MAPPING mode.", 503)
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
        auto_pose_encoder_baseline = None

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

            if (live_map.get("source") != "slam_toolbox"
                or live_map.get("map_verified") is not True):
                raise NavigationControlError(
                    "SLAM_MAP_NOT_VERIFIED",
                    "Preview LiDAR observations cannot be used as a Driving map.",
                    409,
                )
            if initial_pose_payload is None:
                initial_pose_payload = self._current_mapping_initial_pose()
            if initial_pose_payload is None:
                raise NavigationControlError(
                    "INITIAL_POSE_UNAVAILABLE",
                    "Current Mapping has no verified map-frame pose. "
                    "Obtain a fresh localization estimate or select a position on the map.",
                    409,
                )

            # Nav2 map_server needs a map file. Snapshot the current in-memory
            # Mapping result automatically so the operator does not have to
            # manually save and reload it before setting a Goal.
            saved = await asyncio.to_thread(self._save_map, dict(live_map), map_name)
            map_name = saved.get("map_name")
        elif source == "existing":
            # Do not invent an arbitrary origin when a saved map is selected.
            # Automatic localization must prove a fresh, unambiguous scan
            # match; otherwise the operator must supply Initial Pose.
            if initial_pose_payload is None:
                selected_for_match = self._map_api.resolve_map(map_name)
                with self._lock:
                    manual = self._robot_mode == "manual"
                    not_moving = (
                        self._state["navigation_state"] not in {
                            "NAVIGATING", "RESUMING", "CANCELING", "CANCELING_STOP",
                            "PAUSING_FOR_MANUAL", "REPLACING_GOAL", "EMERGENCY_STOPPED",
                        }
                        and self._motion_allowed_locked(time.time())
                    )
                    auto_pose_encoder_baseline = self._encoder_ticks
                if not manual or not not_moving or auto_pose_encoder_baseline is None:
                    raise NavigationControlError(
                        "AUTO_POSE_REQUIRES_STOP", "Select manual/stopped operation before automatic localization.", 409
                    )
                try:
                    # Merge only fresh LiDAR frames with unchanged encoder
                    # ticks; never align moving scans at a guessed pose.
                    scan = await asyncio.to_thread(
                        fuse_stationary_scans, self._get_live_scans()
                    )
                    initial_pose_payload = await asyncio.to_thread(
                        estimate_initial_pose, selected_for_match, scan,
                        mount_x=env_float("LIDAR_X"),
                        mount_y=env_float("LIDAR_Y"),
                        mount_yaw=env_float("LIDAR_YAW"),
                    )
                except (AutoPoseUncertain, OSError, ValueError, KeyError) as exc:
                    raise NavigationControlError(
                        "AUTO_INITIAL_POSE_UNCERTAIN",
                        f"Automatic localization cannot confirm a unique position: {exc}. Enter Initial Pose manually.",
                        409,
                    ) from exc
                # A valid snapshot is not proof the RC car remained stopped
                # while expensive global scan matching was running.
                with self._lock:
                    if (self._encoder_ticks != auto_pose_encoder_baseline
                        or self._robot_mode != "manual"
                        or self._state["emergency_stop"]
                        or not self._motion_allowed_locked(time.time())):
                        raise NavigationControlError(
                            "AUTO_POSE_MOVED", "Robot moved during automatic localization; retry while stopped.", 409,
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
            if auto_pose_encoder_baseline is not None:
                with self._lock:
                    if (self._encoder_ticks != auto_pose_encoder_baseline
                        or self._robot_mode != "manual"
                        or self._state["emergency_stop"]
                        or not self._motion_allowed_locked(time.time())):
                        raise NavigationControlError(
                            "AUTO_POSE_MOVED", "Robot moved before Driving readiness; reload the map.", 409,
                        )
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
        with self._lock:
            pre_mode_telemetry = self._pi_updated_at
        delivered = await self._send_robot_command(
            self._robot_id,
            {"type": "navigation_mode", "mode": "driving"},
        )
        pi_confirmed = (
            self._pi_command_delivered(delivered)
            and await self._await_pi_navigation_telemetry("DRIVING", pre_mode_telemetry)
        )
        if not self._pi_command_delivered(delivered) or not pi_confirmed:
            try:
                await asyncio.to_thread(self._process.stop, "DRIVING")
            except Exception:
                # The process is not guaranteed to be stopped; motor
                # authorization remains blocked until a fresh safe state.
                pass
            with self._lock:
                self._arm_pending_stop_locked()
                self._state["navigation_state"] = "ERROR"
                self._state["localization_ready"] = False
                self._state["nav2_ready"] = False
                self._state["last_error"] = "PI_MODE_SYNC_FAILED"
                self._touch_locked()
            raise NavigationControlError("PI_MODE_SYNC_FAILED", "Pi did not confirm DRIVING mode.", 503)
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
        delivered = self._pi_command_delivered(stop_result)
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

    async def plan_goal(self, payload: dict[str, Any], *,
                        session_cached_path: list[dict[str, float]] | None = None
                        ) -> dict[str, Any]:
        owner = self._loop_session_owner
        if owner is not None and not owner.done() and owner is not asyncio.current_task():
            raise NavigationControlError(
                "LOOP_ACTIVE", "Stop active LOOP before replacing a Goal", 409)
        goal = self._validated_goal(payload.get("goal", payload))
        waypoints = self._validated_waypoints(payload.get("waypoints"), goal)
        raw_loop = payload.get("loop_goals")
        loop_goals: list[dict[str, float]] = []
        if raw_loop is not None:
            if not isinstance(raw_loop, list) or len(raw_loop) != 2 or waypoints:
                raise NavigationControlError(
                    "INVALID_LOOP", "Loop requires exactly A and B without intermediate waypoints.", 400,
                )
            loop_goals = [self._validated_goal(p) for p in raw_loop]
            if goal != loop_goals[0] or math.hypot(
                loop_goals[0]["x"] - loop_goals[1]["x"],
                loop_goals[0]["y"] - loop_goals[1]["y"],
            ) < self.LOOP_MIN_SPACING_M:
                raise NavigationControlError(
                    "INVALID_LOOP", "A and B must be at least 1.20m apart to avoid overlapping arrival regions.", 400,
                )
        with self._lock:
            if self._recorded_motor_active or self._recorded_nav2_active:
                raise NavigationControlError("RECORDED_REPLAY_ACTIVE",
                                             "Stop Recorded Loop before setting a new Goal", 409)
            self._expire_pending_cancel_locked(time.time())
            if self._pending_goal_cancel is not None:
                raise NavigationControlError("CANCEL_IN_PROGRESS", "Wait for the encoder-confirmed Goal cancel.", 409)
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
        if loop_goals:
            self._validate_goal_in_active_map(loop_goals[1])
        if replacing:
            await self._stop_for_goal_replacement()

        try:
            if replacing:
                # Destination validation occurred before cancel; confirm the
                # live sensor and Pi health again after the physical STOP.
                self._assert_watchdogs_healthy()
            if session_cached_path is not None:
                # Private Python-only option: HTTP Goal requests cannot pass it.
                if (self._loop_session_owner is not asyncio.current_task()
                    or waypoints or loop_goals):
                    raise NavigationControlError(
                        "LOOP_PATH_OWNER", "Cached path requires active LOOP owner", 409)
                normalized_path = self._normalized_path(session_cached_path)
                if not 2 <= len(normalized_path) <= 2000:
                    raise NavigationControlError(
                        "LOOP_PATH_INVALID", "Invalid recorded path length", 422)
                live = self._safe_live_pose()
                if (not self._fresh_loop_pose(live)
                    or math.hypot(live["x"] - normalized_path[0]["x"],
                                  live["y"] - normalized_path[0]["y"]) > 0.35
                    or math.hypot(goal["x"] - normalized_path[-1]["x"],
                                  goal["y"] - normalized_path[-1]["y"]) > 0.40):
                    raise NavigationControlError(
                        "LOOP_PATH_MISMATCH", "Cached path endpoints not aligned", 409)
                if not await asyncio.to_thread(self._ros.loop_path_valid, normalized_path):
                    raise NavigationControlError(
                        "LOOP_PATH_BLOCKED", "Cached path obstructed", 409)
            else:
                # Existing SET GOAL/#93 behavior remains unchanged.
                path = await asyncio.to_thread(
                    self._compute_path_for_route, goal, waypoints,
                    loop_forward=bool(loop_goals),
                )
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
            self._goal_motion_generation += 1
            self._clear_loop_locked()
            self._loop_goals = [dict(p) for p in loop_goals]
            self._loop_leg_index = 0
            self._last_goal_cancel = None
            self._state["active_goal"] = goal
            self._state["waypoints"] = [dict(point) for point in waypoints]
            self._state["planned_path"] = normalized_path
            self._loop_session_path = (
                [dict(p) for p in normalized_path]
                if session_cached_path is not None else None
            )
            self._state["navigation_state"] = "PATH_READY"
            self._state["last_error"] = None
            self._touch_locked()
        return self.state_response()

    async def start_navigation(self) -> dict[str, Any]:
        owner = self._loop_session_owner
        if owner is not None and not owner.done() and owner is not asyncio.current_task():
            raise NavigationControlError(
                "LOOP_ACTIVE", "Stop active LOOP before starting another Goal", 409)
        with self._lock:
            if self._recorded_motor_active or self._recorded_nav2_active:
                raise NavigationControlError("RECORDED_REPLAY_ACTIVE",
                                             "Stop Recorded Loop before starting Nav2", 409)
            goal = dict(self._state["active_goal"] or {})
            waypoints = [dict(p) for p in self._state["waypoints"]]
            loop_goals = [dict(p) for p in self._loop_goals]
            session_path = (
                [dict(p) for p in self._loop_session_path]
                if self._loop_session_path is not None else None
            )
            session_owner = self._loop_session_owner
            session_token = self._loop_session_cancel_token
            loop_epoch = self._loop_epoch
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
        command_baseline = self._nonzero_command_sequence()
        if loop_goals:
            # The first leg ends at A, never at an intermediate Nav2 pose.
            # Every A/B turnaround is a distinct physically verified Spin.
            live = self._safe_live_pose()
            if live is None:
                raise NavigationControlError("LOOP_POSE_MISSING", "A fresh pose is required.", 409)
            dx, dy = goal["x"] - live["x"], goal["y"] - live["y"]
            inbound = math.atan2(dy, dx) if math.hypot(dx, dy) > 0.1 else live["yaw"]
            goal["yaw"] = math.atan2(math.sin(inbound), math.cos(inbound))
            with self._lock:
                if (self._loop_epoch != loop_epoch or self._state["emergency_stop"]
                    or self._state["navigation_state"] != "PATH_READY"):
                    raise NavigationControlError("LOOP_INTERRUPTED", "Loop start interrupted.", 409)
        command_baseline = self._nonzero_command_sequence()
        if (loop_goals or session_owner is asyncio.current_task()) and hasattr(
            self._ros, "begin_loop_trace"
        ):
            await asyncio.to_thread(self._ros.begin_loop_trace)
        if session_path is not None:
            def cached_preflight() -> bool:
                with self._lock:
                    return (
                        self._loop_session_owner is session_owner
                        and self._loop_session_cancel_token == session_token
                        and session_owner is not None and not session_owner.done()
                        and self._estop_generation == estop_generation
                        and not self._state["emergency_stop"]
                    )
            result = await asyncio.to_thread(
                self._ros.follow_loop_path, session_path,
                recorded_auto=True, recorded_preflight=cached_preflight,
            )
        else:
            result = (
                await asyncio.to_thread(self._ros.navigate_to_pose, goal, loop_reapproach=True)
                if loop_goals
                else await asyncio.to_thread(self._navigate_route, goal, waypoints)
            )
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
                or (bool(loop_goals) and self._loop_epoch != loop_epoch)
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
                if loop_goals and self._loop_epoch == loop_epoch:
                    self._loop_started = True
                    self._loop_stage = "drive"
                    self._loop_leg_index = 0
                    self._state["active_goal"] = dict(goal)
                    self._state["waypoints"] = []
                # Only an accepted run supersedes the previous stop baseline.
                self._stop_commanded_at = None
                self._stop_encoder_ticks = None
                self._stop_confirmation_after_sequence = None
                self._post_resume_motion_ready = False
                self._start_navigation_progress_locked(
                    progress_pose, progress_started_at, command_baseline,
                )
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
        await self._stop_recorded_before_transition()
        with self._lock:
            self._clear_loop_locked()
            # The preceding leg already completed a physical STOP; no
            # second Nav2 cancel or motor STOP is needed between loop legs.
            if (self._state["active_goal"] is None
                and self._state["navigation_state"] == "READY"
                and self._pending_goal_cancel is None
                and self._stop_confirmed_locked(time.time())):
                self._state["navigation_state"] = "CANCELED"
                self._state["waypoints"] = []
                self._state["planned_path"] = []
                self._last_goal_cancel = {
                    "completed": True, "nav_cancel_confirmed": True,
                    "stop_delivered": True, "physical_stop_confirmed": True,
                }
                self._touch_locked()
                return {**self.state_response(), "stop_delivered": True,
                        "physical_stop_confirmed": True, "cancel_completed": True}
            if (self._pending_goal_cancel is None and self._state["active_goal"] is None
                    and self._last_goal_cancel and self._last_goal_cancel.get("completed")):
                return {**self.state_response(), "stop_delivered": True,
                        "physical_stop_confirmed": True, "cancel_completed": True}
            if self._pending_goal_cancel is not None:
                # A state snapshot may expire the pending cancel before the
                # response is assembled. Never dereference the mutable owner
                # after reading state_response(), and never issue a second STOP.
                pending = self._pending_goal_cancel
                snapshot = self.state_response()
                return {**snapshot, "cancel_completed": False,
                        "stop_delivered": (
                            snapshot.get("cancel_progress") is not None
                            and pending.get("stop_delivered") is True
                        ),
                        "physical_stop_confirmed": False}
            self._pending_nav_success = None
            self._sensor_recovery = None
            self._goal_motion_generation += 1
            self._last_goal_cancel = None
            self._arm_pending_stop_locked()
            # Block the ROS terminal callback from clearing the Goal while
            # cancellation and physical STOP are still in flight.
            self._state["navigation_state"] = "CANCELING"
            # Establish the cancellation owner before any await. Its stop
            # timeout begins only after Nav2 completion and STOP delivery.
            cancel_generation = self._goal_motion_generation
            pending_cancel = {
                "requested_at": time.time(),
                "started_at": None,
                "nav_cancel_requested": False,
                "nav_cancel_confirmed": False,
                "stop_delivered": False,
                "physical_stop_confirmed": False,
            }
            self._pending_goal_cancel = pending_cancel
            self._touch_locked()
        try:
            cancel_result, stop_result = await asyncio.wait_for(
                asyncio.gather(
                    asyncio.to_thread(self._ros.cancel_navigation),
                    self._send_robot_command(
                        self._robot_id, {"type": "stop", "reason": "navigation_goal_cancel"}
                    ),
                    return_exceptions=True,
                ),
                timeout=self.CANCEL_REQUEST_TIMEOUT_SEC,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            error = (
                "NAV_CANCEL_TIMEOUT"
                if isinstance(exc, asyncio.TimeoutError)
                else "NAV_CANCEL_INTERRUPTED"
            )
            with self._lock:
                if self._pending_goal_cancel is pending_cancel:
                    self._pending_goal_cancel = None
                    self._arm_pending_stop_locked()
                    self._queue_cancel_failure_stop_locked("navigation_goal_cancel_unconfirmed")
                    self._state["navigation_state"] = "FAILED"
                    self._state["last_error"] = error
                    self._last_goal_cancel = {
                        "completed": False,
                        "nav_cancel_confirmed": False,
                        "stop_delivered": False,
                        "physical_stop_confirmed": False,
                        "error": error,
                    }
                    self._touch_locked()
            if isinstance(exc, asyncio.TimeoutError):
                raise NavigationControlError(
                    "NAV_CANCEL_TIMEOUT", "Nav2 cancel / Pi STOP was not confirmed in time.", 504,
                ) from exc
            raise
        cancel_error = cancel_result if isinstance(cancel_result, BaseException) else None
        cancel = None if cancel_error is not None else cancel_result
        delivered = self._pi_command_delivered(stop_result)
        with self._lock:
            if (self._pending_goal_cancel is not pending_cancel
                or self._goal_motion_generation != cancel_generation
                or self._state["emergency_stop"]):
                # An E-STOP or a later execution owns this Goal now. Never
                # reset the newer stop proof baseline or terminal state.
                return {**self.state_response(), "cancel": cancel,
                        "stop_delivered": delivered,
                        "physical_stop_confirmed": False,
                        "cancel_completed": False}
            # A newer E-STOP owns the physical stop baseline. Late cancel
            # results never replace its encoder/safety generations.
            if not self._state["emergency_stop"]:
                self._stop_commanded_at = time.time()
                self._stop_encoder_ticks = self._encoder_ticks
                self._stop_confirmation_after_sequence = (
                    (self._encoder_sequence or 0) if delivered else None
                )
                self._stop_confirmation_ticks = None
                self._stop_confirmation_started_at = None
                self._stop_confirmation_last_at = None
                self._stop_confirmation_samples = 0
            terminal = self._cancel_terminal(cancel)
            nav_confirmed = (
                isinstance(cancel, dict)
                and (
                    (cancel.get("confirmed") is True
                     and (cancel.get("completed") is True or cancel.get("requested") is False))
                    # A Nav2 result may win the cancel race. The cancel ACK
                    # is false, but a SUCCEEDED action terminal is authoritative.
                    or (terminal == "SUCCEEDED" and cancel.get("completed") is True)
                )
            )
            if self._state["emergency_stop"]:
                self._state["navigation_state"] = "EMERGENCY_STOPPED"
                self._pending_goal_cancel = None
            elif cancel_error is not None or not nav_confirmed:
                self._state["navigation_state"] = "FAILED"
                self._state["last_error"] = "NAV_CANCEL_FAILED"
                self._pending_goal_cancel = None
            elif not delivered:
                self._state["navigation_state"] = "FAILED"
                self._state["last_error"] = "STOP_NOT_DELIVERED"
                self._pending_goal_cancel = None
            elif terminal == "SUCCEEDED":
                # A successful Nav2 action takes the existing pose/yaw
                # verification flow; it does not complete a user cancel.
                self._pending_goal_cancel = None
                self._state["navigation_state"] = "NAVIGATING"
                self._apply_cancel_terminal_locked("SUCCEEDED")
            elif terminal == "FAILED":
                self._state["navigation_state"] = "FAILED"
                self._state["last_error"] = "NAV_GOAL_FAILED"
                self._pending_goal_cancel = None
            else:
                self._state["navigation_state"] = "CANCELING_STOP"
                self._state["last_error"] = None
                pending_cancel.update({
                    "started_at": time.time(),
                    "nav_cancel_requested": bool(cancel.get("requested")) if isinstance(cancel, dict) else False,
                    "nav_cancel_confirmed": True,
                    "nav_terminal": terminal or "CANCELED",
                    "stop_delivered": True,
                    "physical_stop_confirmed": False,
                })
            self._last_goal_cancel = {
                "completed": False,
                "nav_cancel_confirmed": nav_confirmed,
                "stop_delivered": delivered,
                "physical_stop_confirmed": False,
                "error": self._state["last_error"] if self._state["navigation_state"] == "FAILED" else None,
            }
            self._touch_locked()
        if cancel_error is not None:
            raise cancel_error
        return {
            **self.state_response(), "cancel": cancel,
            "stop_delivered": delivered,
            "physical_stop_confirmed": False,
            "cancel_completed": False,
        }

    async def pause_for_manual(self) -> dict[str, Any]:
        """Stop the active ROS action while retaining the goal for manual arrival."""
        with self._lock:
            self._sensor_recovery = None
            self._arm_pending_stop_locked()
            self._state["navigation_state"] = "PAUSING_FOR_MANUAL"
            self._touch_locked()
        cancel = None
        cancel_error = None
        try:
            cancel = await asyncio.to_thread(self._ros.cancel_navigation)
            terminal = self._cancel_terminal(cancel)
            confirmed = (
                isinstance(cancel, dict)
                and (
                    (cancel.get("confirmed") is True
                     and (cancel.get("completed") is True or cancel.get("requested") is False))
                    or (terminal == "SUCCEEDED" and cancel.get("completed") is True)
                )
            )
            if not confirmed:
                cancel_error = NavigationControlError(
                    "NAV_CANCEL_FAILED", "Nav2 did not confirm the Goal cancellation.", 409
                )
        except Exception as exc:
            cancel_error = exc
        stop_response = await self._send_robot_command(
            self._robot_id,
            {"type": "stop", "reason": "manual_drive_takeover"},
        )
        delivered = self._pi_command_delivered(stop_response)
        with self._lock:
            # A concurrent E-STOP owns the newest physical STOP baseline.
            # A late manual-pause acknowledgment must not replace its proof.
            if not self._state["emergency_stop"]:
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
                elif not delivered:
                    # A missing physical STOP must not present manual
                    # takeover as READY despite a successful ROS cancel.
                    self._state["navigation_state"] = "FAILED"
                    self._state["last_error"] = "STOP_NOT_DELIVERED"
                elif terminal == "SUCCEEDED":
                    # Keep the success STOP/pose proof alive: do not clear it
                    # in _reset_navigation_progress_locked below.
                    self._state["navigation_state"] = "NAVIGATING"
                    self._apply_cancel_terminal_locked(terminal)
                elif terminal == "FAILED":
                    self._state["navigation_state"] = "FAILED"
                    self._state["last_error"] = "NAV_GOAL_FAILED"
                elif self._state["navigation_mode"] == "MAPPING":
                    self._state["navigation_state"] = "IDLE"
                else:
                    self._state["navigation_state"] = "READY"
                if delivered and terminal != "FAILED" and not self._state["emergency_stop"]:
                    self._state["last_error"] = None
                if terminal != "SUCCEEDED" or not delivered or self._state["emergency_stop"]:
                    self._reset_navigation_progress_locked()
            else:
                # Cancel failure cannot restore NAVIGATING: fresh STOP encoder
                # proof alone could then reopen automatic motor authorization.
                if self._state["emergency_stop"]:
                    self._state["navigation_state"] = "EMERGENCY_STOPPED"
                else:
                    self._state["navigation_state"] = "FAILED"
                    self._state["last_error"] = "NAV_CANCEL_FAILED"
                self._reset_navigation_progress_locked()
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
        with self._lock:
            was_loop = bool(self._loop_goals)
            self._clear_loop_locked()
            if was_loop:
                # Loop cancellation is final. Safety release must never
                # silently restart its remaining Nav2 batch.
                self._state["active_goal"] = None
                self._state["waypoints"] = []
                self._state["planned_path"] = []
        normalized_reason = (str(reason).strip() or "dashboard_emergency_stop")[:96]
        try:
            last_route_status = self._ros.navigation_status()
        except Exception:
            last_route_status = {}
        with self._lock:
            # E-STOP supersedes a pending cancel; no later encoder event
            # may silently declare that interrupted operation successful.
            self._pending_goal_cancel = None
            # Capture already-passed intermediate poses before changing the
            # navigation state to EMERGENCY_STOPPED. A later safe resume must
            # not route the robot back to those completed waypoints.
            self._sync_waypoint_progress_locked(last_route_status)
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
        cancel, stop_response = await asyncio.gather(
            asyncio.to_thread(self._ros.cancel_navigation),
            self._send_robot_command(
                self._robot_id,
                {"type": "emergency_stop", "reason": normalized_reason, "automatic": automatic},
            ),
            return_exceptions=True,
        )
        delivered = self._pi_command_delivered(stop_response)
        with self._lock:
            if self._estop_delivery_token == delivery_token:
                self._estop_command_acknowledged = delivered
        # A failed ROS cancel must never suppress or overwrite the Pi E-STOP
        # result; the server remains latched in EMERGENCY_STOPPED either way.
        return {
            **self.state_response(),
            "cancel": None if isinstance(cancel, BaseException) else cancel,
            "cancel_error": str(cancel) if isinstance(cancel, BaseException) else None,
            "stop_delivered": delivered,
            "automatic": automatic,
        }

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
        if not isinstance(prepared, dict) or not self._pi_command_delivered(prepared):
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
            with self._lock:
                release_status_at = self._pi_updated_at
            delivered = await self._send_robot_command(self._robot_id, {
                "type": "resume_navigation",
                "safety_session": safety_session,
                "safety_epoch": safety_epoch,
            })
            if not self._pi_command_delivered(delivered):
                await self.emergency_stop("RESUME_RELEASE_FAILED", automatic=True, force=True)
                raise NavigationControlError("PI_RESUME_FAILED", "The Pi did not accept the resume command.", 409)
            deadline = time.monotonic() + max(1.5, self._watchdog.pi_timeout_sec)
            while True:
                with self._lock:
                    released = (
                        self._pi_updated_at is not None
                        and (release_status_at is None or self._pi_updated_at > release_status_at)
                        and self._pi_estop_latched is False
                    )
                    interrupted = self._estop_generation != estop_generation
                if released or interrupted or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.05)
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
        command_baseline = self._nonzero_command_sequence()
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
                self._start_navigation_progress_locked(
                    progress_pose, progress_started_at, command_baseline,
                )
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

    async def set_speaker_volume(self, payload: dict[str, Any]) -> dict[str, Any]:
        volume = self._bounded_int(payload.get("volume_percent"), 0, 100, "volume_percent")
        with self._lock:
            if not self._connected:
                raise NavigationControlError("PI_OFFLINE", "Pi is not connected.", 409)
        reply = await self._send_robot_command(
            self._robot_id,
            {"type": "set_volume", "volume_percent": volume},
        )
        if not reply or (
            isinstance(reply, dict)
            and (reply.get("ok") is not True or reply.get("speaker_volume_percent") != volume)
        ):
            raise NavigationControlError(
                "VOLUME_NOT_CONFIRMED",
                "Pico W did not confirm the requested speaker volume.",
                409,
            )
        with self._lock:
            self._speaker_volume_percent = volume
            # An in-flight older Pi status must not roll back a new ACK.
            self._volume_override_until = time.time() + 3.0
        return {"ok": True, "volume_percent": volume, "delivered": True}

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
            # A canceled Goal must time out even with no dashboard readers
            # and no further encoder messages from a disconnected Pi.
            self._expire_pending_cancel_locked(current)
            pending_terminal_stop = self._pending_terminal_stop is not None
            hard_fault = bool(self._encoder_stop_violation or self._pi_session_stop_pending)
            recovering = self._sensor_recovery is not None
        if pending_terminal_stop and not hard_fault:
            await self._deliver_terminal_stop()
        with self._lock:
            loop_sensor_recovery = bool(
                recovering and (self._loop_goals or self._loop_recovery)
            )
        if loop_sensor_recovery:
            # Generic sensor recovery would launch a different BT/controller
            # and could bypass the loop-only no-reverse guard. Never resume
            # automatically from a sensor fault during A/B or its backup.
            await self.emergency_stop(
                "LOOP_SENSOR_RECOVERY_BLOCKED", automatic=True, force=True,
            )
            return "LOOP_SENSOR_RECOVERY_BLOCKED"
        if not recovering and not hard_fault and self._loop_recovery is not None:
            await self._advance_loop_recovery()
        if not recovering and not hard_fault and self._loop_pending:
            await self._advance_loop_leg()
        with self._lock:
            cache_monitor_due = (
                not recovering and not hard_fault and self._loop_follow_active
                and self._loop_started and self._loop_stage == "drive"
                and self._state["navigation_state"] == "NAVIGATING"
                and self._pending_nav_success is None
                and time.monotonic() - self._loop_validity_last_at >= 0.5
            )
            if cache_monitor_due:
                self._loop_validity_last_at = time.monotonic()
        if cache_monitor_due:
            await self._monitor_loop_cached_path()
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
                blocked = False
                if (
                    issue is None and not waiting_for_stop
                    and self._state["navigation_state"] in self.NO_PROGRESS_NAV_STATES
                ):
                    waiting_since = self._navigation_command_wait_started_at
                    if waiting_since is not None:
                        sequence = rotation.get("nonzero_command_sequence")
                        baseline = self._navigation_command_baseline
                        if (
                            type(sequence) is int and type(baseline) is int
                            and sequence > baseline
                        ):
                            self._navigation_command_wait_started_at = None
                            self._navigation_command_baseline = None
                            self._navigation_progress_at = current
                        else:
                            blocked = (
                                current - waiting_since
                                >= self.CONTROLLER_COMMAND_START_TIMEOUT_SEC
                            )
                    if self._navigation_command_wait_started_at is None:
                        self._note_rotation_progress_locked(rotation, current)
                        blocked = self._navigation_progress_timed_out_locked(current)
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
                failed = await self._fail_blocked_navigation(current)
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
            delivered = self._pi_command_delivered(stop)
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
                delivered = self._pi_command_delivered(stop)
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
                command_baseline = self._nonzero_command_sequence()
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
                        self._safe_live_pose(), time.time(), command_baseline,
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
                delivered = self._pi_command_delivered(response)
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
                    if self._pending_goal_cancel is not None:
                        self._pending_goal_cancel["stop_delivered"] = True
                        if self._last_goal_cancel is not None:
                            self._last_goal_cancel["stop_delivered"] = True
                    self._pending_terminal_stop = None
                elif pending["attempts"] >= 3:
                    self._pending_goal_cancel = None
                    if self._last_goal_cancel is not None:
                        self._last_goal_cancel["error"] = "STOP_NOT_DELIVERED"
                    self._state["navigation_state"] = "FAILED"
                    self._state["last_error"] = "STOP_NOT_DELIVERED"
                    self._touch_locked()
                return delivered
        finally:
            with self._lock:
                if self._pending_terminal_stop is pending:
                    pending["processing"] = False

    @staticmethod
    def _original_goal_pose_confirmed(
        goal: dict,
        pose: dict | None,
        *,
        now: float | None = None,
        max_age_sec: float = 0.5,
        min_received_at: float | None = None,
    ) -> bool:
        """Prove arrival at the original Goal with a fresh server-received pose.

        The cached pose must have been received *after* the Pi STOP ACK:
        encoder stability alone cannot validate a pose from an old goal.
        """
        if not isinstance(goal, dict) or not isinstance(pose, dict):
            return False
        try:
            gx, gy, gyaw = float(goal["x"]), float(goal["y"]), float(goal["yaw"])
            px, py, pyaw = float(pose["x"]), float(pose["y"]), float(pose["yaw"])
            received_at = float(pose["received_at"])
            current = time.time() if now is None else float(now)
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        if not all(math.isfinite(v) for v in (
            gx, gy, gyaw, px, py, pyaw, received_at, current
        )):
            return False
        if (received_at <= 0.0 or
            received_at > current or
            current - received_at > max_age_sec):
            return False
        if min_received_at is not None and received_at < min_received_at:
            return False
        yaw_error = math.atan2(math.sin(pyaw - gyaw), math.cos(pyaw - gyaw))
        return math.hypot(px - gx, py - gy) <= 0.50 and abs(yaw_error) <= 0.20

    @staticmethod
    def _loop_pose_confirmed(
        target: dict[str, float] | None,
        pose: dict[str, Any] | None, *,
        need_yaw: bool, now: float, after: float | None,
    ) -> bool:
        """Validate fresh physical arrival. XY-only for a leg, yaw for Spin."""
        if not isinstance(target, dict) or not isinstance(pose, dict) or after is None:
            return False
        try:
            x, y, yaw = (float(pose[key]) for key in ("x", "y", "yaw"))
            timestamp = float(pose["received_at"])
            tx, ty, tyaw = (float(target[key]) for key in ("x", "y", "yaw"))
        except (KeyError, ValueError, TypeError, OverflowError):
            return False
        if not all(math.isfinite(v) for v in (x, y, yaw, timestamp, tx, ty, tyaw)):
            return False
        if not 0 <= now - timestamp <= 0.5 or timestamp < after:
            return False
        # Navigation arrival is deliberately relaxed; a stationary Spin
        # must stay close to the actual rotation origin.
        xy_limit = 0.12 if need_yaw else 0.55
        if math.hypot(x - tx, y - ty) > xy_limit:
            return False
        if need_yaw:
            delta = math.atan2(math.sin(yaw - tyaw), math.cos(yaw - tyaw))
            return abs(delta) <= 0.20
        return True

    @staticmethod
    def _loop_signed_turn_confirmed(progress: Any, commanded: float | None) -> bool:
        """Do not confuse a +180 with a -180 turn sharing the same final yaw."""
        if not isinstance(progress, dict) or commanded is None:
            return False
        try:
            signed = float(progress["signed_yaw_rad"])
            age = float(progress["age_sec"])
            requested = float(commanded)
            samples = progress["samples"]
        except (KeyError, ValueError, TypeError, OverflowError):
            return False
        return bool(
            all(math.isfinite(v) for v in (signed, age, requested))
            and type(samples) is int and samples >= 2
            and progress.get("invalid") is False
            and 0 <= age <= 3.0  # bounded STOP/encoder proof follows Spin
            and abs(requested) >= 0.12
            and signed * requested > 0
            and abs(signed - requested) <= max(0.15, min(0.30, abs(requested) * 0.30))
        )

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
                stop_response = await self._send_robot_command(
                    self._robot_id,
                    {"type": "stop", "reason": "navigation_goal_succeeded"},
                )
                delivered = self._pi_command_delivered(stop_response)
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
                # Fetch live localization without holding the state lock.
                # Require it only in live motor mode; dry-run/demo tests
                # have no physically verified odometry or arrival.
                physical_pose = (
                    self._safe_live_pose() if self._motor_output_enabled else None
                )
                with self._lock:
                    if self._pending_nav_success is not pending:
                        return
                    if self._state["emergency_stop"] or self._encoder_stop_violation:
                        return
                    if self._stop_confirmed_locked(time.time()):
                        recovery = self._loop_recovery
                        recovering_backup = bool(recovery and recovery["phase"] == "backing")
                        if recovering_backup and physical_pose:
                            pose_ok = self._loop_recovery_reverse_progress(
                                recovery, physical_pose, now=time.time(),
                                after=self._stop_commanded_at,
                            )
                        elif self._loop_goals and self._loop_started:
                            # Recovery stages override the prior failed
                            # loop Spin; reapproach must not be mistaken for
                            # another rotation just because loop_stage is turn.
                            in_spin = (
                                recovery["phase"] == "turning" if recovery
                                else self._loop_stage == "turn"
                            )
                            pose_ok = self._loop_pose_confirmed(
                                self._state["active_goal"], physical_pose,
                                need_yaw=in_spin, now=time.time(),
                                after=self._stop_commanded_at,
                            )
                        else:
                            pose_ok = self._original_goal_pose_confirmed(
                                self._state["active_goal"], physical_pose,
                                now=time.time(),
                                max_age_sec=min(0.5, self._watchdog.odometry_timeout_sec),
                                min_received_at=self._stop_commanded_at,
                            )
                        if self._motor_output_enabled and pose_ok and (
                            recovery["phase"] == "turning" if recovery
                            else self._loop_stage == "turn"
                        ):
                            try:
                                progress = self._ros.spin_progress()
                            except Exception:
                                progress = None
                            angle = (
                                float(recovery["spin_angle"]) if recovery
                                else self._loop_turn_requested
                            )
                            pose_ok = self._loop_signed_turn_confirmed(progress, angle)
                        if self._motor_output_enabled and not pose_ok:
                            self._clear_loop_locked()
                            self._state["navigation_state"] = "FAILED"
                            self._state["last_error"] = (
                                "LOOP_REVERSE_NOT_VERIFIED" if recovering_backup
                                else "ORIGINAL_GOAL_NOT_REACHED"
                            )
                            self._reset_navigation_progress_locked()
                            self._touch_locked()
                            return
                        if recovery:
                            follow = {
                                "backing": "reapproach_wait_stop",
                                "reapproaching": "spin_wait_stop",
                                "turning": "resume_wait_stop",
                            }
                            next_phase = follow.get(recovery["phase"])
                            if next_phase is None:
                                self._clear_loop_locked()
                                self._state["navigation_state"] = "FAILED"
                                self._state["last_error"] = "LOOP_RECOVERY_STATE_INVALID"
                            else:
                                recovery["phase"] = next_phase
                                self._state["navigation_state"] = "READY"
                        elif (self._loop_goals and self._loop_started
                            and self._robot_mode == "auto"
                            and self._state["navigation_mode"] == "DRIVING"):
                            if self._loop_stage == "drive":
                                if hasattr(self._ros, "finish_loop_trace"):
                                    trace = self._ros.finish_loop_trace()
                                    direction = self._loop_leg_index
                                    if (self._motor_output_enabled
                                        and self._loop_completed_legs >= 1
                                        and direction not in self._loop_path_cache):
                                        certified = self._certify_physical_loop_path(
                                            trace, self._loop_goals[1 - direction],
                                            self._loop_goals[direction],
                                        )
                                        if certified:
                                            self._loop_path_cache[direction] = certified
                                self._loop_active_path = []
                                self._loop_follow_active = False
                                self._loop_failed_follow_retry = False
                                self._loop_completed_legs += 1
                                self._loop_stage = "turn_pending"
                            elif self._loop_stage == "turn":
                                if (self._loop_turn_requested is not None
                                    and abs(abs(self._loop_turn_requested) - math.pi) < 0.05):
                                    self._loop_next_turn_clockwise = not self._loop_next_turn_clockwise
                                self._loop_leg_index = 1 - self._loop_leg_index
                                self._loop_stage = "drive_pending"
                                self._loop_turn_target = None
                                self._loop_turn_requested = None
                            else:
                                self._clear_loop_locked()
                                self._state["navigation_state"] = "FAILED"
                                self._state["last_error"] = "LOOP_STAGE_INVALID"
                            if self._loop_stage is not None:
                                self._loop_pending = True
                                self._loop_pending_since = time.time()
                                self._state["navigation_state"] = "READY"
                        else:
                            self._clear_loop_locked()
                            self._state["navigation_state"] = "SUCCEEDED"
                        self._state["active_goal"] = None
                        self._state["waypoints"] = []
                        self._state["planned_path"] = []
                        if self._state["navigation_state"] != "FAILED":
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

    def _blocked_diagnostic(self, triggered_at: float) -> dict[str, Any]:
        """Capture no-progress evidence before Nav2 cancel clears motion state."""
        try:
            rotation = self._ros.rotation_status() or {}
        except Exception:
            rotation = {}
        try:
            pose = self._get_live_pose()
        except Exception:
            pose = None
        with self._lock:
            baseline = self._navigation_progress_pose
            waiting = self._navigation_command_wait_started_at
            last_progress = self._navigation_progress_at
            diagnostic = {
                "reason": (
                    "controller_start_timeout" if waiting is not None
                    else "rotation_limit" if self._rotation_limit_reached
                    else "no_translation_progress"
                ),
                "recorded_at": triggered_at,
                "seconds_since_progress": (
                    round(max(0.0, triggered_at - last_progress), 3)
                    if last_progress is not None else None
                ),
                "seconds_waiting_for_command": (
                    round(max(0.0, triggered_at - waiting), 3)
                    if waiting is not None else None
                ),
                "baseline_xy": list(baseline) if baseline is not None else None,
                "baseline_odom_xy": (
                    list(self._navigation_progress_odom_pose)
                    if self._navigation_progress_odom_pose is not None else None
                ),
                "xy_displacement_m": None,
                "odom_displacement_m": None,
                "curved_progress_max_linear_mps": self.CURVED_PROGRESS_MAX_LINEAR_MPS,
                "curved_progress_min_measured_mps": self.CURVED_PROGRESS_MIN_ODOM_LINEAR_MPS,
                "pose_age_sec": self._public_age(triggered_at, self._last_pose_at),
                "pi_age_sec": self._public_age(triggered_at, self._pi_updated_at),
                "rotation_extension_sec": round(self._rotation_extension_sec, 3),
                "rotation": {},
            }
        if isinstance(pose, dict) and baseline is not None:
            try:
                x, y = float(pose["x"]), float(pose["y"])
                if math.isfinite(x) and math.isfinite(y):
                    diagnostic["current_xy"] = [round(x, 4), round(y, 4)]
                    diagnostic["xy_displacement_m"] = round(
                        math.hypot(x - baseline[0], y - baseline[1]), 4
                    )
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
        if isinstance(rotation, dict):
            for field in (
                "command_age_sec", "odometry_age_sec", "command_linear_x",
                "command_angular_z", "odom_x", "odom_y", "odom_yaw",
                "odom_linear_x", "odom_angular_z",
                "odom_yaw_delta", "odom_sequence", "nonzero_command_sequence",
            ):
                value = rotation.get(field)
                if type(value) in (int, float) and math.isfinite(float(value)):
                    diagnostic["rotation"][field] = round(float(value), 4)
            baseline_odom = diagnostic["baseline_odom_xy"]
            ox, oy = diagnostic["rotation"].get("odom_x"), diagnostic["rotation"].get("odom_y")
            if baseline_odom is not None and ox is not None and oy is not None:
                diagnostic["odom_displacement_m"] = round(
                    math.hypot(ox - baseline_odom[0], oy - baseline_odom[1]), 4
                )
        return diagnostic

    async def _fail_blocked_navigation(self, triggered_at: float | None = None) -> bool:
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

            diagnostic = self._blocked_diagnostic(
                triggered_at if triggered_at is not None else time.time()
            )
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

            stop_response = await self._send_robot_command(
                self._robot_id,
                {"type": "stop", "reason": "navigation_blocked_timeout"},
            )
            delivered = self._pi_command_delivered(stop_response)
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
                    self._last_blocked_diagnostic = diagnostic
                    self._reset_navigation_progress_locked()
                    self._touch_locked()
                    failed = True

            if failed:
                logging.getLogger(__name__).warning(
                    "BLOCKED_TIMEOUT diagnostic: %s", diagnostic
                )
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
                    self._last_blocked_diagnostic = diagnostic
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

    def _clear_loop_locked(self) -> None:
        self._loop_epoch += 1
        self._loop_goals = []
        self._loop_pending = False
        self._loop_pending_since = None
        self._loop_started = False
        self._loop_leg_index = 0
        self._loop_route_size = 0
        self._loop_route_start_index = 0
        self._loop_refresh_inflight = False
        self._loop_recovery = None
        self._loop_reverse_used = False
        self._loop_stage = None
        self._loop_completed_legs = 0
        self._loop_next_turn_clockwise = True
        self._loop_turn_target = None
        self._loop_turn_requested = None
        self._loop_path_cache.clear()
        self._loop_active_path = []
        self._loop_follow_active = False
        self._loop_failed_follow_retry = False
        self._loop_validity_last_at = 0.0
        self._loop_cache_hits = 0
        self._loop_replans = 0
        if hasattr(self._ros, "discard_loop_trace"):
            self._ros.discard_loop_trace()

    @staticmethod
    def _loop_route_points(
        goals: list[dict[str, float]], first: int, count: int = 12,
    ) -> list[dict[str, float]]:
        """At each A/B turnaround, pivot 180 degrees before driving forward.

        The intermediate pose orientation faces the NEXT waypoint: at A it
        faces B, at B it faces A. This requires a pivot at each turnaround;
        the loop-specific forward-only controller refuses any reverse edge.
        """
        heading_ab = math.atan2(
            goals[1]["y"] - goals[0]["y"],
            goals[1]["x"] - goals[0]["x"],
        )
        heading_ba = math.atan2(
            math.sin(heading_ab + math.pi),
            math.cos(heading_ab + math.pi),
        )
        return [
            {
                "x": goals[(first + index) % 2]["x"],
                "y": goals[(first + index) % 2]["y"],
                "yaw": heading_ab if (first + index) % 2 == 0 else heading_ba,
            }
            for index in range(count)
        ]

    def _arm_loop_pivot_recovery_locked(
        self, ros_nav: dict[str, Any], current: float,
    ) -> bool:
        """Only a recent observed pivot failure permits one guarded backup."""
        if (not self._loop_started or len(self._loop_goals) != 2
            or self._loop_reverse_used or self._state["emergency_stop"]
            or self._robot_mode != "auto" or self._state["navigation_mode"] != "DRIVING"
            or ros_nav.get("action") != "spin"):
            return False
        try:
            rotation = self._ros.rotation_status()
            # Only commands recorded during the current Nav2 Spin can
            # establish a genuine failed pivot. Never use the generic last
            # Twist from an earlier forward or turning action.
            if rotation.get("pivot_command_age_sec") is None:
                return False
            age = float(rotation["pivot_command_age_sec"])
            angular = float(rotation["pivot_command_angular_z"])
            linear = float(rotation["pivot_command_linear_x"])
            pose = self._safe_live_pose()
            px, py, yaw = float(pose["x"]), float(pose["y"]), float(pose["yaw"])
            received = float(pose["received_at"])
        except (AttributeError, KeyError, ValueError, TypeError, OverflowError):
            return False
        if (not all(math.isfinite(x) for x in (age, angular, linear, px, py, yaw, received))
            or not 0 <= age <= 0.75
            or not 0 <= current - received <= 0.5
            or abs(angular) < self._ros.ROTATION_MIN_ANGULAR_RPS
            or abs(linear) > self._ros.ROTATION_MAX_LINEAR_MPS):
            return False
        distances = [
            math.hypot(px - g["x"], py - g["y"]) for g in self._loop_goals
        ]
        at_index = 0 if distances[0] <= distances[1] else 1
        if distances[at_index] > 0.35:
            return False
        # Reverse only along the known inbound track; if an attempted
        # pivot has already changed heading substantially, abort instead.
        other = self._loop_goals[1 - at_index]
        reached = self._loop_goals[at_index]
        inbound_yaw = math.atan2(reached["y"] - other["y"], reached["x"] - other["x"])
        err = math.atan2(math.sin(yaw - inbound_yaw), math.cos(yaw - inbound_yaw))
        if abs(err) > 0.35:
            return False
        self._loop_reverse_used = True
        self._loop_recovery = {
            "phase": "backup_wait_stop",
            "epoch": self._loop_epoch,
            "goal_index": at_index,
            "goal": {"x": reached["x"], "y": reached["y"], "yaw": inbound_yaw},
            "failed_pose": {"x": px, "y": py, "yaw": yaw},
            "spin_angle": float(self._loop_turn_requested or (
                math.pi if angular > 0 else -math.pi
            )),
            "started_at": current,
        }
        return True

    @staticmethod
    def _loop_recovery_reverse_progress(
        recovery: dict[str, Any], pose: dict, *,
        now: float | None = None, after: float | None = None,
    ) -> bool:
        """Prove fresh, post-STOP odometry moved backward at least 0.15m."""
        try:
            before = recovery["failed_pose"]
            heading = float(before["yaw"])
            dx = float(pose["x"]) - float(before["x"])
            dy = float(pose["y"]) - float(before["y"])
            received = float(pose["received_at"])
            current = time.time() if now is None else float(now)
            return (
                after is not None
                and all(math.isfinite(x) for x in (
                    heading, dx, dy, received, current, float(after)
                ))
                and 0 <= current - received <= 0.5
                and received >= float(after)
                and dx * math.cos(heading) + dy * math.sin(heading) <= -0.15
            )
        except (KeyError, ValueError, TypeError, OverflowError):
            return False

    async def _advance_loop_recovery(self) -> None:
        """Once-only: stop -> guarded short backup -> same Goal -> spin -> loop.

        Every stage waits for fresh Pi STOP ACK and stationary encoder proof.
        No direct manual MOVE command can bypass the Nav2 collision guards.
        """
        async with self._operation_lock:
            with self._lock:
                recovery = self._loop_recovery
                if recovery is None:
                    return
                phase = recovery["phase"]
                if phase not in {
                    "backup_wait_stop", "reapproach_wait_stop",
                    "spin_wait_stop", "resume_wait_stop",
                }:
                    return
                now = time.time()
                if now - float(recovery.get("started_at", now)) > 60.0:
                    # Abandoned or repeatedly blocked recovery may not sit
                    # indefinitely waiting for physical motion clearance.
                    self._clear_loop_locked()
                    self._arm_pending_stop_locked()
                    self._pending_terminal_stop = {
                        "reason": "loop_recovery_timeout",
                        "attempts": 0, "last_attempt_at": None,
                        "processing": False,
                    }
                    self._state["navigation_state"] = "FAILED"
                    self._state["last_error"] = "LOOP_RECOVERY_TIMEOUT"
                    self._touch_locked()
                    return
                allowed = (
                    recovery["epoch"] == self._loop_epoch
                    and self._loop_started and self._loop_goals
                    and not self._state["emergency_stop"]
                    and self._state["navigation_mode"] == "DRIVING"
                    and self._robot_mode == "auto"
                    and self._pi_navigation_mode == "DRIVING"
                    and self._pi_estop_latched is False
                    and not self._encoder_stop_violation
                    and self._stop_confirmed_locked(now)
                    and self._motion_allowed_locked(now)
                    and self._state["navigation_state"] in {
                        "LOOP_RECOVERY_STOPPING", "READY",
                    }
                    and self._pending_terminal_stop is None
                    and self._pending_goal_cancel is None
                    and self._watchdog_issue_locked(now) is None
                )
                if not allowed:
                    # Physical STOP may still be pending. Never take over
                    # until the interlock is verified.
                    return
                epoch = self._loop_epoch
                if phase == "resume_wait_stop":
                    # Recovered Spin already completed its physical STOP and
                    # yaw confirmation. Now schedule exactly one forward leg.
                    if abs(abs(float(recovery["spin_angle"])) - math.pi) < 0.05:
                        self._loop_next_turn_clockwise = not self._loop_next_turn_clockwise
                    self._loop_leg_index = 1 - int(recovery["goal_index"])
                    self._loop_stage = "drive_pending"
                    self._loop_pending = True
                    self._loop_pending_since = time.time()
                    self._loop_recovery = None
                    self._loop_turn_target = None
                    self._loop_turn_requested = None
                    self._state["navigation_state"] = "READY"
                    self._touch_locked()
                    return
                recovery["phase"] = "launching"
                self._state["navigation_state"] = "LOOP_RECOVERY_STARTING"
                self._touch_locked()
                target = dict(recovery["goal"])
                stage_goal: dict[str, float] | None = None

            command_baseline = self._nonzero_command_sequence()
            try:
                if phase == "backup_wait_stop":
                    source = recovery["failed_pose"]
                    stage_goal = {
                        "x": source["x"] - 0.35 * math.cos(source["yaw"]),
                        "y": source["y"] - 0.35 * math.sin(source["yaw"]),
                        "yaw": source["yaw"],
                    }
                    # Nav2 BackUp checks its footprint and the existing
                    # rear sector guard independently limits unseen reverse.
                    action = await asyncio.to_thread(
                        self._ros.reverse_loop_recovery, 0.35,
                    )
                    next_phase = "backing"
                elif phase == "reapproach_wait_stop":
                    self._validate_goal_in_active_map(target)
                    action = await asyncio.to_thread(
                        self._ros.navigate_to_pose, target, loop_reapproach=True,
                    )
                    stage_goal = target
                    next_phase = "reapproaching"
                elif phase == "spin_wait_stop":
                    latest = self._safe_live_pose()
                    if latest is None:
                        raise NavigationControlError(
                            "LOOP_POSE_MISSING", "Fresh pose required before spin.",
                        )
                    spin_yaw = float(recovery["spin_angle"])
                    stage_goal = {
                        "x": float(latest["x"]),
                        "y": float(latest["y"]),
                        "yaw": math.atan2(
                            math.sin(float(latest["yaw"]) + spin_yaw),
                            math.cos(float(latest["yaw"]) + spin_yaw),
                        ),
                    }
                    if (not all(math.isfinite(z) for z in stage_goal.values())
                        or not 0 <= time.time() - float(latest["received_at"]) <= 0.5):
                        raise NavigationControlError(
                            "LOOP_POSE_STALE", "Cannot certify turn origin.",
                        )
                    action = await asyncio.to_thread(
                        self._ros.spin_loop_turn, spin_yaw,
                    )
                    next_phase = "turning"
                else:
                    raise NavigationControlError(
                        "LOOP_RECOVERY_STAGE_INVALID", "Unexpected recovery stage.", 409,
                    )
                if not isinstance(action, dict) or action.get("accepted") is not True:
                    raise NavigationControlError(
                        "LOOP_RECOVERY_REJECTED", "Nav2 rejected the recovery action.",
                    )
            except Exception:
                with self._lock:
                    if self._loop_recovery is recovery and epoch == self._loop_epoch:
                        self._clear_loop_locked()
                        self._arm_pending_stop_locked()
                        self._pending_terminal_stop = {
                            "reason": "loop_recovery_failed",
                            "attempts": 0, "last_attempt_at": None,
                            "processing": False,
                        }
                        self._state["navigation_state"] = "FAILED"
                        self._state["last_error"] = "LOOP_RECOVERY_FAILED"
                        self._touch_locked()
                return

            with self._lock:
                interrupted = (
                    self._loop_recovery is not recovery
                    or epoch != self._loop_epoch
                    or self._state["emergency_stop"]
                    or self._robot_mode != "auto"
                    or self._state["navigation_mode"] != "DRIVING"
                )
                if not interrupted:
                    self._state["active_goal"] = stage_goal
                    self._state["navigation_state"] = "NAVIGATING"
                    self._state["last_error"] = None
                    self._stop_commanded_at = None
                    self._stop_encoder_ticks = None
                    self._stop_confirmation_after_sequence = None
                    self._post_resume_motion_ready = False
                    recovery["phase"] = next_phase
                    self._start_navigation_progress_locked(
                        self._safe_live_pose(), time.time(), command_baseline,
                    )
                    self._touch_locked()
            if interrupted:
                # A user cancellation may have landed during an async Nav2
                # action acceptance. Do not leave that action running.
                await asyncio.to_thread(self._ros.cancel_navigation)

    @staticmethod
    def _certify_physical_loop_path(
        trace: Any, origin: dict[str, float], destination: dict[str, float],
    ) -> list[dict[str, float]]:
        """Only proven physical AMCL samples may enter the reuse cache."""
        if not isinstance(trace, list) or not 4 <= len(trace) <= 1500:
            return []
        try:
            path = [
                {k: float(p[k]) for k in ("x", "y", "yaw")}
                for p in trace
            ]
        except (KeyError, TypeError, ValueError, OverflowError):
            return []
        if not all(math.isfinite(v) for p in path for v in p.values()):
            return []
        if (math.hypot(path[0]["x"] - origin["x"],
                       path[0]["y"] - origin["y"]) > 0.55
            or math.hypot(path[-1]["x"] - destination["x"],
                          path[-1]["y"] - destination["y"]) > 0.55):
            return []
        # The C++ loop controller classifies a moving edge as reverse
        # when its longitudinal projection is < -0.003m. Use the SAME
        # criterion. Skip only very short AMCL backstep noise, never
        # replace genuine reverse motion with an invented forward edge.
        cleaned = [path[0]]
        travelled = 0.0
        for pose in path[1:]:
            previous = cleaned[-1]
            dx = pose["x"] - previous["x"]
            dy = pose["y"] - previous["y"]
            distance = math.hypot(dx, dy)
            if distance > 0.45:
                return []
            projection = dx * math.cos(previous["yaw"]) + dy * math.sin(previous["yaw"])
            if distance >= 0.003 and projection < -0.003:
                if distance <= 0.06:
                    continue  # backwards localization noise, not a motor leg
                return []
            travelled += distance
            cleaned.append(pose)
        if len(cleaned) < 4 or travelled < 0.50:
            return []
        if math.hypot(cleaned[-1]["x"] - destination["x"],
                      cleaned[-1]["y"] - destination["y"]) > 0.40:
            return []
        return cleaned

    @staticmethod
    def _fresh_loop_pose(pose: Any, *, now: float | None = None) -> bool:
        """Refuse stale/future AMCL samples when validating a cached route."""
        if not isinstance(pose, dict):
            return False
        try:
            current = time.time() if now is None else float(now)
            values = [float(pose[key]) for key in ("x", "y", "yaw", "received_at")]
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        return (
            math.isfinite(current)
            and all(math.isfinite(value) for value in values)
            and 0 <= current - values[3] <= 0.5
        )

    @staticmethod
    def _remaining_loop_path(
        path: list[dict[str, float]], pose: dict[str, float],
    ) -> list[dict[str, float]]:
        if len(path) < 2:
            return []
        # Trim historical/behind-the-robot points: an obstacle appearing
        # behind the vehicle must not invalidate the route already traversed.
        index = min(
            range(len(path)),
            key=lambda i: (path[i]["x"] - pose["x"]) ** 2
                          + (path[i]["y"] - pose["y"]) ** 2,
        )
        if math.hypot(path[index]["x"] - pose["x"],
                      path[index]["y"] - pose["y"]) > 0.55:
            return []
        return [dict(p) for p in path[max(index - 1, 0):]]

    async def _monitor_loop_cached_path(self) -> None:
        """Reuse valid physical route, replan only after costmap rejection.

        Read-only costmap validation and planning never hold the operation
        lock, so authenticated Cancel / mode switches can invalidate the
        session immediately. Only the final Nav2 goal handoff is serialized.
        """
        with self._lock:
            if (not self._loop_follow_active or not self._loop_started
                or self._loop_stage != "drive"
                or self._state["navigation_state"] != "NAVIGATING"
                or self._state["emergency_stop"]
                or self._pending_nav_success is not None):
                return
            epoch = self._loop_epoch
            path = [dict(p) for p in self._loop_active_path]
            goal = dict(self._state["active_goal"] or {})
            index = self._loop_leg_index
        try:
            pose = self._safe_live_pose()
            if not self._fresh_loop_pose(pose):
                raise NavigationControlError(
                    "LOOP_CACHE_POSE_STALE", "No fresh pose for cache validation.", 409,
                )
            remainder = self._remaining_loop_path(path, pose)
            if len(remainder) < 2:
                if not remainder:
                    raise NavigationControlError(
                        "LOOP_CACHE_OFF_ROUTE", "Robot diverged from saved path.", 409,
                    )
                return
            if await asyncio.to_thread(self._ros.loop_path_valid, remainder):
                return

            with self._lock:
                if (epoch != self._loop_epoch or not self._loop_follow_active
                    or self._state["emergency_stop"]
                    or self._state["navigation_state"] != "NAVIGATING"
                    or self._pending_nav_success is not None):
                    return
                self._loop_path_cache.pop(index, None)
                self._loop_replans += 1

            # This planner call can take seconds; NEVER hold the operation
            # lock here. The existing local controller still checks current
            # obstacles, and the API's epoch revokes canceled computations.
            fresh = self._normalized_path(
                await asyncio.to_thread(
                    self._compute_path_for_route, goal, [], loop_forward=True,
                )
            )
            if len(fresh) < 2 or not await asyncio.to_thread(
                self._ros.loop_path_valid, fresh,
            ):
                raise NavigationControlError(
                    "LOOP_REPLAN_UNSAFE", "Obstacle blocks the new path.", 422,
                )

            interrupted = False
            async with self._operation_lock:
                with self._lock:
                    if (epoch != self._loop_epoch or not self._loop_follow_active
                        or self._state["emergency_stop"]
                        or self._state["navigation_state"] != "NAVIGATING"
                        or self._pending_nav_success is not None
                        or self._robot_mode != "auto"
                        or self._state["navigation_mode"] != "DRIVING"):
                        return
                action = await asyncio.to_thread(
                    self._ros.follow_loop_path, fresh, preempt=True,
                )
                if not isinstance(action, dict) or action.get("accepted") is not True:
                    raise NavigationControlError(
                        "LOOP_REPLAN_REJECTED", "FollowPath rejected rerouting.", 409,
                    )
                with self._lock:
                    interrupted = (
                        epoch != self._loop_epoch or self._state["emergency_stop"]
                        or self._robot_mode != "auto"
                        or self._state["navigation_mode"] != "DRIVING"
                    )
                    if not interrupted and self._loop_stage == "drive":
                        self._loop_active_path = [dict(p) for p in fresh]
                        self._state["planned_path"] = fresh
                        self._touch_locked()
            # Never await E-STOP while holding the mutation operation lock.
            if interrupted:
                await self.emergency_stop(
                    "LOOP_CACHE_PREEMPT_INTERRUPTED", automatic=True, force=True,
                )
        except Exception:
            with self._lock:
                should_stop = (
                    epoch == self._loop_epoch and self._loop_started
                    and not self._state["emergency_stop"]
                )
            if should_stop:
                await self.emergency_stop(
                    "LOOP_CACHE_VALIDATION_FAILED", automatic=True, force=True,
                )

    async def _advance_loop_leg(self) -> None:
        """Advance only after the last action physically stopped.

        Each A/B is a terminal single-leg Goal. Its arrival always requires
        fresh Pi STOP ACK + stationary encoder samples. A distinct Nav2 Spin
        must then finish before the next forward-only navigation action.
        """
        async with self._operation_lock:
            with self._lock:
                if not self._loop_pending or len(self._loop_goals) != 2:
                    return
                phase = self._loop_stage
                epoch = self._loop_epoch
                now = time.time()
                if self._loop_pending_since is None:
                    self._loop_pending_since = now
                if now - self._loop_pending_since > self.LOOP_STAGE_PENDING_TIMEOUT_SEC:
                    self._clear_loop_locked()
                    self._arm_pending_stop_locked()
                    self._pending_terminal_stop = {
                        "reason": "loop_stage_wait_timeout", "attempts": 0,
                        "last_attempt_at": None, "processing": False,
                    }
                    self._state["active_goal"] = None
                    self._state["waypoints"] = []
                    self._state["planned_path"] = []
                    self._state["navigation_state"] = "FAILED"
                    self._state["last_error"] = "LOOP_STAGE_WAIT_TIMEOUT"
                    self._touch_locked()
                    return
                allowed = (
                    phase in {"turn_pending", "drive_pending"}
                    and self._loop_started
                    and self._state["navigation_state"] == "READY"
                    and self._state["navigation_mode"] == "DRIVING"
                    and self._robot_mode == "auto"
                    and self._pi_navigation_mode == "DRIVING"
                    and self._pi_estop_latched is False
                    and not self._state["emergency_stop"]
                    and self._pending_goal_cancel is None
                    and self._pending_terminal_stop is None
                    and self._stop_confirmed_locked(now)
                    and self._motion_allowed_locked(now)
                    and self._watchdog_issue_locked(now) is None
                )
                if not allowed:
                    # A stop or sensor handshake can still be in flight.
                    return
                index = self._loop_leg_index
                goals = [dict(p) for p in self._loop_goals]
                half_turn = self._loop_completed_legs >= 2
                clockwise = self._loop_next_turn_clockwise
                self._loop_pending = False
                self._loop_pending_since = None
                self._state["navigation_state"] = "LOOP_STAGE_STARTING"
                self._touch_locked()
        action_accepted = False
        try:
            self._assert_watchdogs_healthy()
            if phase == "turn_pending":
                pose = self._safe_live_pose()
                now = time.time()
                if not isinstance(pose, dict) or not 0 <= now - float(
                    pose["received_at"]
                ) <= 0.5:
                    raise NavigationControlError(
                        "LOOP_POSE_STALE", "Cannot verify 180-degree turn origin.", 409,
                    )
                # A first arrival may come from any direction: align to
                # A->B; every subsequent B/A turnaround turns 180 deg.
                heading = math.atan2(
                    goals[1]["y"] - goals[0]["y"],
                    goals[1]["x"] - goals[0]["x"],
                )
                if half_turn:
                    angle = -math.pi if clockwise else math.pi
                else:
                    desired = heading if index == 0 else heading + math.pi
                    angle = math.atan2(
                        math.sin(desired - float(pose["yaw"])),
                        math.cos(desired - float(pose["yaw"])),
                    )
                target = {
                    "x": float(pose["x"]), "y": float(pose["y"]),
                    "yaw": math.atan2(
                        math.sin(float(pose["yaw"]) + angle),
                        math.cos(float(pose["yaw"]) + angle),
                    ),
                }
                if abs(angle) < 0.12 and not half_turn:
                    # Already aligned at the initial A. No 180 turn is
                    # skipped: the first arrival is not yet a turnaround.
                    with self._lock:
                        if epoch != self._loop_epoch or not self._loop_started:
                            return
                        self._loop_leg_index = 1 - index
                        self._loop_stage = "drive_pending"
                        self._loop_pending = True
                        self._loop_pending_since = time.time()
                        self._state["navigation_state"] = "READY"
                        self._touch_locked()
                    return
                next_phase = "turn"
                plan = []
            else:
                next_index = index  # Index was flipped after the turn.
                previous = goals[1 - next_index]
                destination = goals[next_index]
                heading = math.atan2(
                    destination["y"] - previous["y"],
                    destination["x"] - previous["x"],
                )
                target = {
                    "x": destination["x"], "y": destination["y"], "yaw": heading,
                }
                self._validate_goal_in_active_map(target)
                with self._lock:
                    cached = [dict(p) for p in self._loop_path_cache.get(next_index, [])]
                reuse = False
                # Even first-time planning requires current localization.
                # Reject both cached replay and new planning on a stale pose.
                current_pose = self._safe_live_pose()
                if not self._fresh_loop_pose(current_pose):
                    raise NavigationControlError(
                        "LOOP_POSE_STALE",
                        "Cannot start a loop leg without fresh localization.",
                        409,
                    )
                if cached and hasattr(self._ros, "loop_path_valid"):
                    # A small start-pose mismatch is not an obstruction.
                    # Preserve the learned trajectory for subsequent laps
                    # instead of deleting it due to temporary alignment.
                    aligned = (
                        self._fresh_loop_pose(current_pose)
                        and math.hypot(
                            current_pose["x"] - cached[0]["x"],
                            current_pose["y"] - cached[0]["y"],
                        ) <= 0.35
                        and abs(math.atan2(
                            math.sin(current_pose["yaw"] - cached[0]["yaw"]),
                            math.cos(current_pose["yaw"] - cached[0]["yaw"]),
                        )) <= 0.35
                    )
                    if aligned:
                        reuse = await asyncio.to_thread(
                            self._ros.loop_path_valid, cached,
                        )
                        if not reuse:
                            # A confirmed dynamic obstacle invalidates
                            # this direction's physical trajectory.
                            with self._lock:
                                if epoch == self._loop_epoch:
                                    self._loop_path_cache.pop(next_index, None)
                                    self._loop_replans += 1
                if reuse:
                    plan = cached
                else:
                    # First physical passage, unsafe cache, or unusable
                    # start alignment: compute via existing Nav2 planner.
                    plan = self._normalized_path(
                        await asyncio.to_thread(
                            self._compute_path_for_route, target, [], loop_forward=True,
                        )
                    )
                if len(plan) < 2:
                    raise NavigationControlError(
                        "LOOP_PATH_NOT_FOUND", "No safe forward loop leg.", 422,
                    )
                with self._lock:
                    if epoch != self._loop_epoch or not self._loop_started:
                        return
                next_phase = "drive"
            # Slow planning/validity calls above MUST NOT own the mutation lock.
            # Recheck the loop epoch before handing a new action to Nav2.
            async with self._operation_lock:
                with self._lock:
                    if epoch != self._loop_epoch or not self._loop_started:
                        return
                    now = time.time()
                    ready = (
                        self._loop_stage == phase
                        and self._state["navigation_state"] == "LOOP_STAGE_STARTING"
                        and self._state["navigation_mode"] == "DRIVING"
                        and self._robot_mode == "auto"
                        and self._pi_navigation_mode == "DRIVING"
                        and self._pi_estop_latched is False
                        and not self._state["emergency_stop"]
                        and self._pending_goal_cancel is None
                        and self._pending_terminal_stop is None
                        and self._stop_confirmed_locked(now)
                        and self._motion_allowed_locked(now)
                    )
                    if not ready:
                        raise NavigationControlError(
                            "LOOP_STAGE_REVOKED", "Loop stage no longer safe to start.", 409,
                        )
                self._assert_watchdogs_healthy()
                command_baseline = self._nonzero_command_sequence()
                if phase == "turn_pending":
                    action = await asyncio.to_thread(self._ros.spin_loop_turn, angle)
                else:
                    if hasattr(self._ros, "begin_loop_trace"):
                        await asyncio.to_thread(self._ros.begin_loop_trace)
                    if reuse:
                        action = await asyncio.to_thread(
                            self._ros.follow_loop_path, plan,
                        )
                    else:
                        action = await asyncio.to_thread(
                            self._ros.navigate_to_pose, target, loop_reapproach=True,
                        )
                if not isinstance(action, dict) or action.get("accepted") is not True:
                    raise NavigationControlError(
                        "LOOP_ACTION_REJECTED", "Nav2 rejected a loop action.", 422,
                    )
                action_accepted = True
                with self._lock:
                    interrupted = (
                        epoch != self._loop_epoch or self._state["emergency_stop"]
                        or self._robot_mode != "auto"
                        or self._state["navigation_mode"] != "DRIVING"
                    )
                    if not interrupted:
                        self._loop_stage = next_phase
                        self._state["active_goal"] = target
                        self._state["waypoints"] = []
                        self._state["planned_path"] = plan
                        if phase == "drive_pending":
                            self._loop_follow_active = bool(reuse)
                            self._loop_active_path = [dict(p) for p in plan] if reuse else []
                            self._loop_validity_last_at = 0.0
                            if reuse:
                                self._loop_cache_hits += 1
                        self._state["navigation_state"] = "NAVIGATING"
                        self._state["last_error"] = None
                        self._stop_commanded_at = None
                        self._stop_encoder_ticks = None
                        self._stop_confirmation_after_sequence = None
                        self._post_resume_motion_ready = False
                        if phase == "turn_pending":
                            self._loop_turn_target = dict(target)
                            self._loop_turn_requested = angle
                        self._start_navigation_progress_locked(
                            self._safe_live_pose(), time.time(), command_baseline,
                        )
                        self._touch_locked()
                if interrupted and action_accepted:
                    # Cancel accepted action if user canceled during ROS handshake.
                    await asyncio.to_thread(self._ros.cancel_navigation)

        except Exception:
            with self._lock:
                if epoch == self._loop_epoch and self._loop_goals:
                    self._clear_loop_locked()
                    self._arm_pending_stop_locked()
                    self._pending_terminal_stop = {
                        "reason": "loop_stage_failed",
                        "attempts": 0, "last_attempt_at": None,
                        "processing": False,
                    }
                    self._state["navigation_state"] = "FAILED"
                    self._state["last_error"] = "LOOP_STAGE_FAILED"
                    self._touch_locked()
            return
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
        self._navigation_progress_odom_pose = None
        self._navigation_progress_at = None
        self._navigation_command_baseline = None
        self._navigation_command_wait_started_at = None
        self._rotation_extension_sec = 0.0
        self._rotation_last_odom_sequence = None
        self._curve_last_odom_sequence = None
        self._rotation_last_verified_yaw = None
        self._rotation_limit_reached = False

    def _nonzero_command_sequence(self) -> int | None:
        try:
            sequence = self._ros.rotation_status().get("nonzero_command_sequence")
        except Exception:
            return None
        return sequence if type(sequence) is int and sequence >= 0 else None

    def _start_navigation_progress_locked(
        self,
        payload: dict[str, Any] | None,
        received_at: float,
        nonzero_command_baseline: int | None = None,
    ) -> None:
        self._goal_motion_generation += 1
        self._reset_navigation_progress_locked()
        self._navigation_progress_at = received_at
        if type(nonzero_command_baseline) is int:
            self._navigation_command_baseline = nonzero_command_baseline
            self._navigation_command_wait_started_at = received_at
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
        *,
        rotation: dict[str, Any] | None = None,
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

        displacement = math.hypot(x - previous[0], y - previous[1])
        # Initialize an independent odom-frame anchor. Map->odom can jump
        # during AMCL corrections; map XY alone must not certify motion.
        odom_position = None
        if isinstance(rotation, dict):
            try:
                ox = float(rotation["odom_x"])
                oy = float(rotation["odom_y"])
                odom_age = float(rotation["odometry_age_sec"])
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
            else:
                if (
                    all(math.isfinite(v) for v in (ox, oy, odom_age))
                    and 0 <= odom_age <= self.ROTATION_DATA_MAX_AGE_SEC
                ):
                    odom_position = (ox, oy)
        if odom_position is not None and self._navigation_progress_odom_pose is None:
            self._navigation_progress_odom_pose = odom_position

        moved = displacement >= self.PROGRESS_DISTANCE_M
        if (
            not moved and displacement >= self.CURVED_PROGRESS_DISTANCE_M
            and odom_position is not None
            and self._navigation_progress_odom_pose is not None
            and isinstance(rotation, dict)
        ):
            # A genuine slow curve must move both MAP and ODOM poses.
            # The net displacement since the last watchdog checkpoint may
            # include a PRIOR reverse leg, pivot or turn. Compare the current
            # command with fresh, instantaneous wheel odom TWIST, NOT the
            # full historical displacement projected onto the last yaw.
            # AMCL correction, a stalled motor, and reversed wheel motion
            # never reset the watchdog.
            try:
                linear = float(rotation["command_linear_x"])
                angular = float(rotation["command_angular_z"])
                command_age = float(rotation["command_age_sec"])
                yaw_delta = float(rotation["odom_yaw_delta"])
                measured_linear = float(rotation["odom_linear_x"])
                measured_angular = float(rotation["odom_angular_z"])
                odom_sequence = rotation["odom_sequence"]
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
            else:
                odom_dx = odom_position[0] - self._navigation_progress_odom_pose[0]
                odom_dy = odom_position[1] - self._navigation_progress_odom_pose[1]
                odom_distance = math.hypot(odom_dx, odom_dy)
                moved = (
                    all(math.isfinite(v) for v in (
                        linear, angular, command_age, yaw_delta,
                        odom_distance, measured_linear, measured_angular
                    ))
                    and type(odom_sequence) is int and odom_sequence >= 0
                    and 0 <= command_age <= self.ROTATION_DATA_MAX_AGE_SEC
                    and self.CURVED_PROGRESS_DISTANCE_M <= odom_distance
                    and self._ros.ROTATION_MAX_LINEAR_MPS < abs(linear) <=
                        self.CURVED_PROGRESS_MAX_LINEAR_MPS
                    and abs(angular) >= self.CURVED_PROGRESS_MIN_ANGULAR_RPS
                    and abs(yaw_delta) >= self.ROTATION_MIN_YAW_DELTA_RAD
                    and angular * yaw_delta > 0
                    and abs(measured_linear) >= self.CURVED_PROGRESS_MIN_ODOM_LINEAR_MPS
                    and linear * measured_linear > 0
                    and angular * measured_angular > 0
                    and odom_sequence != self._curve_last_odom_sequence
                )
                if moved:
                    self._curve_last_odom_sequence = odom_sequence
        if moved:
            if odom_position is not None:
                self._navigation_progress_odom_pose = odom_position
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
        self, final_goal: dict[str, float], waypoints: list[dict[str, float]],
        *, loop_forward: bool = False,
    ) -> Any:
        if waypoints:
            if loop_forward:
                raise NavigationControlError(
                    "INVALID_LOOP", "Loop legs require one forward goal.", 400,
                )
            return self._ros.compute_path_through_poses([*waypoints, final_goal])
        if loop_forward:
            return self._ros.compute_path(final_goal, loop_forward=True)
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
            # A manual arrival at the final XY must not skip ordered
            # intermediate waypoints that are still pending.
            or bool(self._state.get("waypoints"))
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

        @self._app.post("/api/navigation/control/patrol-mode")
        async def set_patrol_mode(request: Request):
            return await self._mutation(
                request, self.switch_patrol_mode, abort_loop_for_mode="manual",
            )

        @self._app.post("/api/navigation/control/mode")
        async def set_navigation_control_mode(request: Request):
            return await self._mutation(
                request, self.switch_mode, include_user=True,
                abort_loop_for_mode="mapping",
            )

        @self._app.post("/api/navigation/control/goal")
        async def set_navigation_goal(request: Request):
            return await self._mutation(request, self.plan_goal)

        @self._app.post("/api/navigation/control/start")
        async def start_navigation(request: Request):
            return await self._mutation(request, lambda _: self.start_navigation())

        @self._app.post("/api/navigation/control/cancel")
        async def cancel_navigation(request: Request):
            return await self._mutation(
                request, lambda _: self.cancel_goal(), abort_loop=True,
            )

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

        @self._app.post("/api/navigation/control/speaker-volume")
        async def update_speaker_volume(request: Request):
            return await self._mutation(request, self.set_speaker_volume)

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
        abort_loop: bool = False,
        abort_loop_for_mode: str | None = None,
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
        # Revoke background loop ownership as soon as an authenticated
        # operator STOP/Manual/Mapping request arrives; do not wait behind
        # a slow planner or rolling Nav2 goal preemption.
        abort_requested = (abort_loop or (abort_loop_for_mode is not None
            and str(payload.get("mode", "")).strip().lower() == abort_loop_for_mode))
        loop_was_active = False
        urgent_loop_stop = False
        if abort_requested:
            with self._lock:
                loop_was_active = bool(self._loop_goals)
                # Normally cancel_goal owns one STOP command. Only if a
                # running route update already owns the operation lock do we
                # front-load an emergency motor STOP before waiting on it.
                urgent_loop_stop = bool(
                    loop_was_active and self._operation_lock.locked()
                    and self._state["navigation_state"] in {
                        "NAVIGATING", "LOOP_STAGE_STARTING", "LOOP_RECOVERY_STARTING",
                    }
                )
                self._clear_loop_locked()
                if urgent_loop_stop:
                    self._arm_pending_stop_locked()
        if urgent_loop_stop:
            try:
                # Do not wait for the serialized Nav2 mutation to STOP the Pi:
                # it may still be waiting on FollowPath goal acceptance.
                await self._send_robot_command(
                    self._robot_id,
                    {"type": "stop", "reason": "operator_loop_abort_during_preemption"},
                )
            except Exception:
                # The normal Cancel/Manual/Mapping path still sends its own
                # STOP and handles physical acknowledgement/failure.
                pass
        try:
            async def invoke():
                if include_user:
                    user = request.session.get("user") or {}
                    identity = str(user.get("user_id") or user.get("email") or "unknown")
                    result = await action(payload, user=identity)
                else:
                    result = await action(payload)
                # Ordinary Goal pause retention is unchanged. A canceled
                # loop must NOT become a resumable single Goal after Manual.
                if abort_loop_for_mode == "manual" and loop_was_active:
                    with self._lock:
                        if (self._robot_mode == "manual"
                            and not self._state["emergency_stop"]):
                            self._state["active_goal"] = None
                            self._state["waypoints"] = []
                            self._state["planned_path"] = []
                            self._state["navigation_state"] = (
                                "READY" if self._state["navigation_mode"] == "DRIVING"
                                else "IDLE"
                            )
                            self._touch_locked()
                    return self.state_response()
                return result

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
