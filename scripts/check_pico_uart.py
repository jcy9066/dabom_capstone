#!/usr/bin/env python3
"""Safe, diagnostic-only Pico UART probe; never issues MOVE or DRIVE.

Run on the Raspberry Pi *after* stopping start_pi_stack.sh and any process
that owns the motor UART. Keep motor power disconnected.
"""
from pathlib import Path
import os
import subprocess
import sys
import time

import serial
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    cfg = dotenv_values(ROOT / ".env")
    port = (cfg.get("MOTOR_SERIAL_PORT") or "").strip()
    try:
        baudrate = int(cfg.get("MOTOR_SERIAL_BAUDRATE") or "0")
    except ValueError:
        baudrate = 0
    if not port or baudrate <= 0:
        print("FAIL: MOTOR_SERIAL_PORT/MOTOR_SERIAL_BAUDRATE not configured", file=sys.stderr)
        return 2
    if not os.path.exists(port):
        print(f"FAIL: serial port not found: {port}", file=sys.stderr)
        return 2

    # Do not probe a UART actively owned by the Pi stack: concurrent readers
    # can steal the STOP ACK and corrupt the live robot controller's UART state.
    try:
        check = subprocess.run(
            ["fuser", "-s", os.path.realpath(port)],
            capture_output=True, timeout=3, check=False,
        )
    except FileNotFoundError:
        print("FAIL: fuser is required to confirm exclusive UART access", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print("FAIL: UART ownership check timed out", file=sys.stderr)
        return 2
    if check.returncode == 0:
        print("FAIL: UART is busy; stop Pi stack and any serial monitor first", file=sys.stderr)
        return 2
    if check.returncode != 1:
        print(f"FAIL: UART ownership check error: {check.returncode}", file=sys.stderr)
        return 2

    print(f"PORT={port} RESOLVED={os.path.realpath(port)} BAUD={baudrate}", flush=True)
    print("No DRIVE/MOVE commands will be sent.", flush=True)
    try:
        with serial.Serial(port, baudrate, timeout=0.2, write_timeout=0.5) as device:
            def exchange(command: str, expected: str) -> bool:
                device.reset_input_buffer()
                data = (command + "\n").encode("ascii")
                print(f">> {command}", flush=True)
                device.write(data)
                device.flush()
                deadline = time.monotonic() + 3.0
                samples = 0
                while time.monotonic() < deadline:
                    raw = device.readline()
                    if not raw:
                        continue
                    samples += 1
                    line = raw.decode("ascii", errors="replace").strip()
                    print(f"<< {line!r}", flush=True)
                    if line == expected:
                        print(f"PASS: {command}", flush=True)
                        return True
                    if line.startswith("ERR,"):
                        print(f"FAIL: Pico returned error to {command}", file=sys.stderr)
                        return False
                print(
                    f"FAIL: {command}: expected {expected!r}, "
                    f"received {samples} nonempty line(s)",
                    file=sys.stderr,
                )
                return False

            # Require a positive STOP ACK before making any further queries.
            if not exchange("STOP,firmware_check", "OK,STOP"):
                return 1
            if not exchange("PING", "OK,PONG"):
                return 1
            if not exchange("FW_INFO", "OK,FW,MOTOR_SAFE_V1"):
                return 1
    except (OSError, ValueError, serial.SerialException) as exc:
        print(f"FAIL: serial transport: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("PICO UART CHECK: PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
