"""Recorded route referential-integrity regression, no live robot."""
import tempfile
from pathlib import Path
import unittest

from server.recorded_loop_core import RecordedLoopError, RecordedRouteStore


class RouteIntegrityTests(unittest.TestCase):
    def test_route_protects_goal_and_segment(self):
        with tempfile.TemporaryDirectory() as dirname:
            registry = RecordedRouteStore(Path(dirname) / "routes.json")
            digest = "abc"
            start = {"x": 0.0, "y": 0.0, "yaw": 0.0}
            end = {"x": 0.6, "y": 0.0, "yaw": 0.0}
            registry.put_goal("lab.v1", digest, "A", start)
            registry.put_goal("lab.v1", digest, "B", end)
            trace = [start, {"x": 0.3, "y": 0.0, "yaw": 0.0}, end]
            backward = list(reversed(trace))
            cmd = [{"direction": "forward", "speed": 0.2, "duration": 0.2}]
            registry.put_segment("lab.v1", digest, "AB", "manual", "A", "B", cmd, trace)
            registry.put_segment("lab.v1", digest, "BA", "manual", "B", "A", cmd, backward)
            registry.put_route("lab.v1", digest, "loop", ["AB", "BA"])
            with self.assertRaises(RecordedLoopError) as goal_error:
                registry.put_goal("lab.v1", digest, "A", {
                    "x": 0.1, "y": 0.0, "yaw": 0.0})
            self.assertEqual(goal_error.exception.code, "GOAL_IN_USE")
            with self.assertRaises(RecordedLoopError) as segment_error:
                registry.put_segment("lab.v1", digest, "AB", "manual", "A", "B", cmd, trace)
            self.assertEqual(segment_error.exception.code, "SEGMENT_IN_USE")
            self.assertEqual(registry.snapshot("lab.v1", digest)["goals"]["A"], start)

    def test_reject_missing_turnaround_in_route(self):
        with tempfile.TemporaryDirectory() as dirname:
            registry = RecordedRouteStore(Path(dirname) / "routes.json")
            digest = "abc"
            a = {"x": 0.0, "y": 0.0, "yaw": 0.0}
            b = {"x": 0.6, "y": 0.0, "yaw": 0.0}
            registry.put_goal("lab.v1", digest, "A", a)
            registry.put_goal("lab.v1", digest, "B", b)
            cmd = [{"direction": "forward", "speed": 0.2, "duration": 0.2}]
            registry.put_segment("lab.v1", digest, "AB", "manual", "A", "B", cmd,
                                 [a, {"x": 0.3, "y": 0.0, "yaw": 0.0}, b])
            backward_heading = [
                {"x": 0.6, "y": 0.0, "yaw": 3.14},
                {"x": 0.3, "y": 0.0, "yaw": 3.14},
                {"x": 0.0, "y": 0.0, "yaw": 3.14},
            ]
            registry.put_segment("lab.v1", digest, "BA", "manual", "B", "A",
                                 cmd, backward_heading)
            with self.assertRaises(RecordedLoopError) as exc:
                registry.put_route("lab.v1", digest, "loop", ["AB", "BA"])
            self.assertEqual(exc.exception.code, "ROUTE_POSE_DISCONNECTED")
