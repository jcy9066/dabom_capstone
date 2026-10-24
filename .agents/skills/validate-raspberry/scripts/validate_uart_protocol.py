from __future__ import annotations

import argparse
import ast
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str


@dataclass(frozen=True)
class RuntimeResult:
    status: str
    message: str
    output: str = ""


PI_TO_PICO_REQUIRED = (
    "PING",
    "MOVE",
    "DRIVE",
    "STOP",
    "ENC_RESET",
    "ENC_STREAM",
    "LED",
    "BEEP",
)

PICO_EXTRA = ("ENC_GET",)

PICO_TO_PI_REQUIRED = (
    "OK,",
    "ERR,",
    "EVENT,FAILSAFE_STOP",
    "EVENT,ENC",
    "READY,PICO_W_MOTOR_ENCODER",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Raspberry Pi ↔ Pico W UART protocol."
    )
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument(
        "--serial-port",
        default=None,
        help=(
            "Optional real UART port. When omitted, only static validation runs. "
            "Examples: /dev/serial0, COM4"
        ),
    )
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--runtime-timeout-sec", type=float, default=1.0)
    parser.add_argument("--show-runtime-output", action="store_true")
    parser.add_argument("--strict-warnings", action="store_true")
    return parser.parse_args()


def _is_repo_root(path: Path) -> bool:
    return (
        (path / "raspberry" / "controllers" / "motor_controller.py").is_file()
        and (path / "raspberry" / "pico_w_sdk" / "main.c").is_file()
    )


def find_repo_root(explicit: Path | None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit.expanduser())
    candidates.append(Path.cwd())
    try:
        candidates.extend(Path(__file__).resolve().parents)
    except OSError:
        pass

    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        for current in (resolved, *resolved.parents):
            key = os.path.normcase(str(current))
            if key in seen:
                continue
            seen.add(key)
            if _is_repo_root(current):
                return current

    raise RuntimeError(
        "dabom repository root를 찾지 못했습니다. "
        "--repo-root로 repository root를 지정하세요."
    )


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def add(findings: list[Finding], severity: str, code: str, message: str) -> None:
    findings.append(Finding(severity, code, message))


def require_tokens(
    source: str,
    tokens: tuple[str, ...],
    findings: list[Finding],
    code: str,
    subject: str,
    severity: str = "ERROR",
) -> None:
    for token in tokens:
        if token not in source:
            add(
                findings,
                severity,
                code,
                f"{subject}에서 확인하지 못했습니다: {token}",
            )


def check_required_files(root: Path, findings: list[Finding]) -> None:
    required = (
        "raspberry/controllers/motor_controller.py",
        "raspberry/pico_w_sdk/main.c",
        "raspberry/pico_w_sdk/CMakeLists.txt",
    )
    for relative in required:
        if not (root / relative).is_file():
            add(findings, "ERROR", "required-file", f"필수 파일이 없습니다: {relative}")


def check_python_syntax(root: Path, findings: list[Finding]) -> None:
    for path in (
        root / "raspberry" / "controllers" / "motor_controller.py",
        root / "raspberry" / "robot_command_client.py",
    ):
        if not path.is_file():
            continue
        try:
            ast.parse(read_text(path), filename=str(path))
        except SyntaxError as exc:
            add(
                findings,
                "ERROR",
                "python-syntax",
                f"{path}:{exc.lineno}:{exc.offset} {exc.msg}",
            )


def check_pi_controller(root: Path, findings: list[Finding]) -> None:
    path = root / "raspberry" / "controllers" / "motor_controller.py"
    if not path.is_file():
        return
    source = read_text(path)

    require_tokens(
        source,
        (
            'serial_port: str = "/dev/serial0"',
            "baudrate: int = 115200",
            "command_timeout_sec: float = 0.45",
            "serial_timeout_sec: float = 0.25",
            'env_float("MAX_WHEEL_MPS", minimum=0.01)',
            "_reader_loop",
            "_response_queue",
            "EVENT,ENC,",
            "EVENT,FAILSAFE_STOP",
            'line.startswith("READY,")',
            'self._exchange_locked("PING")',
            'self._exchange_locked("STOP,pi_connected")',
            'self._exchange_locked("ENC_STREAM,1")',
            'self._exchange_locked("STOP,pico_reboot_recovery")',
        ),
        findings,
        "pi-controller-contract",
        "motor_controller.py",
    )

    for command in PI_TO_PICO_REQUIRED:
        if command not in source:
            add(findings, "ERROR", "pi-command", f"Pi controller command 누락: {command}")

    if source.count(".readline()") != 1:
        add(
            findings,
            "WARN",
            "single-reader",
            (
                "motor_controller.py의 readline() 호출 수가 1이 아닙니다. "
                "단일 UART reader 구조가 유지되는지 확인하세요."
            ),
        )


def check_pico_sdk(root: Path, findings: list[Finding]) -> None:
    path = root / "raspberry" / "pico_w_sdk" / "main.c"
    if not path.is_file():
        return
    source = read_text(path)

    require_tokens(
        source,
        (
            "#define CONTROL_UART uart0",
            "#define UART_BAUDRATE 115200",
            "#define UART_TX_PIN 0",
            "#define UART_RX_PIN 1",
            "#define LEFT_PWM_PIN 2",
            "#define LEFT_DIR_PIN 3",
            "#define RIGHT_PWM_PIN 4",
            "#define RIGHT_DIR_PIN 5",
            "#define LEFT_FRONT_ENCODER_A_PIN 6",
            "#define LEFT_FRONT_ENCODER_B_PIN 8",
            "#define RIGHT_FRONT_ENCODER_A_PIN 10",
            "#define RIGHT_FRONT_ENCODER_B_PIN 11",
            "#define LEFT_REAR_ENCODER_A_PIN 12",
            "#define LEFT_REAR_ENCODER_B_PIN 13",
            "#define RIGHT_REAR_ENCODER_A_PIN 14",
            "#define RIGHT_REAR_ENCODER_B_PIN 15",
            "#define SPEAKER_PIN 16",
            "#define MOSFET_PIN 20",
            "#define ENCODER_REPORT_INTERVAL_MS 50U",
            "#define COMMAND_TIMEOUT_MS 350U",
            "#define BEEP_MIN_DURATION_MS 50U",
            "#define BEEP_MAX_DURATION_MS 2000U",
            "#define RX_BUFFER_SIZE 128U",
            "#define LEFT_FRONT_ENCODER_SIGN 1",
            "#define RIGHT_FRONT_ENCODER_SIGN -1",
            "#define LEFT_REAR_ENCODER_SIGN 1",
            "#define RIGHT_REAR_ENCODER_SIGN -1",
            'uart_reply("EVENT,FAILSAFE_STOP")',
            'send_encoder_response("EVENT,ENC")',
            '"READY,PICO_W_MOTOR_ENCODER"',
            '"ERR,receive buffer overflow"',
            '"ERR,unknown command"',
            'speaker_tick(now_ms)',
            '"OK,LED,1"',
            '"OK,BEEP,%lu"',
        ),
        findings,
        "pico-sdk-contract",
        "pico_w_sdk/main.c",
    )

    for command in PI_TO_PICO_REQUIRED + PICO_EXTRA:
        if f'"{command}"' not in source:
            add(findings, "ERROR", "pico-command", f"Pico SDK command 누락: {command}")

    for marker in PICO_TO_PI_REQUIRED:
        if marker not in source:
            add(findings, "ERROR", "pico-response", f"Pico SDK response/event 누락: {marker}")


def check_protocol_alignment(root: Path, findings: list[Finding]) -> None:
    pi_path = root / "raspberry" / "controllers" / "motor_controller.py"
    pico_path = root / "raspberry" / "pico_w_sdk" / "main.c"
    if not pi_path.is_file() or not pico_path.is_file():
        return

    pi = read_text(pi_path)
    pico = read_text(pico_path)

    for command in PI_TO_PICO_REQUIRED:
        if command in pi and f'"{command}"' not in pico:
            add(
                findings,
                "ERROR",
                "protocol-mismatch",
                f"Pi는 {command}를 사용하지만 Pico SDK parser에서 찾지 못했습니다.",
            )

    pi_baud = re.search(r"baudrate: int = (\d+)", pi)
    pico_baud = re.search(r"#define UART_BAUDRATE (\d+)", pico)
    if pi_baud and pico_baud and pi_baud.group(1) != pico_baud.group(1):
        add(
            findings,
            "ERROR",
            "baudrate-mismatch",
            f"Pi/Pico baudrate 불일치: {pi_baud.group(1)} vs {pico_baud.group(1)}",
        )


def run_runtime(
    serial_port: str | None,
    baudrate: int,
    timeout_sec: float,
) -> RuntimeResult:
    if not serial_port:
        return RuntimeResult(
            "SKIP",
            "--serial-port를 지정하지 않아 실제 UART 검증을 생략했습니다.",
        )

    try:
        import serial  # type: ignore
    except ImportError:
        return RuntimeResult(
            "SKIP",
            "현재 Python 환경에 pyserial이 없어 실제 UART 검증을 생략했습니다.",
        )

    output: list[str] = []
    device = None
    try:
        device = serial.Serial(
            port=serial_port,
            baudrate=int(baudrate),
            timeout=max(0.1, float(timeout_sec)),
            write_timeout=max(0.1, float(timeout_sec)),
        )
        device.reset_input_buffer()
        device.reset_output_buffer()

        # 실제 모터가 연결되어 있을 수 있으므로 먼저 STOP을 보낸다.
        for command in ("STOP,validator", "PING"):
            device.write((command + "\n").encode("ascii"))
            device.flush()
            response = device.readline().decode("ascii", errors="replace").strip()
            output.append(f"> {command}\n< {response}")
            if not response.startswith("OK"):
                return RuntimeResult(
                    "ERROR",
                    f"UART 응답이 예상과 다릅니다: {command}",
                    "\n".join(output),
                )
            time.sleep(0.02)

        return RuntimeResult(
            "PASS",
            f"{serial_port}에서 STOP/PING UART 검증을 통과했습니다.",
            "\n".join(output),
        )
    except Exception as exc:
        return RuntimeResult(
            "ERROR",
            f"실제 UART 검증 실패: {exc}",
            "\n".join(output),
        )
    finally:
        if device is not None:
            try:
                device.write(b"STOP,validator_close\n")
                device.flush()
            except Exception:
                pass
            try:
                device.close()
            except Exception:
                pass


def main() -> int:
    args = parse_args()
    try:
        root = find_repo_root(args.repo_root)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    findings: list[Finding] = []
    check_required_files(root, findings)
    check_python_syntax(root, findings)
    check_pi_controller(root, findings)
    check_pico_sdk(root, findings)
    check_protocol_alignment(root, findings)

    runtime = run_runtime(
        args.serial_port,
        args.baudrate,
        args.runtime_timeout_sec,
    )

    print(f"dabom raspberry UART validation: {root}")
    print("-" * 72)
    if findings:
        for finding in findings:
            print(f"[{finding.severity:<5}] {finding.code}: {finding.message}")
    else:
        print("[PASS ] static checks: no findings")
    print("-" * 72)
    print(f"[{runtime.status:<5}] runtime: {runtime.message}")
    if args.show_runtime_output and runtime.output.strip():
        print("-" * 72)
        print(runtime.output.rstrip())

    errors = sum(1 for item in findings if item.severity == "ERROR")
    warnings = sum(1 for item in findings if item.severity == "WARN")
    if runtime.status == "ERROR":
        errors += 1
    elif runtime.status == "WARN":
        warnings += 1

    print("-" * 72)
    print(f"summary: errors={errors}, warnings={warnings}, runtime={runtime.status.lower()}")

    if errors:
        return 1
    if args.strict_warnings and warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
