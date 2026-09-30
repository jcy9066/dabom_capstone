from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from pathlib import Path
from urllib.parse import quote

import requests
import websockets
from dotenv import load_dotenv

from controllers.motor_controller import MotorController
from controllers.speaker_controller import SpeakerController

try:
    from raspberry.encoder_ros_publisher import EncoderRosPublisher
except ModuleNotFoundError:
    from encoder_ros_publisher import EncoderRosPublisher
try:
    from raspberry.env_config import env_float, env_int, env_text
except ModuleNotFoundError:  # Direct script execution from raspberry/.
    from env_config import env_float, env_int, env_text


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")


class RobotCommandClient:
    def __init__(self, args: argparse.Namespace) -> None:
        self.robot_id = args.robot_id
        self.server_base_url = args.server_base_url.rstrip("/")
        self.control_token = args.control_token

        self.status_url = (
            f"{self.server_base_url}/status"
        )

        ws_url = (
            self.server_base_url
            .replace("http://", "ws://")
            .replace("https://", "wss://")
            + f"/ws/robot/{quote(self.robot_id, safe='')}"
        )
        self.ws_url = ws_url

        self.status_interval_sec = (
            args.status_interval_sec
        )
        self.status_request_timeout_sec = (
            args.status_request_timeout_sec
        )
        self.ws_reconnect_delay_sec = (
            args.ws_reconnect_delay_sec
        )
        self.encoder_interval_sec = (
            args.encoder_interval_sec
        )

        self.running = True
        self.current_mode = "manual"
        self.navigation_mode = "mapping"
        self.emergency_stop_latched = False
        self.led_enabled = False
        self._led_task = None
        self._cpu_sample = None
        self._last_status_latency_ms = None
        self._server_reachable = None

        self.motor = MotorController(
            serial_port=args.serial_port,
            baudrate=args.serial_baudrate,
            command_timeout_sec=args.command_timeout_sec,
            serial_timeout_sec=args.serial_timeout_sec,
            max_wheel_mps=args.max_wheel_mps,
        )

        self.speaker = SpeakerController()
        self.encoder_ros = EncoderRosPublisher(
            topic=env_text("WHEEL_TICKS_TOPIC")
        )

    def start(self) -> None:
        self.encoder_ros.start()

        threads = [
            threading.Thread(
                target=self.status_loop,
                daemon=True,
            ),
            threading.Thread(
                target=self.failsafe_loop,
                daemon=True,
            ),
        ]

        for thread in threads:
            thread.start()

        try:
            asyncio.run(self.command_loop())

        except KeyboardInterrupt:
            print("\n[robot-client] 종료 요청")

        finally:
            self.running = False
            self.encoder_ros.close()
            self.motor.close()

    @staticmethod
    def _read_cpu_times() -> tuple[int, int] | None:
        try:
            line = Path("/proc/stat").read_text(
                encoding="utf-8"
            ).splitlines()[0]
            fields = line.split()
            if not fields or fields[0] != "cpu":
                return None
            values = [int(value) for value in fields[1:]]
            if len(values) < 4:
                return None
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            return sum(values), idle
        except (OSError, ValueError, IndexError):
            return None

    def _cpu_usage_percent(self) -> float | None:
        current = self._read_cpu_times()
        if current is None:
            return None
        previous = self._cpu_sample
        self._cpu_sample = current
        if previous is None:
            return None
        total_delta = current[0] - previous[0]
        idle_delta = current[1] - previous[1]
        if total_delta <= 0:
            return None
        busy = 100.0 * (total_delta - idle_delta) / total_delta
        return round(max(0.0, min(100.0, busy)), 1)

    @staticmethod
    def _ram_usage_percent() -> float | None:
        try:
            values = {}
            for line in Path("/proc/meminfo").read_text(
                encoding="utf-8"
            ).splitlines():
                key, _, raw = line.partition(":")
                if key in {"MemTotal", "MemAvailable"}:
                    values[key] = int(raw.strip().split()[0])
            total = values.get("MemTotal", 0)
            available = values.get("MemAvailable", 0)
            if total <= 0:
                return None
            used = 100.0 * (total - available) / total
            return round(max(0.0, min(100.0, used)), 1)
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def _cpu_temp_c() -> float | None:
        for path in (
            Path("/sys/class/thermal/thermal_zone0/temp"),
            Path("/sys/devices/virtual/thermal/thermal_zone0/temp"),
        ):
            try:
                raw = float(path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            value = raw / 1000.0 if raw > 200.0 else raw
            if -40.0 <= value <= 150.0:
                return round(value, 1)
        return None

    def status_payload(self) -> dict:
        if self._server_reachable is True:
            internet = "ok"
        elif self._server_reachable is False:
            internet = "offline"
        else:
            internet = "unknown"

        return {
            "robot_id": self.robot_id,
            "cpu_usage": self._cpu_usage_percent(),
            "cpu_temp": self._cpu_temp_c(),
            "ram_usage": self._ram_usage_percent(),
            "internet": internet,
            "ping": self._last_status_latency_ms,
            "mode": self.current_mode,
            "navigation_mode": self.navigation_mode,
            "navigation_state": (
                "EMERGENCY_STOPPED"
                if self.emergency_stop_latched
                else "IDLE"
            ),
            "emergency_stop": self.emergency_stop_latched,
            "led_enabled": bool(getattr(self.motor, "led_enabled", self.led_enabled)),
            "motor_connected": self.motor.connected,
            "motor_motion": self.motor.current_motion,
        }

    def status_loop(self) -> None:
        while self.running:
            started_at = time.monotonic()
            try:
                requests.post(
                    self.status_url,
                    json=self.status_payload(),
                    headers={"X-Robot-Control-Token": self.control_token},
                    timeout=self.status_request_timeout_sec,
                )
                self._last_status_latency_ms = round(
                    (time.monotonic() - started_at) * 1000.0,
                    1,
                )
                self._server_reachable = True

            except requests.RequestException as exc:
                self._last_status_latency_ms = None
                self._server_reachable = False
                print(f"[status] 전송 실패: {exc}")

            time.sleep(self.status_interval_sec)

    def failsafe_loop(self) -> None:
        while self.running:
            self.motor.failsafe_tick()
            time.sleep(0.05)

    async def command_loop(self) -> None:
        while self.running:
            try:
                async with websockets.connect(
                    self.ws_url,
                    extra_headers={"X-Robot-Control-Token": self.control_token},
                    ping_interval=10,
                    ping_timeout=5,
                    close_timeout=1,
                    max_queue=16,
                ) as websocket:

                    print(f"[ws] connected: robot_id={self.robot_id}")

                    encoder_task = asyncio.create_task(
                        self.encoder_telemetry_loop(
                            websocket
                        )
                    )

                    try:
                        async for raw_message in websocket:
                            message = json.loads(raw_message)

                            await self.handle_command(
                                websocket,
                                message,
                            )

                    finally:
                        encoder_task.cancel()

                        try:
                            await encoder_task
                        except asyncio.CancelledError:
                            pass
                        except Exception as exc:
                            print(f"[encoder] telemetry stopped: {exc}")
                        finally:
                            was_moving = self.motor.current_motion != "stop"
                            self.motor.stop(
                                reason="websocket_disconnected",
                                suppress_errors=True,
                            )
                            if was_moving:
                                self.emergency_stop_latched = True

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                print(f"[ws] disconnected: {type(exc).__name__}")

                await asyncio.sleep(
                    self.ws_reconnect_delay_sec
                )

    async def encoder_telemetry_loop(
        self,
        websocket,
    ) -> None:
        while self.running:
            try:
                await asyncio.to_thread(
                    self.motor.start_encoder_stream
                )
                break

            except Exception as exc:
                print(
                    f"[encoder] UART 연결 실패: {exc}"
                )

                await asyncio.sleep(1.0)

        previous_sequence = -1

        while self.running:
            if self.motor.pico_reboot_pending():
                try:
                    await asyncio.to_thread(
                        self.motor.recover_after_pico_reboot
                    )
                except Exception as exc:
                    print(
                        f"[encoder] Pico reboot 복구 실패: {exc}"
                    )
                    await asyncio.sleep(1.0)
                    continue

            snapshot = self.motor.encoder_snapshot()

            if (
                snapshot is not None
                and snapshot["sequence"]
                != previous_sequence
            ):
                previous_sequence = snapshot["sequence"]

                self.encoder_ros.publish(snapshot)

                message = {
                    "type": "encoder",
                    "robot_id": self.robot_id,
                    "data": {
                        "sequence": snapshot[
                            "sequence"
                        ],
                        "left_front_ticks": snapshot[
                            "left_front_ticks"
                        ],
                        "right_front_ticks": snapshot[
                            "right_front_ticks"
                        ],
                        "left_rear_ticks": snapshot[
                            "left_rear_ticks"
                        ],
                        "right_rear_ticks": snapshot[
                            "right_rear_ticks"
                        ],
                        "pico_timestamp_ms": snapshot[
                            "pico_timestamp_ms"
                        ],
                        "pi_timestamp": snapshot[
                            "updated_at"
                        ],
                    },
                }

                await websocket.send(
                    json.dumps(
                        message,
                        separators=(",", ":"),
                    )
                )

            await asyncio.sleep(
                self.encoder_interval_sec
            )


    async def handle_command(
        self,
        websocket,
        message: dict,
    ) -> None:
        command_type = str(
            message.get("type", "")
        ).strip().lower()

        ok = True
        error = None

        try:
            if command_type == "move":
                if self.emergency_stop_latched:
                    raise RuntimeError("emergency stop is latched")
                if self.current_mode != "manual":
                    raise RuntimeError(
                        "수동 모드가 아니므로 "
                        "이동 명령을 거부했습니다."
                    )

                await asyncio.to_thread(
                    self.motor.move,
                    message.get("direction", ""),
                    message.get("speed", 0.35),
                )

            elif command_type == "auto_drive":
                if self.emergency_stop_latched:
                    raise RuntimeError("emergency stop is latched")
                if self.current_mode != "auto":
                    raise RuntimeError(
                        "자동 모드가 아니므로 "
                        "자율주행 명령을 거부했습니다."
                    )

                await asyncio.to_thread(
                    self.motor.drive,
                    message.get("left_mps"),
                    message.get("right_mps"),
                )

            elif command_type == "stop":
                await asyncio.to_thread(
                    self.motor.stop,
                    message.get(
                        "reason",
                        "manual_stop",
                    ),
                )

            elif command_type == "emergency_stop":
                await asyncio.to_thread(
                    self.motor.stop,
                    "emergency_stop",
                )
                self.emergency_stop_latched = True

            elif command_type == "resume_navigation":
                await asyncio.to_thread(
                    self.motor.stop,
                    "resume_safety_check",
                )
                self.emergency_stop_latched = False

            elif command_type == "navigation_mode":
                target_navigation_mode = str(
                    message.get("mode", "")
                ).strip().lower()
                if target_navigation_mode not in {"mapping", "driving"}:
                    raise RuntimeError(
                        f"unsupported navigation mode: {target_navigation_mode}"
                    )
                await asyncio.to_thread(
                    self.motor.stop,
                    f"navigation_mode_{target_navigation_mode}",
                )
                self.navigation_mode = target_navigation_mode

            elif command_type == "mode":
                target_mode = str(
                    message.get("mode", "")
                ).strip().lower()

                if target_mode not in {
                    "auto",
                    "manual",
                }:
                    raise RuntimeError(
                        f"지원하지 않는 모드: "
                        f"{target_mode}"
                    )

                previous_mode = self.current_mode

                await asyncio.to_thread(
                    self.motor.stop,
                    (
                        "mode_change_"
                        f"{previous_mode}_to_{target_mode}"
                    ),
                )

                self.current_mode = target_mode
                print(
                    f"[mode] "
                    f"{previous_mode} -> {self.current_mode}"
                )

            elif command_type == "speak":
                self.speaker.speak(
                    str(message.get("text", ""))
                )

            elif command_type == "beep":
                duration_ms = self._duration_ms(
                    message.get("duration_ms", 350)
                )
                await asyncio.to_thread(
                    self.motor.beep,
                    duration_ms,
                )

            elif command_type == "led":
                enabled = message.get("enabled")
                if not isinstance(enabled, bool):
                    raise RuntimeError("led.enabled must be boolean")
                duration_ms = self._duration_ms(
                    message.get("duration_ms", 0)
                )
                await asyncio.to_thread(self.motor.set_led, enabled)
                self.led_enabled = enabled
                self._schedule_led_off(duration_ms if enabled else 0)

            elif command_type == "warning":
                duration_ms = self._duration_ms(
                    message.get("led_duration_ms", 3000)
                )
                await asyncio.to_thread(self.motor.set_led, True)
                self.led_enabled = True
                self._schedule_led_off(duration_ms)
                await asyncio.to_thread(
                    self.speaker.speak,
                    str(message.get("text", "")),
                )

            else:
                raise RuntimeError(
                    f"unknown command type: "
                    f"{command_type}"
                )

        except Exception as exc:
            ok = False
            error = str(exc)

            if command_type in {
                "move",
                "auto_drive",
            }:
                self.motor.stop(
                    reason=f"{command_type}_error",
                    suppress_errors=True,
                )

        await websocket.send(
            json.dumps(
                {
                    "type": "ack",
                    "command_id": message.get(
                        "command_id"
                    ),
                    "ok": ok,
                    "error": error,
                    "mode": self.current_mode,
                    "navigation_mode": self.navigation_mode,
                    "emergency_stop": self.emergency_stop_latched,
            "led_enabled": bool(getattr(self.motor, "led_enabled", self.led_enabled)),
                    "motion": self.motor.current_motion,
                    "motor_connected": (
                        self.motor.connected
                    ),
                }
            )
        )

    @staticmethod
    def _duration_ms(value) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise RuntimeError("duration must be an integer")
        if isinstance(value, str) and not value.strip().lstrip("+-").isdigit():
            raise RuntimeError("duration must be an integer")
        try:
            duration = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("duration must be an integer") from exc
        if duration < 0 or duration > 10000:
            raise RuntimeError("duration must be between 0 and 10000 ms")
        return duration

    def _schedule_led_off(self, duration_ms: int) -> None:
        if self._led_task is not None:
            self._led_task.cancel()
            self._led_task = None
        if duration_ms > 0:
            self._led_task = asyncio.create_task(
                self._turn_led_off_after(duration_ms)
            )

    async def _turn_led_off_after(self, duration_ms: int) -> None:
        try:
            await asyncio.sleep(duration_ms / 1000.0)
            await asyncio.to_thread(self.motor.set_led, False)
            self.led_enabled = False
        except asyncio.CancelledError:
            raise
        finally:
            if self._led_task is asyncio.current_task():
                self._led_task = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--server-base-url",
        default=env_text("SERVER_BASE_URL"),
    )

    parser.add_argument(
        "--robot-id",
        default=env_text("ROBOT_ID"),
    )

    parser.add_argument(
        "--control-token",
        default=env_text("ROBOT_CONTROL_TOKEN"),
    )

    parser.add_argument(
        "--serial-port",
        default=env_text("MOTOR_SERIAL_PORT"),
    )

    parser.add_argument(
        "--serial-baudrate",
        type=int,
        default=env_int("MOTOR_SERIAL_BAUDRATE", minimum=1),
    )

    parser.add_argument(
        "--serial-timeout-sec",
        type=float,
        default=env_float("MOTOR_SERIAL_TIMEOUT_SEC", minimum=0.0),
    )

    parser.add_argument(
        "--command-timeout-sec",
        type=float,
        default=env_float("COMMAND_TIMEOUT_SEC", minimum=0.01),
    )

    parser.add_argument(
        "--max-wheel-mps",
        type=float,
        default=env_float("MAX_WHEEL_MPS", minimum=0.01),
    )

    parser.add_argument(
        "--status-interval-sec",
        type=float,
        default=env_float("STATUS_INTERVAL_SEC", minimum=0.01),
    )

    parser.add_argument(
        "--status-request-timeout-sec",
        type=float,
        default=env_float("STATUS_REQUEST_TIMEOUT_SEC", minimum=0.01),
    )

    parser.add_argument(
        "--ws-reconnect-delay-sec",
        type=float,
        default=env_float("WS_RECONNECT_DELAY_SEC", minimum=0.01),
    )

    parser.add_argument(
        "--encoder-interval-sec",
        type=float,
        default=env_float("ENCODER_INTERVAL_SEC", minimum=0.001),
    )

    args = parser.parse_args()

    if not args.server_base_url:
        parser.error(
            "SERVER_BASE_URL 또는 "
            "--server-base-url이 필요합니다."
        )

    if not args.control_token:
        parser.error("ROBOT_CONTROL_TOKEN 또는 --control-token이 필요합니다.")

    return args


if __name__ == "__main__":
    RobotCommandClient(parse_args()).start()
