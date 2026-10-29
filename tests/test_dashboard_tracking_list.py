"""Run live Object List regression checks with the existing Node test runtime."""
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_tracking_list_regressions():
    completed = subprocess.run(
        ["node", "tests/test_dashboard_tracking_list.js"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
