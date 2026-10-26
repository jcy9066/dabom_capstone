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
    from raspberry.controllers.jerk_limiter import WheelJerkLimiter
    from raspberry.controllers.direction_interlock import DriveDirectionInterlock, segment_mode
except ModuleNotFoundError:
    from controllers.jerk_limiter import WheelJerkLimiter
    from controllers.direction_interlock import DriveDirectionInterlock, segment_mode

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
# Hard motor duty-cycle ceiling, independent of the .env breakaway setting.
# The Pico independently enforces this exact same physical limit.
MOTOR_MAX_PWM = 0.30
MIN_AUTO_DRIVE_PWM = min(
    env_float("MIN_AUTO_DRIVE_PWM", minimum=0.0, maximum=1.0),
    MOTOR_MAX_PWM,
)


def cap_motor_pwm_pair(left: float, right: float) -> tuple[float, float]:
    """Proportional peak clipping preserves differential steering curvature."""
    peak = max(abs(left), abs(right))
    if not math.isfinite(peak):
        raise MotorControllerError("Non-finite motor PWM command")
    if peak > MOTOR_MAX_PWM:
        scale = MOTOR_MAX_PWM / peak
        return left * scale, right * scale
    return left, right



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

        # Profile operates on final normalized wheel PWM, after breakaway
        # scaling. Emergency/STOP commands bypass the profile entirely.
        self._jerk_limiter = WheelJerkLimiter(
            max_accel=0.50 / self.max_wheel_mps,
            max_jerk=2.0 / self.max_wheel_mps,
        )
        self._direction_interlock = DriveDirectionInterlock()
        self._direction_fault_latched = False
        self.last_command_at = 0.0
        self.current_motion = "stop"
        self.led_enabled = False
        self.volume_percent: Optional[int] = None

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
                self.volume_percent = None

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
            self.volume_percent = None

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

    @staticmethod
    def _valid_uart_ack(command: str, response: str) -> bool:
        """Only the exact Pico ACK for this command proves execution.

        UART v1 has no command IDs; validating opcode and grammar is
        mandatory, though a delayed *same-opcode* ACK cannot be disambiguated
        without a future firmware protocol version.
        """
        parts = command.split(",")
        opcode = parts[0]
        exact = {
            "PING": "OK,PONG",
            "FW_INFO": "OK,FW,MOTOR_SAFE_V1",
            "STOP": "OK,STOP",
            "DRIVE": "OK,DRIVE",
            "ENC_RESET": "OK,ENC_RESET",
        }
        if opcode in exact:
            return response == exact[opcode]
        if opcode == "MOVE" and len(parts) == 3:
            return response == "OK,MOVE," + parts[1]
        if opcode == "ENC_STREAM" and len(parts) == 2:
            return response == "OK,ENC_STREAM," + parts[1]
        if opcode == "LED" and len(parts) == 2:
            return response == "OK,LED," + parts[1]
        if opcode == "BEEP" and len(parts) == 2:
            return response == "OK,BEEP," + parts[1]
        if opcode == "VOLUME_GET" and len(parts) == 1:
            fields = response.split(",")
            return (
                len(fields) == 3
                and fields[:2] == ["OK", "VOLUME"]
                and fields[2].isdigit()
                and 0 <= int(fields[2]) <= 100
            )
        if opcode == "VOLUME" and len(parts) == 2:
            return (
                parts[1].isdigit()
                and 0 <= int(parts[1]) <= 100
                and response == "OK,VOLUME," + parts[1]
            )
        if opcode == "ENC_GET":
            fields = response.split(",")
            return (
                len(fields) == 7 and fields[:2] == ["OK", "ENC"]
                and all(x.lstrip("-").isdigit() for x in fields[2:])
            )
        return False

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
                if response.startswith("ERR,"):
                    raise MotorControllerError(
                        f"Pico W 명령 거부: {response}"
                    )
                if not self._valid_uart_ack(command, response):
                    # On a mismatched response, the Pi cannot claim that
                    # the motor received STOP. Close the ambiguous UART
                    # session; reconnection begins with a new STOP.
                    self._close_serial_locked()
                    raise MotorControllerError(
                        f"Pico W ACK 불일치: command={command.split(',')[0]} "
                        f"response={response}"
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
            self._sync_optional_volume_locked()

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
            self._sync_optional_volume_locked()

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

    def _read_volume_locked(self) -> int:
        response = self._exchange_locked("VOLUME_GET")
        if not response.startswith("OK,VOLUME,"):
            raise MotorControllerError(f"Unexpected volume response: {response}")
        try:
            volume = int(response.split(",")[2])
        except (IndexError, ValueError) as exc:
            raise MotorControllerError(f"Invalid volume response: {response}") from exc
        if not 0 <= volume <= 100 or response != f"OK,VOLUME,{volume}":
            raise MotorControllerError(f"Invalid volume response: {response}")
        with self._state_lock:
            self.volume_percent = volume
        return volume

    def _sync_optional_volume_locked(self) -> None:
        """Support older Pico firmware without optional volume query."""
        try:
            self._read_volume_locked()
        except MotorControllerError as exc:
            if "ERR,unknown command" not in str(exc):
                raise
            with self._state_lock:
                self.volume_percent = None
            print(
                "[motor] WARN: Pico firmware does not support "
                "VOLUME_GET; encoder streaming remains enabled"
            )

    def get_volume(self) -> int:
        with self._command_lock:
            self._ensure_connected_locked()
            return self._read_volume_locked()

    def set_volume(self, percent: int) -> int:
        if type(percent) is not int or not 0 <= percent <= 100:
            raise MotorControllerError("volume percent must be an integer between 0 and 100")
        with self._command_lock:
            self._ensure_connected_locked()
            response = self._exchange_locked(f"VOLUME,{percent}")
            if response != f"OK,VOLUME,{percent}":
                raise MotorControllerError(f"Volume not confirmed by Pico W: {response}")
            with self._state_lock:
                self.volume_percent = percent
            return percent

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
            min(normalized_speed, MOTOR_MAX_PWM),
        )

        if normalized_speed <= 0.0:
            self.stop(reason="zero_speed")
            return

        with self._command_lock:
            self._ensure_connected_locked()
            # Manual MOVE remains independent from the autonomous velocity
            # profile. Resume auto drive from zero after manual takeover.
            self._jerk_limiter.reset()
            self._exchange_locked(
                f"MOVE,{direction},{normalized_speed:.3f}"
            )

            with self._state_lock:
                self.last_command_at = time.monotonic()
                self.current_motion = direction

    def _trace_motor_output(
        self, state: str, left_pwm: float = 0.0,
        right_pwm: float = 0.0, detail: str = "",
    ) -> None:
        """Bounded Pi-side diagnostics; does not authorize or send motion."""
        now = time.monotonic()
        previous_state = getattr(self, "_last_motor_trace_state", None)
        previous_at = getattr(self, "_last_motor_trace_at", None)
        if (
            previous_state == state
            and previous_at is not None
            and now - previous_at < 0.5
        ):
            return
        self._last_motor_trace_state = state
        self._last_motor_trace_at = now
        safe_detail = str(detail).replace(chr(10), " ").replace(chr(13), " ")[:80]
        try:
            print(
                f"[motor-motion] state={state} "
                f"left_pwm={left_pwm:.3f} right_pwm={right_pwm:.3f} "
                f"detail={safe_detail}",
                flush=True,
            )
        except (OSError, ValueError):
            # Diagnostics must never affect Pico motor command delivery.
            pass

    def drive(
        self,
        left_mps: float,
        right_mps: float,
        rear_blind_limited: bool = False,
        preserve_recorded_pwm: bool = False,
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

        # Recorded Replay already stores the user's manually applied PWM.
        # Boosting it to MIN_AUTO_DRIVE_PWM changes the physical recording,
        # so bypass only that optional min-output boost. All safety ceilings,
        # reversal interlocks and jerk/acceleration limits still apply.
        if not rear_blind_limited and not preserve_recorded_pwm and 0.0 < peak < MIN_AUTO_DRIVE_PWM:
            pwm_scale = MIN_AUTO_DRIVE_PWM / peak
            left_normalized *= pwm_scale
            right_normalized *= pwm_scale

        # Apply the 30% hard ceiling to BOTH normal and rear-blind driving.
        # Never raise output above this bound to overcome stiction.
        left_normalized, right_normalized = cap_motor_pwm_pair(
            left_normalized, right_normalized,
        )

        with self._command_lock:
            self._ensure_connected_locked()
            if self._direction_fault_latched:
                raise MotorControllerError(
                    "automatic direction switch timed out; motion locked until explicit safety reset"
                )
            now = time.monotonic()
            with self._state_lock:
                telemetry = dict(self._encoder_state)
            updated_at = telemetry.get("updated_monotonic")
            snapshot = {
                **telemetry,
                "age_sec": None if updated_at is None else now - updated_at,
            }
            mode = segment_mode(left_normalized, right_normalized)
            decision = self._direction_interlock.request(mode, now, snapshot)
            if decision == "stop":
                # One STOP per transition. Only a successful UART reply
                # starts the encoder-stable confirmation window.
                self._jerk_limiter.reset()
                try:
                    self._exchange_locked("STOP,direction_switch")
                except Exception:
                    self._direction_fault_latched = True
                    raise
                with self._state_lock:
                    sequence = int(self._encoder_state["sequence"])
                    self.current_motion = "stop"
                    self.last_command_at = time.monotonic()
                self._direction_interlock.mark_stop_sent(
                    time.monotonic(), sequence,
                )
                self._trace_motor_output("interlock_stop", detail=mode)
                return
            if decision == "hold":
                self._trace_motor_output("interlock_hold", detail=mode)
                return
            if decision == "timeout":
                self._jerk_limiter.reset()
                self._direction_fault_latched = True
                self._exchange_locked("STOP,direction_switch_timeout")
                with self._state_lock:
                    self.current_motion = "stop"
                    self.last_command_at = time.monotonic()
                self._trace_motor_output("interlock_timeout", detail=mode)
                raise MotorControllerError(
                    "direction change STOP not encoder-confirmed within 3s"
                )

            limited_left, limited_right = self._jerk_limiter.step(
                left_normalized, right_normalized, time.monotonic(),
            )
            # The jerk limiter can overshoot a new target during a sharp
            # reversal. Apply the physical ceiling *again* before UART.
            final_peak = max(abs(limited_left), abs(limited_right))
            limited_left, limited_right = cap_motor_pwm_pair(
                limited_left, limited_right,
            )
            if final_peak > MOTOR_MAX_PWM:
                # Clear the profile's pre-cap acceleration/target state.
                self._jerk_limiter.reset()
            try:
                self._exchange_locked(
                    "DRIVE,"
                    f"{limited_left:.3f},"
                    f"{limited_right:.3f}"
                )
                self._trace_motor_output(
                    "uart_drive", limited_left, limited_right,
                    "rear_limited" if rear_blind_limited else mode,
                )
            except Exception:
                self._jerk_limiter.reset()
                raise

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
            # STOP, failsafe and E-STOP have no acceleration/jerk delay.
            self._jerk_limiter.reset()
            self._direction_interlock.abort_pending()
            with self._state_lock:
                self.current_motion = "stop"
                self.last_command_at = time.monotonic()

            try:
                self._ensure_connected_locked()

                self._exchange_locked(
                    f"STOP,{self._sanitize_reason(reason)}"
                )
                self._trace_motor_output("uart_stop", detail=reason)

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

    def clear_direction_fault_after_safety_resume(self) -> None:
        """Re-arm only after operator/Pi safety-resume workflow completes."""
        with self._command_lock:
            self._direction_fault_latched = False
            self._direction_interlock.reset()
            self._jerk_limiter.reset()

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
