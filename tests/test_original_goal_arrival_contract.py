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
    assert not valid(requested, {"x": 0.06, "y": 0, "yaw": 0.0})
    assert valid(requested, {"x": 0.55, "y": 0, "yaw": 0.0})
    assert not valid(requested, {"x": 0.95, "y": 0, "yaw": 1.0})
    assert valid(requested, {"x": 0.95, "y": 0, "yaw": 2 * math.pi})
    assert not valid(requested, {"x": 1.0, "y": 0.0})
    assert not valid(requested, {"x": float("nan"), "y": 0, "yaw": 0.0})


def test_live_motor_goal_success_requires_original_pose_evidence():
    source = NAV.read_text(encoding="utf-8")
    assert 'if self._stop_confirmed_locked(time.time()):' in source
    assert 'if self._motor_output_enabled and not self._original_goal_pose_confirmed(' in source
    assert 'self._state["last_error"] = "ORIGINAL_GOAL_NOT_REACHED"' in source
    assert source.index('"ORIGINAL_GOAL_NOT_REACHED"') < source.index(
        'self._state["navigation_state"] = "SUCCEEDED"',
        source.index('async def _resolve_pending_nav_success(')
    )
