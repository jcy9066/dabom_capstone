"""If a robot moves during localization, reject the previously matched pose."""
from pathlib import Path


def test_auto_pose_rechecks_encoder_after_matching_and_before_pi_driving():
    control = (Path(__file__).resolve().parents[1] / "server/navigation_control_api.py").read_text(encoding="utf-8")
    # First check after matching, then again before Pi DRIVING transition.
    assert "baseline_ticks = self._encoder_ticks" in control
    assert "self._encoder_ticks != baseline_ticks" in control
    assert "self._encoder_ticks != auto_pose_encoder_baseline" in control
    assert "await self._estimate_initial_pose_with_retry(" in control
    assert "AUTO_POSE_MOVED" in control
    assert '"CANCELING_STOP"' in control
    assert "or self._state[\"emergency_stop\"]" in control


def test_encoder_stationarity_is_checked_before_scan_fusion():
    fusion = (Path(__file__).resolve().parents[1] / "server/scan_pose_fusion.py").read_text(encoding="utf-8")
    assert "if max(values) != min(values):" in fusion
    assert "Wheel encoder movement during localization" in fusion
