"""Unit tests for best-effort, non-blocking GPU server telemetry."""
import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server.gpu_telemetry import GpuUtilizationSampler


class GpuUtilizationSamplerTests(unittest.TestCase):
    def make_sample(self, output, returncode=0):
        sampler = GpuUtilizationSampler()
        sampler._gpu_index = "0"
        sampler._next_refresh_at = float("inf")  # Do not launch live GPU probes.
        response = SimpleNamespace(returncode=returncode, stdout=output)
        with patch("server.gpu_telemetry.subprocess.run", return_value=response) as run:
            sampler._sample("0")
        run.assert_called_once()
        args = run.call_args.args[0]
        self.assertIn("--query-gpu=utilization.gpu", args)
        self.assertIn("--id=0", args)
        self.assertFalse(run.call_args.kwargs["check"])
        self.assertIsNotNone(run.call_args.kwargs["timeout"])
        return sampler.snapshot("0")

    def test_valid_usage_values(self):
        for output, expected in (("0\n", 0), ("37\n", 37), ("100\n", 100)):
            with self.subTest(output=output):
                self.assertEqual(self.make_sample(output), expected)

    def test_invalid_and_failed_readings_are_unavailable(self):
        for output, code in (
            ("", 0), ("N/A", 0), ("-1", 0), ("101", 0),
            ("37\n42\n", 0), ("37", 1),
        ):
            with self.subTest(output=output, code=code):
                self.assertIsNone(self.make_sample(output, code))

    def test_timeout_and_missing_binary_are_unavailable(self):
        for error in (
            FileNotFoundError("nvidia-smi"),
            subprocess.TimeoutExpired("nvidia-smi", 1.5),
        ):
            with self.subTest(error=type(error).__name__):
                sampler = GpuUtilizationSampler()
                sampler._gpu_index = "0"
                sampler._next_refresh_at = float("inf")
                with patch("server.gpu_telemetry.subprocess.run", side_effect=error):
                    sampler._sample("0")
                self.assertIsNone(sampler.snapshot("0"))

    def test_snapshot_never_waits_for_gpu_probe(self):
        sampler = GpuUtilizationSampler()
        with patch("server.gpu_telemetry.threading.Thread") as thread:
            self.assertIsNone(sampler.snapshot("0"))
            self.assertIsNone(sampler.snapshot("0"))
            thread.assert_called_once()
            thread.return_value.start.assert_called_once()

    def test_stale_values_and_gpu_switch(self):
        sampler = GpuUtilizationSampler()
        sampler._gpu_index = "0"
        sampler._value = 55
        sampler._sampled_at = time.monotonic() - 10
        sampler._next_refresh_at = float("inf")
        self.assertIsNone(sampler.snapshot("0"), "stale GPU data must not display")
        with patch("server.gpu_telemetry.threading.Thread") as thread:
            self.assertIsNone(sampler.snapshot("1"), "GPU selection clears old sample")
            thread.assert_called_once()
            with patch("server.gpu_telemetry.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="77\
")):
                sampler._sample("0")  # Late old-device result must not overwrite new GPU.
            self.assertIsNone(sampler.snapshot("1"))


if __name__ == "__main__":
    unittest.main()
