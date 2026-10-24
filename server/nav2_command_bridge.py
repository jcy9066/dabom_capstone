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
from rclpy.node import Node

try:
    from server.env_config import env_bool, env_float, env_text
except ModuleNotFoundError:  # Direct script execution from server/.
    from env_config import env_bool, env_float, env_text


class Nav2CommandBridge(Node):
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

        self.request_timeout_sec = max(
            0.05,
            float(
                self.get_parameter(
                    "request_timeout_sec"
                ).value
            ),
        )

        if self.wheel_track_m <= 0.0:
            raise ValueError(
                "wheel_track_m must be positive"
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

        if self.twist_timeout_sec <= 0.0:
            raise ValueError(
                "twist_timeout_sec must be positive"
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

        self.timed_out = True
        self.received_count = 0
        self.rejected_count = 0
        self.sent_count = 0
        self.send_error_count = 0

        self._command_condition = threading.Condition()
        self._pending_command: dict | None = None
        self._worker_shutdown = False

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

            # 아직 전송되지 않은 이전 명령은 폐기하고
            # 가장 최근 명령만 서버에 전송한다.
            self._pending_command = command
            self._command_condition.notify()

        return True

    def _command_worker(self) -> None:
        while True:
            with self._command_condition:
                self._command_condition.wait_for(
                    lambda: (
                        self._worker_shutdown
                        or self._pending_command
                        is not None
                    )
                )

                if (
                    self._worker_shutdown
                    and self._pending_command is None
                ):
                    return

                payload = self._pending_command
                self._pending_command = None

            if payload is not None:
                self._post_command(payload)

    def _post_command(
        self,
        payload: dict,
    ) -> bool:
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

        try:
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

                if not response_data.get(
                    "ok",
                    False,
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

    def on_twist(
        self,
        msg: Twist,
    ) -> None:
        linear_x = float(msg.linear.x)
        angular_z = float(msg.angular.z)

        if (
            not math.isfinite(linear_x)
            or not math.isfinite(angular_z)
        ):
            self.rejected_count += 1

            self.get_logger().warning(
                "Twist rejected: NaN or Infinity"
            )

            self._queue_stop(
                "nav2_invalid_twist"
            )
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

        now = time.monotonic()

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
        if self.motor_output_enabled:
            # 종료 시에는 대기열을 거치지 않고 즉시 정지 요청한다.
            self._post_command(
                {
                    "type": "stop",
                    "reason": (
                        "nav2_command_bridge_shutdown"
                    ),
                }
            )

        with self._command_condition:
            self._pending_command = None
            self._worker_shutdown = True
            self._command_condition.notify_all()

        self._command_worker_thread.join(
            timeout=(
                self.request_timeout_sec
                + 0.5
            )
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
