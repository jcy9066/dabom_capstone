---
name: validate-raspberry
description: Validate Raspberry Pi 3B/Pico/UART/motor/encoder/failsafe and Pi runtime configuration paths.
---

# Validate Raspberry

Read `references/uart_protocol.md`.

Current Raspberry Pi target:

```text
Raspberry Pi 3 Model B (1GB)
Ubuntu Server 22.04 arm64
Wi-Fi: 2.4GHz
UART: /dev/serial0 -> PL011 ttyAMA0
GPIO14 TX / GPIO15 RX
115200 8N1
```

Current Pi runtime entry point:

```text
start_pi_stack.sh
```

## Procedure

1. Delegate the current Pi/Pico/runtime-config and safety diff to `hardware_reviewer`; reuse a matching result.
2. If Pi/Pico UART code changed, compare both sides of each changed command/event, baudrate, framing, timeout and failsafe contract.
3. If `start_pi_stack.sh` exists or changed, verify the final runtime contract:
   - project root is resolved from the script location
   - `.env` is loaded from project root
   - Pico and LiDAR serial ports cannot be identical
   - Pico UART preflight performs `PING` and safe `STOP` before starting the robot client
   - legacy `dabom-command.service` cannot keep a duplicate robot client alive
   - same-role Robot/LiDAR/Camera processes are stopped before restart
   - LiDAR is not declared ready until a real `LaserScan` is received
   - camera hardware is detected before streaming starts
   - GPU absence is treated as reconnect/waiting state rather than a local hardware failure
   - owned children are stopped on supervisor shutdown
4. Run static shell validation when Bash is available:
   `bash -n start_pi_stack.sh`
5. Run protocol validation:
   `python .agents/skills/validate-raspberry/scripts/validate_uart_protocol.py`
6. Run affected Python/tests in quiet/RTK mode when available.
7. Build Pico SDK only when the toolchain is available and the firmware changed.
8. Separate static/build/mock results from real Pi 3B/UART/hardware results.
9. Only when the current environment is the actual Pi 3B and the user explicitly requests device validation, execute `start_pi_stack.sh`; note that it performs real hardware preflight and replaces existing runtime processes.

## Context Budget

Use targeted search/partial reads. Do not return full firmware/source/build logs or successful
test output. Return protocol mismatches, Pi 3B configuration mismatches, safety issues,
evidence, and skipped hardware checks.

## Restrictions

- Do not execute `start_pi_stack.sh` automatically on a development machine or unapproved Pi. It interacts with real UART, camera, LiDAR and Pico W and stops same-role runtime processes.
- No real serial movement, GPIO output, motor/speaker drive, firmware flash, OpenOCD,
  Git remote/history mutation, or `test_reviewer` delegation here.
- Do not claim `/dev/serial0 -> ttyAMA0`, camera detection, ROS runtime,
  Pico response, or connected LiDAR as PASS unless verified on the actual Pi 3B.
