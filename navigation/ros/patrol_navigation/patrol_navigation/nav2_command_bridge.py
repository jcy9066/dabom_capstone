from __future__ import annotations

import json
import math
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import rclpy
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from sensor_msgs.msg import LaserScan
from rclpy.qos import qos_profile_sensor_data
from rclpy.node import Node
from rclpy.time import Time
from tf2_ros import Buffer, TransformException, TransformListener
from patrol_navigation.env_config import env_bool, env_float, env_text
from patrol_navigation.rear_reverse_guard import RearReverseGuard
from patrol_navigation.nav2_twist_guard import valid_differential_twist


class Nav2CommandBridge(Node):
    # On transport/ACK failures continue attempting STOP at a bounded rate.
    # Physical output stays disabled until Pi acknowledges a STOP.
    STOP_RETRY_INTERVAL_SEC = 0.50
    SHUTDOWN_STOP_MAX_ATTEMPTS = 3
    SHUTDOWN_STOP_RETRY_SEC = 0.10

    def __init__(self) -> None:
        super().__init__("nav2_command_bridge")

        self.declare_parameter(
            "cmd_vel_topic",
            "/cmd_vel_nav_dry_run",
        )
        self.declare_parameter(
            "wheel_track_m",
            0.201,
        )
        self.declare_parameter(
            "max_wheel_mps",
            env_float("MAX_WHEEL_MPS", minimum=0.01),
        )
        self.declare_parameter(
            "twist_timeout_sec",
            0.50,
        )
        self.declare_parameter(
            "server_base_url",
            env_text("SERVER_BASE_URL"),
        )
        self.declare_parameter(
            "robot_id",
            env_text("ROBOT_ID"),
        )
        self.declare_parameter(
            "request_timeout_sec",
            0.25,
        )
        self.declare_parameter(
            "odom_topic",
            "/odom",
        )
        self.declare_parameter(
            "map_frame",
            "map",
        )
        self.declare_parameter(
            "odom_frame",
            "odom",
        )
        self.declare_parameter(
            "base_frame",
            "base_link",
        )
        self.declare_parameter(
            "min_auto_drive_pwm",
            0.50,
        )

        self.cmd_vel_topic = str(
            self.get_parameter(
                "cmd_vel_topic"
            ).value
        )

        self.wheel_track_m = float(
            self.get_parameter(
                "wheel_track_m"
            ).value
        )

        self.max_wheel_mps = float(
            self.get_parameter(
                "max_wheel_mps"
            ).value
        )

        self.twist_timeout_sec = float(
            self.get_parameter(
                "twist_timeout_sec"
            ).value
        )

        self.server_base_url = str(
            self.get_parameter(
                "server_base_url"
            ).value
        ).rstrip("/")

        self.robot_id = str(
            self.get_parameter(
                "robot_id"
            ).value
        ).strip()

        self.control_token = env_text("ROBOT_CONTROL_TOKEN")

        request_timeout_sec = float(
            self.get_parameter("request_timeout_sec").value
        )
        if not math.isfinite(request_timeout_sec) or request_timeout_sec <= 0.0:
            raise ValueError("request_timeout_sec must be finite and positive")
        self.request_timeout_sec = max(0.05, request_timeout_sec)

        self.odom_topic = str(
            self.get_parameter(
                "odom_topic"
            ).value
        )
        self.map_frame = str(
            self.get_parameter(
                "map_frame"
            ).value
        )
        self.odom_frame = str(
            self.get_parameter(
                "odom_frame"
            ).value
        )
        self.base_frame = str(
            self.get_parameter(
                "base_frame"
            ).value
        )
        self.min_auto_drive_pwm = float(
            self.get_parameter(
                "min_auto_drive_pwm"
            ).value
        )

        if not math.isfinite(self.wheel_track_m) or self.wheel_track_m <= 0.0:
            raise ValueError(
                "wheel_track_m must be finite and positive"
            )

        if (
            not math.isfinite(self.max_wheel_mps)
            or not math.isclose(
                self.max_wheel_mps, env_float("MAX_WHEEL_MPS", minimum=0.01)
            )
        ):
            raise ValueError(
                "max_wheel_mps must match MAX_WHEEL_MPS on both hosts"
            )

        if not math.isfinite(self.twist_timeout_sec) or self.twist_timeout_sec <= 0.0:
            raise ValueError(
                "twist_timeout_sec must be finite and positive"
            )

        if not 0.0 <= self.min_auto_drive_pwm <= 1.0:
            raise ValueError(
                "min_auto_drive_pwm must be between 0 and 1"
            )

        if not self.robot_id:
            raise ValueError(
                "robot_id must not be empty"
            )

        encoded_robot_id = quote(
            self.robot_id,
            safe="",
        )

        self.command_url = (
            f"{self.server_base_url}"
            f"/api/robots/{encoded_robot_id}"
            "/command"
        )

        # 브리지에서 서버까지의 HTTP 요청은 허용한다.
        # 실제 Pi 모터 출력은 서버의 MOTOR_OUTPUT_ENABLED와
        # 아래 motor_output_enabled가 모두 활성화되기 전까지 차단한다.
        self.server_request_enabled = True
        self.motor_output_enabled = env_bool("MOTOR_OUTPUT_ENABLED", default=False)

        self.last_twist_at: float | None = None
        self.last_log_at = 0.0
        self.last_server_log_at = 0.0
        self.last_error_log_at = 0.0
        self.latest_odom: Odometry | None = None
        self.rear_reverse_guard = RearReverseGuard()

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(
            self.tf_buffer,
            self,
        )

        self.timed_out = True
        self.received_count = 0
        self.rejected_count = 0
        self.sent_count = 0
        self.send_error_count = 0

        self._command_condition = threading.Condition()
        # In live-motor mode, a newly started/restarted Nav2 bridge must
        # establish a Pi-acknowledged STOP before accepting ANY drive.
        # This does not affect DRY_RUN launches.
        self._pending_command: dict | None = (
            {
                "type": "stop",
                "reason": "nav2_command_bridge_startup",
                "source": "nav2_command_bridge",
                "dry_run": False,
            }
            if self.motor_output_enabled else None
        )
        self._worker_shutdown = False
        # A physical STOP may supersede a pending drive, but no later drive
        # may erase a queued or currently transmitting STOP command.
        self._stop_command_in_flight = False
        # A failed physical STOP must be explicitly retried/acknowledged
        # before accepting any subsequent auto_drive.
        self._stop_delivery_failed = bool(self.motor_output_enabled)
        # One acknowledged STOP is sufficient for a continuous zero-Twist
        # interval. Sending a STOP on each controller tick repeatedly advances
        # the Pi safety_epoch and cancels its physical direction-switch
        # confirmation, starving reverse -> forward handoffs.
        self._zero_twist_stop_active = False
        self._stop_retry_command: dict | None = None
        self._stop_retry_due_at: float | None = None

        self._command_worker_thread = threading.Thread(
            target=self._command_worker,
            name="nav2-command-http-worker",
            daemon=True,
        )
        self._command_worker_thread.start()

        self.create_subscription(
            Twist,
            self.cmd_vel_topic,
            self.on_twist,
            10,
        )
        self.create_subscription(
            Odometry,
            self.odom_topic,
            self.on_odom,
            20,
        )
        self.create_subscription(
            LaserScan,
            "/scan",
            self.on_scan,
            qos_profile_sensor_data,
        )

        self.create_timer(
            0.05,
            self.check_timeout,
        )

        self.get_logger().info(
            "nav2_command_bridge ready "
            f"topic={self.cmd_vel_topic} "
            f"wheel_track_m={self.wheel_track_m:.3f} "
            f"max_wheel_mps={self.max_wheel_mps:.3f} "
            f"command_url={self.command_url} "
            f"odom_topic={self.odom_topic} "
            "server_request_enabled=true "
            f"motor_output_enabled={str(self.motor_output_enabled).lower()}"
        )

    def _queue_command(
        self,
        payload: dict,
    ) -> bool:
        if not self.server_request_enabled:
            return False

        command = {
            **payload,
            "source": "nav2_command_bridge",
            "dry_run": not self.motor_output_enabled,
        }

        with self._command_condition:
            if self._worker_shutdown:
                return False

            is_stop = command.get("type") in ("stop", "emergency_stop")
            is_zero_twist_stop = (
                command.get("type") == "stop"
                and command.get("reason") == "nav2_zero_twist"
            )
            if (
                is_zero_twist_stop
                and self._zero_twist_stop_active
                and not self._stop_delivery_failed
            ):
                # First STOP is already queued, in flight, or acknowledged.
                # Never reset the Pi's STOP/encoder handoff for every zero
                # velocity sample. An unacknowledged STOP is still retried
                # by the existing worker, and all OTHER safety STOPs bypass
                # this idempotence gate.
                return True
            pending_is_stop = (
                self._pending_command is not None
                and self._pending_command.get("type") in ("stop", "emergency_stop")
            )
            # STOP is a safety barrier. Never allow auto_drive to erase
            # pending, in-flight, or unacknowledged STOP.
            if not is_stop and (
                pending_is_stop or self._stop_command_in_flight
                or self._stop_delivery_failed
            ):
                return False
            if is_stop and (self._stop_command_in_flight or self._stop_delivery_failed):
                # Repeated zero-Twist / timeout callbacks must not bypass
                # the retry rate limit or flood the HTTP/Pi ACK path.
                # Update the reason without delaying a scheduled retry.
                if self._stop_delivery_failed:
                    self._stop_retry_command = command
                    if self._stop_retry_due_at is None:
                        self._stop_retry_due_at = time.monotonic()
                    self._command_condition.notify_all()
                return True
            self._pending_command = command
            if is_zero_twist_stop:
                self._zero_twist_stop_active = True
            elif not is_stop:
                # Any newly queued movement invalidates the previous STOP;
                # even a later zero Twist must issue a new physical STOP.
                self._zero_twist_stop_active = False
            self._command_condition.notify()

        return True

    def _command_worker(self) -> None:
        while True:
            with self._command_condition:
                while True:
                    # Shutdown may enqueue one final STOP; do not spawn
                    # additional retries after the worker is being destroyed.
                    if self._pending_command is not None:
                        payload = self._pending_command
                        self._pending_command = None
                        break
                    if self._worker_shutdown:
                        return
                    if self._stop_delivery_failed and self._stop_retry_command is not None:
                        due_at = self._stop_retry_due_at
                        remaining = max(0.0, (due_at or 0.0) - time.monotonic())
                        if remaining <= 0.0:
                            payload = self._stop_retry_command
                            self._stop_retry_command = None
                            self._stop_retry_due_at = None
                            break
                        self._command_condition.wait(timeout=remaining)
                    else:
                        self._command_condition.wait()

                is_stop = payload.get("type") in ("stop", "emergency_stop")
                shutdown_stop = is_stop and self._worker_shutdown
                if is_stop:
                    self._stop_command_in_flight = True

            stop_confirmed = False
            # At shutdown, send a bounded number of final STOP attempts
            # serially after any in-flight drive has completed. Neither
            # timers nor a second HTTP thread may overtake this command.
            max_attempts = (
                self.SHUTDOWN_STOP_MAX_ATTEMPTS if shutdown_stop else 1
            )
            try:
                for attempt in range(max_attempts):
                    try:
                        sent = self._post_command(payload)
                        stop_confirmed = sent is True
                    except Exception as exc:
                        # Never kill the sole command worker on a bad payload
                        # or failed socket; preserve subsequent STOP delivery.
                        self.send_error_count += 1
                        try:
                            self._log_send_error(f"command worker failure: {exc}")
                        except Exception:
                            pass
                    if stop_confirmed:
                        break
                    if shutdown_stop and attempt + 1 < max_attempts:
                        time.sleep(self.SHUTDOWN_STOP_RETRY_SEC)
            finally:
                if not is_stop and payload.get("type") == "auto_drive" and not stop_confirmed:
                    # A timed-out/negative Pi ACK does not prove that the
                    # wheels stayed still. The Pi may have executed the drive
                    # and lost its ACK. Discard queued drives, latch motion,
                    # and serialize a STOP behind the in-flight request.
                    with self._command_condition:
                        self._stop_delivery_failed = True
                        pending_is_stop = (
                            self._pending_command is not None
                            and self._pending_command.get("type") in ("stop", "emergency_stop")
                        )
                        if not self._worker_shutdown and not pending_is_stop:
                            self._pending_command = {
                                "type": "stop",
                                "reason": "nav2_drive_ack_unconfirmed",
                                "source": "nav2_command_bridge",
                                "dry_run": not self.motor_output_enabled,
                            }
                        self._command_condition.notify_all()
                elif is_stop:
                    with self._command_condition:
                        self._stop_delivery_failed = not stop_confirmed
                        self._stop_command_in_flight = False
                        if stop_confirmed:
                            self._stop_retry_command = None
                            self._stop_retry_due_at = None
                        elif not self._worker_shutdown:
                            # Persistent, rate-limited retries; keep motion
                            # latched off until a Pi STOP ACK is received.
                            self._stop_retry_command = dict(payload)
                            self._stop_retry_due_at = (
                                time.monotonic() + self.STOP_RETRY_INTERVAL_SEC
                            )
                        self._command_condition.notify_all()

    def _post_command(
        self,
        payload: dict,
    ) -> bool:
        try:
            # json.dumps(allow_nan=False), Request construction and the
            # HTTP response parser must all live inside the exception gate.
            # Otherwise a malformed payload can kill the STOP worker.
            body = json.dumps(
                payload,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")

            request = Request(
                self.command_url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Robot-Control-Token": self.control_token,
                },
                method="POST",
            )

            with urlopen(
                request,
                timeout=self.request_timeout_sec,
            ) as response:
                raw_body = response.read().decode(
                    "utf-8"
                )

                response_data = (
                    json.loads(raw_body)
                    if raw_body
                    else {}
                )

                if (
                    not isinstance(response_data, dict)
                    or response_data.get("ok") is not True
                    or (
                        payload.get("type") in ("auto_drive", "stop", "emergency_stop")
                        and self.motor_output_enabled
                        and (
                            response_data.get("delivered") is not True
                            or response_data.get("blocked") is True
                        )
                    )
                ):
                    raise RuntimeError(
                        "server rejected command: "
                        f"{response_data}"
                    )

            self.sent_count += 1

            now = time.monotonic()

            if now - self.last_server_log_at >= 0.2:
                self.last_server_log_at = now

                self.get_logger().info(
                    "SERVER_RESPONSE "
                    f"type={payload.get('type')} "
                    f"ok={response_data.get('ok')} "
                    f"accepted={response_data.get('accepted')} "
                    f"delivered={response_data.get('delivered')} "
                    f"blocked={response_data.get('blocked')} "
                    f"dry_run={response_data.get('dry_run')} "
                    "motor_output_enabled="
                    f"{response_data.get('motor_output_enabled')}"
                )

            return True
        except HTTPError as exc:
            self.send_error_count += 1

            try:
                error_body = exc.read().decode(
                    "utf-8"
                )
            except Exception:
                error_body = ""

            self._log_send_error(
                "HTTP command failed "
                f"status={exc.code} "
                f"body={error_body}"
            )

        except (
            URLError,
            TimeoutError,
            json.JSONDecodeError,
            RuntimeError,
            ValueError,
            TypeError,
            OverflowError,
        ) as exc:
            self.send_error_count += 1
            self._log_send_error(
                f"command failed: {exc}"
            )

        return False

    def _log_send_error(
        self,
        message: str,
    ) -> None:
        now = time.monotonic()

        # 통신 장애 시 로그가 과도하게 쌓이지 않도록 제한한다.
        if now - self.last_error_log_at < 1.0:
            return

        self.last_error_log_at = now
        self.get_logger().error(message)

    def _queue_stop(
        self,
        reason: str,
    ) -> bool:
        return self._queue_command(
            {
                "type": "stop",
                "reason": reason,
            }
        )

    @staticmethod
    def _yaw_from_quaternion(rotation) -> float:
        siny_cosp = 2.0 * (
            rotation.w * rotation.z
            + rotation.x * rotation.y
        )
        cosy_cosp = 1.0 - 2.0 * (
            rotation.y * rotation.y
            + rotation.z * rotation.z
        )
        return math.atan2(
            siny_cosp,
            cosy_cosp,
        )

    def on_odom(
        self,
        msg: Odometry,
    ) -> None:
        self.latest_odom = msg
        pose = msg.pose.pose
        self.rear_reverse_guard.update_odometry(
            float(pose.position.x),
            float(pose.position.y),
            self._yaw_from_quaternion(pose.orientation),
            time.monotonic(),
        )

    def on_scan(self, msg: LaserScan) -> None:
        frame_id = str(msg.header.frame_id or "").lstrip("/")
        base_frame = self.base_frame.lstrip("/")
        laser_yaw = None
        if frame_id == base_frame:
            laser_yaw = 0.0
        elif frame_id:
            try:
                transform = self.tf_buffer.lookup_transform(
                    base_frame, frame_id, Time(),
                )
                laser_yaw = self._yaw_from_quaternion(transform.transform.rotation)
            except TransformException:
                pass
        self.rear_reverse_guard.update_scan(
            msg.ranges,
            angle_min=float(msg.angle_min),
            angle_increment=float(msg.angle_increment),
            range_min=float(msg.range_min),
            range_max=float(msg.range_max),
            laser_yaw=laser_yaw,
            now=time.monotonic(),
        )

    def _estimate_pwm(
        self,
        left_mps: float,
        right_mps: float,
    ) -> tuple[float, float]:
        left_pwm = left_mps / self.max_wheel_mps
        right_pwm = right_mps / self.max_wheel_mps

        peak = max(
            abs(left_pwm),
            abs(right_pwm),
        )

        if peak > 1.0:
            left_pwm /= peak
            right_pwm /= peak
            peak = 1.0

        if (
            0.0 < peak
            < self.min_auto_drive_pwm
        ):
            scale = (
                self.min_auto_drive_pwm
                / peak
            )
            left_pwm *= scale
            right_pwm *= scale

        return left_pwm, right_pwm

    def _lookup_pose(
        self,
        parent_frame: str,
        child_frame: str,
    ) -> tuple[float, float, float]:
        try:
            transform = self.tf_buffer.lookup_transform(
                parent_frame,
                child_frame,
                Time(),
            )
        except TransformException:
            return math.nan, math.nan, math.nan

        translation = transform.transform.translation
        rotation = transform.transform.rotation
        return (
            float(translation.x),
            float(translation.y),
            self._yaw_from_quaternion(rotation),
        )

    def _log_drive_diagnostics(
        self,
        *,
        linear_x: float,
        angular_z: float,
        left_mps: float,
        right_mps: float,
    ) -> None:
        left_pwm, right_pwm = self._estimate_pwm(
            left_mps,
            right_mps,
        )

        if self.latest_odom is None:
            odom_x = math.nan
            odom_y = math.nan
            odom_yaw = math.nan
            odom_linear = math.nan
            odom_angular = math.nan
        else:
            odom = self.latest_odom
            odom_x = float(
                odom.pose.pose.position.x
            )
            odom_y = float(
                odom.pose.pose.position.y
            )
            odom_yaw = self._yaw_from_quaternion(
                odom.pose.pose.orientation
            )
            odom_linear = float(
                odom.twist.twist.linear.x
            )
            odom_angular = float(
                odom.twist.twist.angular.z
            )

        (
            map_odom_x,
            map_odom_y,
            map_odom_yaw,
        ) = self._lookup_pose(
            self.map_frame,
            self.odom_frame,
        )
        (
            map_base_x,
            map_base_y,
            map_base_yaw,
        ) = self._lookup_pose(
            self.map_frame,
            self.base_frame,
        )

        self.get_logger().info(
            "NAV_DIAG "
            f"cmd_linear={linear_x:.4f} "
            f"cmd_angular={angular_z:.4f} "
            f"left_mps={left_mps:.4f} "
            f"right_mps={right_mps:.4f} "
            f"left_pwm_est={left_pwm:.4f} "
            f"right_pwm_est={right_pwm:.4f} "
            f"odom_x={odom_x:.4f} "
            f"odom_y={odom_y:.4f} "
            f"odom_yaw={odom_yaw:.4f} "
            f"odom_linear={odom_linear:.4f} "
            f"odom_angular={odom_angular:.4f} "
            f"map_odom_x={map_odom_x:.4f} "
            f"map_odom_y={map_odom_y:.4f} "
            f"map_odom_yaw={map_odom_yaw:.4f} "
            f"map_base_x={map_base_x:.4f} "
            f"map_base_y={map_base_y:.4f} "
            f"map_base_yaw={map_base_yaw:.4f}"
        )

    def on_twist(
        self,
        msg: Twist,
    ) -> None:
        linear_x = float(msg.linear.x)
        angular_z = float(msg.angular.z)

        # Validate all six axes, not only the two we convert to wheel
        # speeds. Otherwise lateral/vertical commands can be silently
        # accepted, or mistaken for a physically verified zero STOP.
        if not valid_differential_twist(
            linear_x, msg.linear.y, msg.linear.z,
            msg.angular.x, msg.angular.y, angular_z,
        ):
            self.rejected_count += 1

            self.get_logger().warning(
                "Twist rejected: non-finite or unsupported motion axis"
            )
            self._queue_stop("nav2_invalid_twist")
            return

        now = time.monotonic()
        rear_blind_limited = False
        if linear_x < 0.0:
            linear_x, rear_blind_limited, rear_stop_reason = (
                self.rear_reverse_guard.limit_reverse(
                    linear_x, max_wheel_mps=self.max_wheel_mps, now=now,
                )
            )
            if rear_stop_reason is not None:
                self.last_twist_at = now
                self._queue_stop(rear_stop_reason)
                return

        half_track = (
            self.wheel_track_m / 2.0
        )

        left_raw = (
            linear_x
            - angular_z * half_track
        )

        right_raw = (
            linear_x
            + angular_z * half_track
        )

        # 좌우 속도 비율을 유지하며 최대 속도를 제한한다.
        peak = max(
            abs(left_raw),
            abs(right_raw),
        )

        if peak > self.max_wheel_mps:
            scale = (
                self.max_wheel_mps / peak
            )
        else:
            scale = 1.0

        left_mps = left_raw * scale
        right_mps = right_raw * scale
        if rear_blind_limited:
            # Angular steering can enlarge individual wheel speeds after the
            # linear_x cap. Enforce the 30% limit on both wheels.
            rear_limit = self.max_wheel_mps * 0.30
            rear_peak = max(abs(left_mps), abs(right_mps))
            if rear_peak > rear_limit:
                left_mps *= rear_limit / rear_peak
                right_mps *= rear_limit / rear_peak

        self.last_twist_at = now
        self.timed_out = False
        self.received_count += 1

        if (
            abs(left_mps) < 1e-6
            and abs(right_mps) < 1e-6
        ):
            would_send = self._queue_stop(
                "nav2_zero_twist"
            )
        else:
            would_send = self._queue_command(
                {
                    "type": "auto_drive",
                    "left_mps": left_mps,
                    "right_mps": right_mps,
                    "rear_blind_limited": rear_blind_limited,
                }
            )

        # 로그 과다 출력을 막기 위해 0.2초마다 출력한다.
        if now - self.last_log_at >= 0.2:
            self.last_log_at = now

            self.get_logger().info(
                "DRY_RUN "
                f"linear_x={linear_x:.4f} "
                f"angular_z={angular_z:.4f} "
                f"left_mps={left_mps:.4f} "
                f"right_mps={right_mps:.4f} "
                f"limited={scale < 1.0} "
                f"would_send={str(would_send).lower()} "
                "motor_output_enabled="
                f"{str(self.motor_output_enabled).lower()}"
            )
            self._log_drive_diagnostics(
                linear_x=linear_x,
                angular_z=angular_z,
                left_mps=left_mps,
                right_mps=right_mps,
            )

    def check_timeout(self) -> None:
        if (
            self.last_twist_at is None
            or self.timed_out
        ):
            return

        age_sec = (
            time.monotonic()
            - self.last_twist_at
        )

        if age_sec <= self.twist_timeout_sec:
            return

        self.timed_out = True

        would_send = self._queue_stop(
            "nav2_twist_timeout"
        )

        self.get_logger().warning(
            "DRY_RUN STOP "
            "reason=twist_timeout "
            f"age_sec={age_sec:.3f} "
            "left_mps=0.0000 "
            "right_mps=0.0000 "
            f"would_send={str(would_send).lower()} "
            "motor_output_enabled="
            f"{str(self.motor_output_enabled).lower()}"
        )

    def destroy_node(self):
        # Serialize the final STOP behind any in-flight drive on the SAME
        # worker. A synchronous STOP from this thread could be delivered
        # before a blocked drive request and then undone by that old drive.
        # If the join deadline expires, the daemon worker will still send
        # this queued STOP after its bounded HTTP request finishes; server
        # and Pi watchdogs independently handle network outages.
        with self._command_condition:
            self._pending_command = (
                {"type": "stop", "reason": "nav2_command_bridge_shutdown"}
                if self.motor_output_enabled else None
            )
            self._worker_shutdown = True
            self._command_condition.notify_all()

        # Budget for one in-flight drive, up to three final STOP requests,
        # and the bounded retry intervals. A kernel/socket hang can still
        # exceed this, so the independent Pi/Pico watchdogs are essential.
        self._command_worker_thread.join(
            timeout=(
                (1 + self.SHUTDOWN_STOP_MAX_ATTEMPTS) * self.request_timeout_sec
                + (self.SHUTDOWN_STOP_MAX_ATTEMPTS - 1) * self.SHUTDOWN_STOP_RETRY_SEC
                + 0.5
            )
        )
        if self._command_worker_thread.is_alive():
            self.get_logger().error(
                "Nav2 command worker still active during shutdown; "
                "remote STOP delivery remains unconfirmed"
            )
        elif self._stop_delivery_failed:
            self.get_logger().error(
                "Nav2 shutdown STOP was not acknowledged by the server"
            )

        return super().destroy_node()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = Nav2CommandBridge()

    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
