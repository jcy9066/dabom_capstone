#!/usr/bin/env bash
# Offline, no-motor verification gate for PR #90 on the GPU host.
# Does not launch Nav2, publish Twist, contact Pi or enable motor output.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd)"
ROS_WS="$ROOT_DIR/navigation/ros"
PREFLIGHT_ONLY=0

usage() {
    printf 'Usage: bash scripts/verify_pr90_humble.sh [--preflight-only]\n' >&2
}
fail() {
    printf '[nav90-verify] FAIL: %s\n' "$*" >&2
    exit 1
}
info() {
    printf '[nav90-verify] %s\n' "$*"
}

for arg in "$@"; do
    case "$arg" in
        --preflight-only) PREFLIGHT_ONLY=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage; fail "Unknown argument: $arg" ;;
    esac
done

[[ -r /etc/os-release ]] || fail 'Unable to determine operating system'
# shellcheck source=/dev/null
source /etc/os-release
[[ "$ID" == 'ubuntu' && "$VERSION_ID" == '22.04' ]] || \
    fail 'Use the project GPU host (Ubuntu 22.04 + ROS2 Humble)'
[[ -f /opt/ros/humble/setup.bash ]] || fail 'ROS2 Humble not installed'
[[ -d "$ROS_WS" ]] || fail 'ROS2 workspace not found'
[[ -f "$ROOT_DIR/tests/test_nav_time_scored_lattice.py" ]] || \
    fail 'Run from a complete dabom_capstone checkout'

command -v colcon >/dev/null || fail 'colcon not installed'
command -v ros2 >/dev/null || fail 'ros2 CLI not installed'
command -v python3 >/dev/null || fail 'python3 not installed'
command -v ldd >/dev/null || fail 'ldd not installed'
command -v g++ >/dev/null || fail 'g++ not installed'
python3 -m pytest --version >/dev/null || fail 'Python pytest not installed'

# These commands modify only the shell environment, never the RC car.
# shellcheck source=/dev/null
set +u
source /opt/ros/humble/setup.bash
set -u
[[ "$ROS_DISTRO" == 'humble' ]] || fail 'ROS_DISTRO is not humble'
for package in \
    nav2_core nav2_controller nav2_planner nav2_bt_navigator \
    nav2_smac_planner nav2_regulated_pure_pursuit_controller \
    nav2_rotation_shim_controller nav2_costmap_2d; do
    ros2 pkg prefix "$package" >/dev/null 2>&1 || \
        fail "Required Humble package missing: $package"
done
info 'Preflight PASS (Ubuntu 22.04 / ROS2 Humble / dependencies)'
if (( PREFLIGHT_ONLY )); then
    exit 0
fi

# This verifier NEVER launches ROS nodes or invokes /api/robots/.../command.
# It cannot establish dynamic Nav2 action correctness without a separate,
# operator-controlled offline map/TF/odom test environment.
info 'Building Nav2 custom planner/controller and Python navigation package'
(
    cd "$ROS_WS"
    colcon build --packages-select patrol_navigation_controller patrol_navigation \
        --event-handlers console_direct+ --cmake-args -DCMAKE_BUILD_TYPE=RelWithDebInfo
)
[[ -f "$ROS_WS/install/setup.bash" ]] || fail 'colcon install/setup.bash missing'
set +u
# shellcheck source=/dev/null
source "$ROS_WS/install/setup.bash"
set -u
prefix="$(ros2 pkg prefix patrol_navigation_controller)" || \
    fail 'Custom controller package not registered in ament index'
lib="$prefix/lib/libsegmented_rpp_controller.so"
[[ -s "$lib" ]] || fail "Built plugin library missing: $lib"
plugin_xml="$prefix/share/patrol_navigation_controller/plugins.xml"
[[ -s "$plugin_xml" ]] || fail "Plugin descriptor missing: $plugin_xml"
python3 - "$plugin_xml" <<'PY'
import sys
import xml.etree.ElementTree as ET

classes = ET.parse(sys.argv[1]).getroot().findall('class')
registered = {(c.get('type'), c.get('base_class_type')) for c in classes}
expected = {
    ('patrol_navigation_controller::SegmentedRPPController', 'nav2_core::Controller'),
    ('patrol_navigation_controller::TimeScoredLatticePlanner', 'nav2_core::GlobalPlanner'),
}
if not expected.issubset(registered):
    raise SystemExit(f'Nav2 plugin registration incomplete: {expected - registered}')
PY
missing="$(ldd "$lib" | grep 'not found' || true)"
[[ -z "$missing" ]] || fail "Unresolved plugin shared-library dependencies: $missing"
info 'Build and plugin descriptor/linkage preflight PASS'

cd "$ROOT_DIR"
# Explicit offline/mock suites. Never start navigation.launch.py or send motor
# commands from this script. Full real-time action testing is a separate gate.
python3 -m pytest -q \
    tests/test_nav_bt_conditional_replanning.py \
    tests/test_nav_gpu_plugin_preflight.py \
    tests/test_nav_lattice_controller_wiring.py \
    tests/test_nav_lattice_jerk_contract.py \
    tests/test_nav_time_scored_lattice.py \
    tests/test_nav_waypoint_bt.py \
    tests/test_navigation_motion_segments.py \
    tests/test_navigation_control_api.py \
    tests/test_navigation_frontend.py \
    tests/test_drive_direction_interlock.py \
    tests/test_nav2_twist_bridge_guard.py \
    tests/test_nav2_bridge_http_worker.py \
    tests/test_nav2_bridge_stop_priority.py \
    tests/test_nav_robot_command_ack_contract.py
info 'Navigation-focused Python/C++ regression suite PASS'
info 'NOT VERIFIED: actual Nav2 action, TF/costmap runtime, Pi motors and physical RC car'
