"""Live navigation can only finish at the requested original Goal pose."""
from __future__ import annotations

import ast
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAV = ROOT / "server/navigation_control_api.py"


def _goal_guard():
    tree = ast.parse(NAV.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "NavigationControlApi")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                  and n.name == "_original_goal_pose_confirmed")
    module = ast.fix_missing_locations(ast.Module(
        body=[ast.ClassDef(name="OfflineApi", bases=[], keywords=[],
                           body=[method], decorator_list=[])],
        type_ignores=[]))
    env = {"math": math}
    exec(compile(module, str(NAV), "exec"), env)
    return env["OfflineApi"]._original_goal_pose_confirmed


def test_original_goal_pose_distance_and_yaw_must_match():
    valid = _goal_guard()
    requested = {"x": 1.0, "y": 0.0, "yaw": 0.0}
    now = 1000.0
    def pose(x, yaw=0.0, stamp=1000.0):
        return {"x": x, "y": 0.0, "yaw": yaw, "received_at": stamp}
    assert not valid(requested, pose(0.06), now=now)
    assert valid(requested, pose(0.55), now=now)
    assert not valid(requested, pose(0.95, yaw=1.0), now=now)
    assert valid(requested, pose(0.95, yaw=2 * math.pi), now=now)
    assert not valid(requested, {"x": 1.0, "y": 0.0}, now=now)
    assert not valid(requested, pose(float("nan")), now=now)


def test_goal_pose_freshness_rejects_stale_future_and_pre_stop_evidence():
    valid = _goal_guard()
    goal = {"x": 1.0, "y": 0.0, "yaw": 0.0}
    accepted = {"x": 1.0, "y": 0.0, "yaw": 0.0, "received_at": 100.2}
    kwargs = {"now": 100.3, "max_age_sec": 0.5, "min_received_at": 100.0}
    assert valid(goal, accepted, **kwargs)
    assert not valid(goal, {**accepted, "received_at": 99.9}, **kwargs)
    assert not valid(goal, {**accepted, "received_at": 100.31}, **kwargs)
    assert not valid(goal, {**accepted, "received_at": 99.0},
                     now=100.3, max_age_sec=0.5)
    assert not valid(goal, {k: v for k, v in accepted.items()
                            if k != "received_at"}, **kwargs)
    assert not valid(goal, {**accepted, "received_at": float("nan")}, **kwargs)
    assert not valid(goal, {**accepted, "received_at": "bad"}, **kwargs)
    # The Watchdog's stricter odometry threshold must win over 0.5 seconds.
    assert not valid(goal, accepted, now=100.5, max_age_sec=0.1,
                     min_received_at=100.0)


def test_live_motor_goal_success_requires_original_pose_evidence():
    source = NAV.read_text(encoding="utf-8")
    assert 'if self._stop_confirmed_locked(time.time()):' in source
    assert 'if self._motor_output_enabled and not self._original_goal_pose_confirmed(' in source
    assert 'max_age_sec=min(0.5, self._watchdog.odometry_timeout_sec)' in source
    assert 'min_received_at=self._stop_commanded_at' in source
    assert 'self._state["last_error"] = "ORIGINAL_GOAL_NOT_REACHED"' in source
    assert source.index('"ORIGINAL_GOAL_NOT_REACHED"') < source.index(
        'self._state["navigation_state"] = "SUCCEEDED"',
        source.index('async def _resolve_pending_nav_success(')
    )
