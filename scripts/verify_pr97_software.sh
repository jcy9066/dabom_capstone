#!/usr/bin/env bash
# PR #97 software-only validation. Does not start ROS2, Pi, Pico or motors.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
"$PYTHON" -m compileall -q server raspberry navigation/ros/patrol_navigation tests
bash -n start_gpu_server.sh start_pi_stack.sh
if ! command -v node >/dev/null 2>&1; then
  echo "Node.js is required for JavaScript contract verification" >&2
  exit 1
fi
node --check frontend/services/static/script.js
node --check frontend/services/static/navigation_control.js
node --check frontend/services/static/recorded_loop_ui.js
node --check frontend/components/navigation/saved_map_modal.js
node --input-type=module --check < frontend/services/static/lidar_3d_viewer.js
for test in \
  tests/js/test_mapping_scan_time_pairing.js \
  tests/js/test_mapping_ws_recovery.js \
  tests/js/test_initial_pose_ui_contract.js \
  tests/js/test_manual_loop_ui_contract.js; do
  node "$test"
done
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$PYTHON" -m pytest -q \
  tests/test_mapping_health.py tests/test_map_storage_integrity.py \
  tests/test_live_mapping_grid.py tests/test_navigation_mapping_recovery.py \
  tests/test_initial_pose_safety.py tests/test_initial_pose_map_validation.py \
  tests/test_localization_observation.py tests/test_localization_rotation.py \
  tests/test_localization_rotation_controller.py \
  tests/test_amcl_pose_stability_contract.py tests/test_auto_pose_motion_contract.py \
  tests/test_navigation_control_api.py tests/test_loop_motor_ownership_contract.py \
  tests/test_loop_session_api.py tests/test_loop_deadline_regression.py \
  tests/test_manual_loop_segments.py tests/test_manual_loop_pi_pulse.py \
  --tb=short
if [[ "${1:-}" == "--full" ]]; then
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$PYTHON" -m pytest -q tests --tb=short
fi
echo "PR97 software validation PASS; actual ROS2/Pi/motor validation still required"
