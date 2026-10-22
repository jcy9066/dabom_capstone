#!/usr/bin/env bash
# Offline software-only regression gate. Does not start ROS, Pi, or motors.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"
fail() { printf '[pr90-software] FAIL: %s\n' "$*" >&2; exit 1; }
info() { printf '[pr90-software] %s\n' "$*"; }
[[ -f tests/test_nav_robot_command_ack_contract.py ]] || fail 'Incomplete repository checkout'
command -v python3 >/dev/null || fail 'python3 missing'
command -v node >/dev/null || fail 'Node.js missing'
command -v g++ >/dev/null || fail 'C++ compiler missing'
python3 -m pytest --version >/dev/null || fail 'pytest missing'

# This verifier never starts robot/ROS runtime or sends hardware commands.
info 'Python tests: complete tests/ collection'
python3 -m pytest -q tests
info 'Python regression PASS'

info 'JavaScript parser and UI contract tests'
# Three.js is an ES module; --input-type=module + stdin also works on
# older supported Node versions without package.json type=module.
node --input-type=module --check < frontend/services/static/lidar_3d_viewer.js
for file in \
    frontend/services/static/script.js \
    frontend/services/static/navigation_control.js \
    frontend/services/static/dashboard_state.js \
    frontend/services/static/speaker_volume.js \
    frontend/services/static/action_feedback.js \
    tests/test_dashboard_action_feedback.js \
    tests/test_dashboard_report_feedback.js; do
    node --check "$file"
done
node tests/test_dashboard_action_feedback.js
node tests/test_dashboard_report_feedback.js
info 'JavaScript regression PASS'

info 'Independent C++17 motion and safety regressions'
tmpdir="$(mktemp -d)"
trap 'rm -rf -- "$tmpdir"' EXIT
for test in \
    tests/cpp/test_path_time_estimator.cpp \
    tests/cpp/test_local_motion_sweep.cpp \
    tests/cpp/test_segment_handoff_guard.cpp; do
    name="$(basename "$test" .cpp)"
    g++ -std=c++17 -O2 -Wall -Wextra -Werror \
        -I navigation/ros/patrol_navigation_controller/include \
        "$test" -o "$tmpdir/$name"
    "$tmpdir/$name"
done
info 'C++ regression PASS'

info 'Static ROS, server, Pi and shell contract checks'
python3 .agents/skills/validate-navigation/scripts/validate_navigation.py
python3 .agents/skills/validate-server/scripts/validate_server.py
python3 .agents/skills/validate-raspberry/scripts/validate_uart_protocol.py
bash -n start_gpu_server.sh start_pi_stack.sh scripts/verify_pr90_humble.sh
info 'Software-only checks PASS; still requires Humble runtime and physical RC-car validation'
