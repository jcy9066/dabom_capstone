#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

import rclpy
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import LaserScan


class ScanMonitor:
    def __init__(self, topic: str):
        self.topic = topic
        self.last_scan_at: float | None = None
        self.scan_count = 0

    def callback(self, _msg: LaserScan) -> None:
        self.last_scan_at = time.monotonic()
        self.scan_count += 1


def terminate_group(process: subprocess.Popen, sig=signal.SIGINT) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 3.0
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 2.0
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", required=True)
    parser.add_argument("--startup-timeout", type=float, default=15.0)
    parser.add_argument("--stale-timeout", type=float, default=3.0)
    parser.add_argument("--restart-delay", type=float, default=1.0)
    parser.add_argument("launch_args", nargs=argparse.REMAINDER)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.launch_args:
        raise SystemExit("launch command is required")

    stop_requested = False
    current: subprocess.Popen | None = None

    def request_stop(_signum, _frame):
        nonlocal stop_requested
        stop_requested = True
        if current is not None:
            terminate_group(current, signal.SIGINT)

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    while not stop_requested:
        print(
            "[lidar-supervisor] starting driver: "
            + " ".join(args.launch_args),
            flush=True,
        )
        current = subprocess.Popen(
            args.launch_args,
            start_new_session=True,
        )

        rclpy.init()
        node = rclpy.create_node("dabom_lidar_runtime_watchdog")
        monitor = ScanMonitor(args.topic)
        subscription = node.create_subscription(
            LaserScan,
            args.topic,
            monitor.callback,
            qos_profile_sensor_data,
        )
        _ = subscription

        started_at = time.monotonic()
        restart_reason = None

        try:
            while not stop_requested:
                if current.poll() is not None:
                    restart_reason = f"driver_exit={current.returncode}"
                    break

                rclpy.spin_once(node, timeout_sec=0.25)
                now = time.monotonic()

                if monitor.last_scan_at is None:
                    if now - started_at > args.startup_timeout:
                        restart_reason = "startup_scan_timeout"
                        break
                    continue

                if now - monitor.last_scan_at > args.stale_timeout:
                    restart_reason = (
                        "scan_stale "
                        f"age={now - monitor.last_scan_at:.1f}s "
                        f"count={monitor.scan_count}"
                    )
                    break
        finally:
            node.destroy_node()
            if rclpy.ok():
                rclpy.shutdown()

        if stop_requested:
            break

        print(
            f"[lidar-supervisor] WARN: {restart_reason}; restarting LiDAR driver",
            flush=True,
        )
        terminate_group(current, signal.SIGINT)
        current = None
        time.sleep(max(0.1, args.restart_delay))

    if current is not None:
        terminate_group(current, signal.SIGINT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
