"""A replaced Pi socket must not inherit the prior encoder sequence."""
from pathlib import Path


def test_fresh_pi_connection_rearms_encoder_sequence():
    control = (Path(__file__).resolve().parents[1] / "server/navigation_control_api.py").read_text(encoding="utf-8")
    start = control.index("    def note_pi_connection(")
    end = control.index("    def note_pi_status(", start)
    body = control[start:end]
    assert body.index("self._encoder_sequence = None") < body.index("if not connected:")
    assert "self._pi_updated_at = None" in body
