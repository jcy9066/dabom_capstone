from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from typing import Optional

try:
    from raspberry.env_config import env_float
except ModuleNotFoundError:  # Direct robot_command_client.py execution.
    from env_config import env_float

try:
    import serial
except ImportError:
    serial = None


ALLOWED_DIRECTIONS = frozenset(
    {
        "forward",
        "backward",
        "left",
        "right",
        "forward_left",
        "forward_right",
        "backward_left",
        "backward_right",
        "rotate_left",
        "rotate_right",
    }
)

_REASON_PATTERN = re.compile(r"[^a-zA-Z0-9_-]+")
MIN_AUTO_DRIVE_PWM = 0.50


class MotorControllerError(RuntimeError):
    """Pi-Pico UART 모터 제어 오류."""


class MotorController:
    """
    Raspberry Pi ↔ Pico W 단일 UART reader 구조.

    Pi → Pico:
      PING
      MOVE,<direction>,<speed>
      DRIVE,<left_normalized>,<right_normalized>
      STOP,<reason>
      ENC_RESET
      ENC_STREAM,0|1
      LED,0|1
      BEEP,<duration_ms>

    Pico → Pi:
      OK,...
      ERR,...
      EVENT,FAILSAFE_STOP
      EVENT,LED_FAILSAFE_OFF
      EVENT,ENC,<LF>,<RF>,<LR>,<RR>,<pico_ms>
    """

    def __init__(
        self,
        serial_port: str = "/dev/serial0",
        baudrate: int = 115200,
        command_timeout_sec: float = 0.45,
        serial_timeout_sec: float = 0.25,
        max_wheel_mps: Optional[float] = None,
    ) -> None:
        self.serial_port = serial_port
        self.baudrate = int(baudrate)
        self.command_timeout_sec = max(
            0.1,
            float(command_timeout_sec),
        )
        self.serial_timeout_sec = max(
            0.1,
            float(serial_timeout_sec),
        )
        # Full-PWM wheel speed calibration, shared with server and Nav2.
        self.max_wheel_mps = env_float("MAX_WHEEL_MPS", minimum=0.01)
        if max_wheel_mps is not None and (
            not math.isfinite(float(max_wheel_mps))
            or not math.isclose(float(max_wheel_mps), self.max_wheel_mps)
        ):
            raise MotorControllerError(
                "max_wheel_mps must match MAX_WHEEL_MPS; update the shared calibration"
            )

        self.last_command_at = 0.0
        self.current_motion = "stop"
        self.led_enabled = False

        self._serial: Optional[object] = None

        # 명령은 반드시 한 번에 하나만 전송한다.
        self._command_lock = threading.RLock()

        # 연결 상태와 encoder 상태 보호.
        self._state_lock = threading.RLock()

        # UART write 보호.
        self._write_lock = threading.Lock()

        # reader thread가 받은 OK/ERR 응답 전달.
        self._response_condition = threading.Condition()
        self._response_queue: deque[str] = deque()

        self._reader_running = False
        self._reader_thread: Optional[threading.Thread] = None

        # Pico가 재부팅되어 READY를 다시 보낸 상태.
        self._pico_reboot_event = threading.Event()

        self._encoder_state = {
            "sequence": 0,
            "left_front_ticks": 0,
            "right_front_ticks": 0,
            "left_rear_ticks": 0,
            "right_rear_ticks": 0,
            "pico_timestamp_ms": 0,
            "updated_at": None,
            "updated_monotonic": None,
        }

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return bool(
                self._serial is not None
                and self._serial.is_open
            )

    def encoder_snapshot(self) -> Optional[dict]:
        with self._state_lock:
            if self._encoder_state["updated_at"] is None:
                return None

            snapshot = dict(self._encoder_state)

        updated_monotonic = snapshot.pop(
            "updated_monotonic",
            None,
        )

        snapshot["age_sec"] = (
            None
            if updated_monotonic is None
            else max(
                0.0,
                time.monotonic() - updated_monotonic,
            )
        )

        return snapshot

    def _serial_device(self):
        with self._state_lock:
            return self._serial

    def _handle_encoder_event(self, line: str) -> None:
        parts = line.split(",")

        # EVENT,ENC,LF,RF,LR,RR,pico_ms
        if len(parts) != 7:
            print(f"[motor] 잘못된 encoder event: {line}")
            return

        try:
            left_front = int(parts[2])
            right_front = int(parts[3])
            left_rear = int(parts[4])
            right_rear = int(parts[5])
            pico_timestamp_ms = int(parts[6])
        except ValueError:
            print(f"[motor] encoder 숫자 변환 실패: {line}")
            return

        with self._state_lock:
            sequence = int(
                self._encoder_state["sequence"]
            ) + 1

            self._encoder_state = {
                "sequence": sequence,
                "left_front_ticks": left_front,
                "right_front_ticks": right_front,
                "left_rear_ticks": left_rear,
                "right_rear_ticks": right_rear,
                "pico_timestamp_ms": pico_timestamp_ms,
                "updated_at": time.time(),
                "updated_monotonic": time.monotonic(),
            }

    def _handle_uart_line(self, line: str) -> None:
        if line.startswith("EVENT,ENC,"):
            self._handle_encoder_event(line)
            return

        if line == "EVENT,FAILSAFE_STOP":
            with self._state_lock:
                self.current_motion = "stop"

            print("[motor] Pico failsafe stop")
            return

        if line == "EVENT,LED_FAILSAFE_OFF":
            with self._state_lock:
                self.led_enabled = False
            print("[motor] Pico warning LED failsafe off")
            return

        if line.startswith("READY,"):
            with self._state_lock:
                self.current_motion = "stop"
                self.led_enabled = False

            self._pico_reboot_event.set()
            print(f"[motor] Pico reboot detected: {line}")
            return

        if line.startswith(("OK", "ERR")):
            with self._response_condition:
                self._response_queue.append(line)
                self._response_condition.notify_all()
            return

        print(f"[motor] unknown UART line: {line}")

    def _reader_loop(self, serial_device) -> None:
        try:
            while True:
                with self._state_lock:
                    active = (
                        self._reader_running
                        and self._serial is serial_device
                    )

                if not active:
                    break

                try:
                    raw = serial_device.readline()
                except Exception as exc:
                    with self._state_lock:
                        still_active = (
                            self._serial is serial_device
                        )

                    if still_active:
                        print(
                            f"[motor] UART reader 오류: {exc}"
                        )

                    break

                if not raw:
                    continue

                line = raw.decode(
                    "ascii",
                    errors="replace",
                ).strip()

                if line:
                    self._handle_uart_line(line)

        finally:
            try:
                serial_device.close()
            except Exception:
                pass

            with self._state_lock:
                if self._serial is serial_device:
                    self._serial = None
                    self._reader_running = False

            with self._response_condition:
                self._response_condition.notify_all()

    def _close_serial_locked(self) -> None:
        with self._state_lock:
            serial_device = self._serial
            reader_thread = self._reader_thread

            self._serial = None
            self._reader_running = False
            self._reader_thread = None

        if serial_device is not None:
            try:
                serial_device.close()
            except Exception:
                pass

        with self._response_condition:
            self._response_queue.clear()
            self._response_condition.notify_all()

        if (
            reader_thread is not None
            and reader_thread is not threading.current_thread()
        ):
            reader_thread.join(timeout=0.5)

    def _exchange_locked(self, command: str) -> str:
        serial_device = self._serial_device()

        if serial_device is None or not serial_device.is_open:
            raise MotorControllerError(
                "Pico W UART가 연결되지 않았습니다."
            )

        with self._response_condition:
            self._response_queue.clear()

        try:
            with self._write_lock:
                serial_device.write(
                    (command + "\n").encode("ascii")
                )
                serial_device.flush()
        except Exception as exc:
            self._close_serial_locked()

            raise MotorControllerError(
                f"Pico W UART 쓰기 실패: {exc}"
            ) from exc

        deadline = (
            time.monotonic()
            + self.serial_timeout_sec
        )

        while True:
            with self._response_condition:
                while not self._response_queue:
                    remaining = deadline - time.monotonic()

                    if remaining <= 0:
                        break

                    self._response_condition.wait(
                        timeout=remaining
                    )

                if self._response_queue:
                    response = self._response_queue.popleft()
                else:
                    response = None

            if response is not None:
                if response.startswith("ERR"):
                    raise MotorControllerError(
                        f"Pico W 명령 거부: {response}"
                    )

                return response

            if not self.connected:
                raise MotorControllerError(
                    "Pico W UART 연결이 끊겼습니다."
                )

            if time.monotonic() >= deadline:
                self._close_serial_locked()

                raise MotorControllerError(
                    f"Pico W 응답 시간 초과: {command}"
                )

    def _ensure_connected_locked(self) -> None:
        if self.connected:
            return

        if serial is None:
            raise MotorControllerError(
                "pyserial이 설치되지 않았습니다."
            )

        try:
            serial_device = serial.Serial(
                port=self.serial_port,
                baudrate=self.baudrate,
                timeout=0.05,
                write_timeout=self.serial_timeout_sec,
            )

            # 이전 프로세스가 종료되기 전에 켜 둔 encoder stream을
            # reader 시작 전에 정리한다.
            serial_device.write(b"ENC_STREAM,0\n")
            serial_device.flush()
            time.sleep(0.1)
            serial_device.reset_input_buffer()

            with self._state_lock:
                self._serial = serial_device
                self._reader_running = True

                self._reader_thread = threading.Thread(
                    target=self._reader_loop,
                    args=(serial_device,),
                    daemon=True,
                    name="pico-uart-reader",
                )

                reader_thread = self._reader_thread

            reader_thread.start()

            # reader thread가 시작될 시간을 준다.
            time.sleep(0.05)

            self._exchange_locked("PING")
            self._exchange_locked("STOP,pi_connected")
            self._exchange_locked("LED,0")
            with self._state_lock:
                self.led_enabled = False
            self._exchange_locked("ENC_STREAM,1")

            print(
                f"[motor] Pico W connected: "
                f"{self.serial_port} @ {self.baudrate}"
            )
            print("[motor] encoder stream enabled")

        except Exception:
            self._close_serial_locked()
            raise

    @staticmethod
    def _sanitize_reason(reason: str) -> str:
        cleaned = _REASON_PATTERN.sub(
            "_",
            str(reason),
        ).strip("_")

        return (cleaned or "stop")[:48]

    def pico_reboot_pending(self) -> bool:
        """Pico 재부팅 후 복구가 필요한지 반환한다."""
        return self._pico_reboot_event.is_set()

    def recover_after_pico_reboot(self) -> None:
        """Pico 재부팅 후 모터 정지와 encoder stream을 복구한다."""
        with self._command_lock:
            self._ensure_connected_locked()
            self._exchange_locked("STOP,pico_reboot_recovery")
            self._exchange_locked("LED,0")
            with self._state_lock:
                self.led_enabled = False
            self._exchange_locked("ENC_STREAM,1")

        self._pico_reboot_event.clear()
        print("[motor] encoder stream recovered after Pico reboot")

    def start_encoder_stream(self) -> None:
        """Pico UART 연결 및 엔코더 스트림 시작."""
        with self._command_lock:
            self._ensure_connected_locked()

    def reset_encoders(self) -> None:
        with self._command_lock:
            self._ensure_connected_locked()
            self._exchange_locked("ENC_RESET")

            with self._state_lock:
                sequence = self._encoder_state["sequence"]

                self._encoder_state = {
                    "sequence": sequence,
                    "left_front_ticks": 0,
                    "right_front_ticks": 0,
                    "left_rear_ticks": 0,
                    "right_rear_ticks": 0,
                    "pico_timestamp_ms": 0,
                    "updated_at": None,
                    "updated_monotonic": None,
                }

    def set_led(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise MotorControllerError("LED state must be boolean")
        with self._command_lock:
            self._ensure_connected_locked()
            self._exchange_locked(f"LED,{1 if enabled else 0}")
            with self._state_lock:
                self.led_enabled = enabled

    def beep(self, duration_ms: int = 350) -> None:
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
            raise MotorControllerError("beep duration must be an integer")
        if duration_ms < 50 or duration_ms > 2000:
            raise MotorControllerError(
                "beep duration must be between 50 and 2000 ms"
            )
        with self._command_lock:
            self._ensure_connected_locked()
            self._exchange_locked(f"BEEP,{duration_ms}")

    def move(
        self,
        direction: str,
        speed: float = 1.0,
    ) -> None:
        direction = str(direction).strip().lower()

        if direction not in ALLOWED_DIRECTIONS:
            raise MotorControllerError(
                f"지원하지 않는 이동 방향: {direction}"
            )

        try:
            normalized_speed = float(speed)
        except (TypeError, ValueError) as exc:
            raise MotorControllerError(
                f"잘못된 속도 값: {speed}"
            ) from exc

        if not math.isfinite(normalized_speed):
            raise MotorControllerError(f"잘못된 속도 값: {speed}")

        normalized_speed = max(
            0.0,
            min(normalized_speed, 1.0),
        )

        if normalized_speed <= 0.0:
            self.stop(reason="zero_speed")
            return

        with self._command_lock:
            self._ensure_connected_locked()

            self._exchange_locked(
                f"MOVE,{direction},{normalized_speed:.3f}"
            )

            with self._state_lock:
                self.last_command_at = time.monotonic()
                self.current_motion = direction

    def drive(
        self,
        left_mps: float,
        right_mps: float,
    ) -> None:
        """
        좌우 바퀴 목표 속도(m/s)를 Pico의
        -1.0~1.0 정규화 PWM 명령으로 변환한다.
        """
        try:
            left = float(left_mps)
            right = float(right_mps)
        except (TypeError, ValueError) as exc:
            raise MotorControllerError(
                "잘못된 바퀴 속도 값: "
                f"left={left_mps}, right={right_mps}"
            ) from exc

        if (
            not math.isfinite(left)
            or not math.isfinite(right)
        ):
            raise MotorControllerError(
                "바퀴 속도에 NaN 또는 Infinity를 "
                "사용할 수 없습니다."
            )

        if abs(left) < 1e-6 and abs(right) < 1e-6:
            self.stop(reason="zero_drive")
            return

        left_normalized = left / self.max_wheel_mps
        right_normalized = right / self.max_wheel_mps

        peak = max(
            abs(left_normalized),
            abs(right_normalized),
        )

        if peak > 1.0:
            left_normalized /= peak
            right_normalized /= peak

        # Nav2의 작은 속도 명령이 실제 DC 모터의 정지 마찰보다
        # 낮은 PWM으로 변환되지 않도록 자동 주행에만 최소 출력을 둔다.
        # 좌우 바퀴를 개별 clamp하지 않고 같은 비율로 확대해
        # differential steering 비율을 보존한다.
        peak = max(
            abs(left_normalized),
            abs(right_normalized),
        )

        if 0.0 < peak < MIN_AUTO_DRIVE_PWM:
            pwm_scale = MIN_AUTO_DRIVE_PWM / peak
            left_normalized *= pwm_scale
            right_normalized *= pwm_scale

        with self._command_lock:
            self._ensure_connected_locked()

            self._exchange_locked(
                "DRIVE,"
                f"{left_normalized:.3f},"
                f"{right_normalized:.3f}"
            )

            with self._state_lock:
                self.last_command_at = time.monotonic()
                self.current_motion = (
                    f"drive:{left:.3f},{right:.3f}"
                )

    def stop(
        self,
        reason: str = "stop",
        suppress_errors: bool = False,
    ) -> None:
        error: Optional[Exception] = None

        with self._command_lock:
            with self._state_lock:
                self.current_motion = "stop"
                self.last_command_at = time.monotonic()

            try:
                self._ensure_connected_locked()

                self._exchange_locked(
                    f"STOP,{self._sanitize_reason(reason)}"
                )

            except Exception as exc:
                error = exc
                self._close_serial_locked()

        if error is not None:
            if suppress_errors:
                print(
                    f"[motor] stop 전달 실패 "
                    f"({reason}): {error}"
                )
                return

            raise MotorControllerError(
                str(error)
            ) from error

    def failsafe_tick(self) -> None:
        with self._state_lock:
            expired = (
                self.current_motion != "stop"
                and time.monotonic() - self.last_command_at
                > self.command_timeout_sec
            )

        if expired:
            print("[motor] command timeout -> stop")

            self.stop(
                reason="command_timeout",
                suppress_errors=True,
            )

    def close(self) -> None:
        with self._command_lock:
            if self.connected:
                try:
                    self._exchange_locked(
                        "STOP,controller_close"
                    )
                except Exception:
                    pass

                try:
                    self._exchange_locked(
                        "LED,0"
                    )
                    with self._state_lock:
                        self.led_enabled = False
                except Exception:
                    pass

                try:
                    self._exchange_locked(
                        "ENC_STREAM,0"
                    )
                except Exception:
                    pass

            self._close_serial_locked()
