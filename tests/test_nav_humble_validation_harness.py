"""Hardware-free tests for the explicit Humble build/regression gate."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/verify_pr90_humble.sh'


def test_humble_gate_shell_syntax():
    result = subprocess.run(['bash', '-n', str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_humble_gate_does_not_start_robot_or_nav2():
    text = SCRIPT.read_text()
    for unsafe in (
        'ros2 launch ', 'ros2 topic pub ', 'ros2 action send_goal ',
        'start_gpu_server.sh ', 'curl -X POST ',
    ):
        # Comments documenting what the script intentionally avoids do not
        # count as an executable shell command.
        lines = [line.strip() for line in text.splitlines() if line.strip()
                 and not line.lstrip().startswith('#')]
        assert not any(line.startswith(unsafe) for line in lines), unsafe
    assert 'colcon build --packages-select patrol_navigation_controller patrol_navigation' in text
    assert 'lib/libsegmented_rpp_controller.so' in text
    assert 'nav2_core::GlobalPlanner' in text
    assert 'nav2_core::Controller' in text


def test_humble_gate_reports_unsupported_host_before_writes():
    result = subprocess.run(
        ['bash', str(SCRIPT), '--preflight-only'], capture_output=True, text=True,
        timeout=10,
    )
    # On non-Humble machines it must fail safely; on an installed Humble
    # machine the preflight is allowed to pass but must not run colcon.
    if result.returncode != 0:
        assert any(x in result.stderr for x in (
            'Ubuntu 22.04', 'Humble', 'not installed', 'missing',
            'checkout', 'Unable', 'not found',
        ))
    assert 'Building Nav2' not in result.stdout
