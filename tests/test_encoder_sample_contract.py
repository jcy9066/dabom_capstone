"""Cross-file contracts for fresh independent encoder proof."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_scan_capture_includes_encoder_sequence():
    app = (ROOT/"server/app.py").read_text(encoding="utf-8")
    assert '"encoder_sequence": encoder_sequence' in app
    assert "return select_independent_encoder_scans(list(localization_scan_history))" in app
    assert "localization_scan_history.clear()" in app
    assert 'encoder_state["updated_at"] = None' in app


def test_duplicate_pi_encoder_packets_are_rejected():
    app = (ROOT/"server/app.py").read_text(encoding="utf-8")
    assert 'received_encoder["sequence"] <= encoder_state["sequence"]' in app
    fusion = (ROOT/"server/scan_pose_fusion.py").read_text(encoding="utf-8")
    assert "encoder_sequences[i] <= encoder_sequences[i - 1]" in fusion
    assert "encoder_timestamps[i] <= encoder_timestamps[i - 1]" in fusion
