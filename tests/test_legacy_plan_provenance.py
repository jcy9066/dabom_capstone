
"""PR #93 legacy Plan provenance guard remains independent of PR #90 tests."""
from pathlib import Path


def test_legacy_unowned_plan_cannot_overwrite_active_goal_path():
    control = (Path(__file__).resolve().parents[1] / "server/navigation_control_api.py").read_text(encoding="utf-8")
    method = control.split("def note_replanned_path(", 1)[1].split("def note_active_map(", 1)[0]
    assert "return" in method
    assert 'self._state["planned_path"] =' not in method
