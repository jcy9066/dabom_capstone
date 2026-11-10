#!/usr/bin/env python3
"""Read-only Pi/GPU clock skew check. Run on the Pi while GPU FastAPI is up."""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from raspberry.clock_sync import probe_gpu_clock


def main() -> int:
    load_dotenv(ROOT / ".env")
    base_url = (os.getenv("SERVER_BASE_URL") or "").strip()
    token = (os.getenv("ROBOT_CONTROL_TOKEN") or "").strip()
    if not base_url or not token:
        print("FAIL: SERVER_BASE_URL or ROBOT_CONTROL_TOKEN missing", file=sys.stderr)
        return 2
    try:
        estimate = probe_gpu_clock(base_url, token, attempts=5)
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        f"GPU minus Pi clock: {estimate.offset_sec:+.3f}s "
        f"(network round trip {estimate.rtt_sec:.3f}s)"
    )
    if abs(estimate.offset_sec) >= 0.05:
        print("WARN: clock difference exceeds 50ms; keep OS NTP enabled")
    else:
        print("PASS: observed clock difference within 50ms")
    print("If LIDAR_USE_SOURCE_TIMESTAMP=true, the sender applies short-RTT correction.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
