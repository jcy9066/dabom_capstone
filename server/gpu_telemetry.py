"""Best-effort NVIDIA GPU utilization for dashboard display.

Never block the /get_status request on nvidia-smi. The last sampled value
is only reported while fresh; unavailable/invalid readings remain None.
"""
import subprocess
import threading
import time


class GpuUtilizationSampler:
    def __init__(self, interval_seconds=2.0, timeout_seconds=1.5):
        self._interval = interval_seconds
        self._timeout = timeout_seconds
        self._lock = threading.Lock()
        self._gpu_index = None
        self._value = None
        self._sampled_at = None
        self._next_refresh_at = 0.0
        self._in_flight = False

    def snapshot(self, gpu_index="0"):
        index = str(gpu_index if gpu_index is not None else "0").strip() or "0"
        now = time.monotonic()
        with self._lock:
            if index != self._gpu_index:
                self._gpu_index = index
                self._value = None
                self._sampled_at = None
                self._next_refresh_at = 0.0
                self._in_flight = False
            if not self._in_flight and now >= self._next_refresh_at:
                self._in_flight = True
                self._next_refresh_at = now + self._interval
                try:
                    threading.Thread(
                        target=self._sample,
                        args=(index,),
                        daemon=True,
                        name="gpu-utilization-sampler",
                    ).start()
                except RuntimeError:
                    self._in_flight = False
            if (self._sampled_at is None
                    or now - self._sampled_at > self._interval * 2 + self._timeout):
                return None
            return self._value

    def _sample(self, index):
        value = None
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu",
                    "--format=csv,noheader,nounits",
                    "--id=" + index,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout,
            )
            if result.returncode == 0:
                lines = result.stdout.strip().splitlines()
                if len(lines) == 1 and lines[0].strip().isdigit():
                    parsed = int(lines[0].strip())
                    if 0 <= parsed <= 100:
                        value = parsed
        except Exception:
            # Optional NVIDIA telemetry must never disrupt dashboard status.
            pass

        with self._lock:
            if index == self._gpu_index:
                self._value = value
                self._sampled_at = time.monotonic()
                self._in_flight = False
