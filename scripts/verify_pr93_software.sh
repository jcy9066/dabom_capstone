#!/usr/bin/env bash
# PR #93 offline/ROS2 software verification. No GitHub Actions or motor commands.
# Usage: bash scripts/verify_pr93_software.sh [--python-only]
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON_ONLY="${1:-}"
echo "[1/4] Compile Python navigation modules"
python3 -m py_compile server/navigation_control_api.py server/navigation_ros_control.py

echo "[2/4] Check dashboard JavaScript syntax"
node --check frontend/services/static/navigation_control.js
node --input-type=module --check < frontend/services/static/lidar_3d_viewer.js

echo "[3/4] Execute navigation and A/B cache regression tests"
PYTHONPATH="$ROOT" PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python3 -m pytest -q \
    tests/test_navigation_control_api.py \
    tests/test_ab_loop_contract.py \
    tests/test_navigation_frontend.py \
    tests/test_nav_time_scored_lattice.py \
    tests/test_nav_lattice_controller_wiring.py \
    tests/test_nav_bt_conditional_replanning.py \
    tests/test_navigation_motion_segments.py

if [[ "$PYTHON_ONLY" == "--python-only" ]]; then
  echo "ROS2 build skipped by explicit --python-only"
  exit 0
fi
if [[ ! -f /opt/ros/humble/setup.bash ]]; then
  echo "ERROR: /opt/ros/humble/setup.bash not installed; ROS2 build not verified" >&2
  exit 2
fi
if ! command -v colcon >/dev/null 2>&1; then
  echo "ERROR: colcon is unavailable; ROS2 build not verified" >&2
  exit 2
fi
echo "[4/4] Compile ROS2 Nav2 planner/controller plugin"
# ROS2 Humble's generated setup scripts may read unset AMENT variables.
# Relax nounset only while sourcing the environment, then restore it.
set +u
# shellcheck disable=SC1091
source /opt/ros/humble/setup.bash
set -u
(
  cd "$ROOT/navigation/ros"
  colcon build --packages-select patrol_navigation_controller patrol_navigation --symlink-install
)
echo "PASS: offline PR #93 software and ROS2 build verification"
