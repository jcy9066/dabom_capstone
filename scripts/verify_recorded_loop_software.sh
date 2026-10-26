#!/usr/bin/env bash
# Recorded Loop Hotfix / PR #93 regression. Does not actuate motors.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"
"$PYTHON" -m py_compile \
  server/recorded_loop_api.py server/recorded_loop_core.py \
  server/recorded_loop_runtime.py server/recorded_loop_executor.py \
  server/recorded_loop_motor_math.py server/recorded_loop_obstacle.py \
  server/navigation_control_api.py server/navigation_ros_control.py server/app.py \
  raspberry/controllers/motor_controller.py raspberry/robot_command_client.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$PYTHON" -m pytest -q \
  tests/test_recorded_loop_core.py tests/test_recorded_loop_runtime.py \
  tests/test_recorded_loop_api.py tests/test_recorded_loop_executor.py \
  tests/test_recorded_loop_safety_gates.py tests/test_recorded_loop_nav2_handoff.py \
  tests/test_recorded_loop_activation_contract.py tests/test_recorded_loop_integrity.py \
  tests/test_recorded_loop_late_nav2_action.py \
  tests/test_navigation_control_api.py tests/test_ab_loop_contract.py --tb=short
if command -v node >/dev/null 2>&1; then
  node --check frontend/services/static/recorded_loop_ui.js
  node --check frontend/services/static/navigation_control.js
  node tests/js/test_recorded_loop_ui.js
  node --input-type=module --check < frontend/services/static/lidar_3d_viewer.js
else
  echo "Node.js unavailable: JS verification incomplete" >&2
  exit 1
fi
echo "Software checks passed. Pi/ROS2/obstacle hardware verification REQUIRED."
