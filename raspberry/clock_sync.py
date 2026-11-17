"""Measure the GPU clock against the Pi without changing either OS clock.

A short round trip is used as an NTP-style approximation. This is a transport
timestamp correction, not a replacement for NTP/chrony on the two hosts.
"""
from dataclasses import dataclass
import math
import time

import requests


@dataclass(frozen=True)
class ClockEstimate:
    offset_sec: float  # GPU UTC minus Pi UTC
    rtt_sec: float


def probe_gpu_clock(base_url, token, *, attempts=3, timeout=0.65,
                    request_get=None, wall_clock=None, steady_clock=None):
    """Return the valid sample with the lowest RTT or raise RuntimeError.

    No timestamps or tokens are logged or persisted. A clock step during an
    individual exchange invalidates that sample.
    """
    request_get = request_get or requests.get
    wall_clock = wall_clock or time.time
    steady_clock = steady_clock or time.monotonic
    url = base_url.rstrip("/") + "/api/time/sync"
    headers = {"X-Robot-Control-Token": token}
    best = None

    for _ in range(attempts):
        try:
            pi_start = wall_clock()
            mono_start = steady_clock()
            response = request_get(url, headers=headers, timeout=timeout)
            mono_end = steady_clock()
            pi_end = wall_clock()
            response.raise_for_status()
            server_time = float(response.json()["server_time_sec"])
            rtt = mono_end - mono_start
            wall_elapsed = pi_end - pi_start
            offset = server_time - (pi_start + pi_end) / 2.0

            if not all(math.isfinite(x) for x in (server_time, rtt, wall_elapsed, offset)):
                continue
            if not (0.0 <= rtt <= 0.30):
                continue
            if abs(wall_elapsed - rtt) > 0.05:
                continue  # local clock jumped while probing
            if abs(offset) > 300.0:
                continue  # grossly invalid OS time requires host-level repair

            estimate = ClockEstimate(offset_sec=offset, rtt_sec=rtt)
            if best is None or estimate.rtt_sec < best.rtt_sec:
                best = estimate
        except (requests.RequestException, ValueError, KeyError, TypeError):
            continue

    if best is None:
        raise RuntimeError("GPU/Pi clock probe unavailable or RTT too high")
    return best
