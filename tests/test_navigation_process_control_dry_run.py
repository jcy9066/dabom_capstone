import os
from unittest.mock import patch

import pytest

from server.navigation_process_control import NavigationProcessControl


@pytest.mark.parametrize(
    "motor_enabled,expect_disabled",
    [
        ("false", True),
        ("true", False),
    ],
)
def test_driving_launch_motor_safety(
    tmp_path, motor_enabled, expect_disabled
):
    control = NavigationProcessControl.__new__(
        NavigationProcessControl
    )
    control.root_dir = tmp_path
    control.log_dir = tmp_path / "logs"
    control.start_timeout_sec = 0.0

    environment = {
        "MOTOR_OUTPUT_ENABLED": motor_enabled,
        "SERVER_BASE_URL": "http://127.0.0.1:21063",
        "ROBOT_ID": "pi-01",
    }

    with patch.dict(os.environ, environment):
        with (
            patch.object(
                control,
                "_ros_command",
                side_effect=lambda command: command,
            ) as command_builder,
            patch(
                "server.navigation_process_control.subprocess.Popen"
            ),
            patch(
                "server.navigation_process_control.time.sleep"
            ),
        ):
            control._start_locked(
                "DRIVING",
                "/tmp/test_map.yaml",
            )

    command = command_builder.call_args.args[0]

    assert "map:=/tmp/test_map.yaml" in command
    assert (
        "start_nav2_command_bridge:=false" in command
    ) is expect_disabled
