#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ROOT_DIR}/.env"
LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/dabom-gpu-stack.lock"
PID_FILE="${XDG_RUNTIME_DIR:-/tmp}/dabom-gpu-stack.pid"

SERVER_PID=""
ODOM_PID=""
RESTART_LOCKED=0

log() {
    printf '[gpu-stack] %s\n' "$*"
}

warn() {
    printf '[gpu-stack] WARN: %s\n' "$*" >&2
}

fail() {
    printf '[gpu-stack] ERROR: %s\n' "$*" >&2
    exit 1
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

proc_cmdline() {
    local pid="$1"
    tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true
}

list_matching_pids() {
    local needle="$1"
    local proc pid cmdline

    for proc in /proc/[0-9]*; do
        pid="${proc##*/}"
        [[ "${pid}" == "$$" ]] && continue

        cmdline="$(proc_cmdline "${pid}")"
        if [[ -n "${cmdline}" && "${cmdline}" == *"${needle}"* ]]; then
            printf '%s\n' "${pid}"
        fi
    done
}

signal_pid() {
    local pid="$1"
    local signal_name="$2"
    local pgid

    kill -0 "${pid}" 2>/dev/null || return 0
    pgid="$(ps -o pgid= -p "${pid}" 2>/dev/null | tr -d '[:space:]' || true)"

    if [[ -n "${pgid}" && "${pgid}" == "${pid}" ]]; then
        kill "-${signal_name}" -- "-${pgid}" 2>/dev/null || true
    else
        kill "-${signal_name}" "${pid}" 2>/dev/null || true
    fi
}

wait_pid_exit() {
    local pid="$1"
    local attempts="${2:-30}"
    local index

    for ((index = 0; index < attempts; index++)); do
        if ! kill -0 "${pid}" 2>/dev/null; then
            return 0
        fi
        sleep 0.1
    done

    return 1
}

process_group_alive() {
    local pgid="$1"
    [[ -n "${pgid}" ]] || return 1
    kill -0 -- "-${pgid}" 2>/dev/null
}

wait_process_group_exit() {
    local pgid="$1"
    local attempts="${2:-30}"
    local index

    for ((index = 0; index < attempts; index++)); do
        if ! process_group_alive "${pgid}"; then
            return 0
        fi
        sleep 0.1
    done

    return 1
}

list_owned_pgids() {
    local owner="$1"
    local proc pid pgid
    local -A seen=()

    for proc in /proc/[0-9]*; do
        pid="${proc##*/}"
        [[ "${pid}" == "$" ]] && continue

        if tr '\0' '\n' < "${proc}/environ" 2>/dev/null \
            | grep -Fqx "DABOM_PROCESS_OWNER=${owner}"; then
            pgid="$(ps -o pgid= -p "${pid}" 2>/dev/null | tr -d '[:space:]' || true)"
            [[ "${pgid}" =~ ^[0-9]+$ ]] || continue
            [[ -n "${seen[${pgid}]+x}" ]] && continue
            seen["${pgid}"]=1
            printf '%s\n' "${pgid}"
        fi
    done
}

stop_owned_groups() {
    local owner="$1"
    local first_signal="${2:-TERM}"
    local pgid
    local -a groups=()

    mapfile -t groups < <(list_owned_pgids "${owner}")
    (("${#groups[@]}" > 0)) || return 0

    for pgid in "${groups[@]}"; do
        log "Stopping owned process group pgid=${pgid}: ${owner}"
        kill "-${first_signal}" -- "-${pgid}" 2>/dev/null || true
    done

    for _ in {1..30}; do
        mapfile -t groups < <(list_owned_pgids "${owner}")
        (("${#groups[@]}" == 0)) && return 0
        sleep 0.1
    done

    for pgid in "${groups[@]}"; do
        kill -TERM -- "-${pgid}" 2>/dev/null || true
    done

    for _ in {1..20}; do
        mapfile -t groups < <(list_owned_pgids "${owner}")
        (("${#groups[@]}" == 0)) && return 0
        sleep 0.1
    done

    for pgid in "${groups[@]}"; do
        kill -KILL -- "-${pgid}" 2>/dev/null || true
    done

    for _ in {1..10}; do
        mapfile -t groups < <(list_owned_pgids "${owner}")
        (("${#groups[@]}" == 0)) && return 0
        sleep 0.1
    done

    warn "Owned process groups still alive after SIGKILL: ${owner} -> ${groups[*]}"
    return 1
}

stop_pid() {
    local pid="$1"
    local first_signal="${2:-TERM}"

    kill -0 "${pid}" 2>/dev/null || return 0

    signal_pid "${pid}" "${first_signal}"
    if wait_pid_exit "${pid}" 30; then
        return 0
    fi

    signal_pid "${pid}" TERM
    if wait_pid_exit "${pid}" 20; then
        return 0
    fi

    signal_pid "${pid}" KILL
    wait_pid_exit "${pid}" 10 || true
}

stop_matching() {
    local needle="$1"
    local first_signal="${2:-TERM}"
    local pid
    local found=0

    while IFS= read -r pid; do
        [[ -n "${pid}" ]] || continue
        found=1
        log "Stopping existing process pid=${pid}: ${needle}"
        stop_pid "${pid}" "${first_signal}"
    done < <(list_matching_pids "${needle}")

    return "${found}"
}

stop_own_group() {
    local pgid="$1"
    local first_signal="${2:-TERM}"

    [[ -n "${pgid}" ]] || return 0

    if process_group_alive "${pgid}"; then
        kill "-${first_signal}" -- "-${pgid}" 2>/dev/null || true
        if wait_process_group_exit "${pgid}" 30; then
            return 0
        fi

        kill -TERM -- "-${pgid}" 2>/dev/null || true
        if wait_process_group_exit "${pgid}" 20; then
            return 0
        fi

        kill -KILL -- "-${pgid}" 2>/dev/null || true
        wait_process_group_exit "${pgid}" 10 || true
        return 0
    fi

    kill -0 "${pgid}" 2>/dev/null || return 0
    stop_pid "${pgid}" "${first_signal}"
}

remove_own_pid_file() {
    if [[ -f "${PID_FILE}" ]] && [[ "$(cat "${PID_FILE}" 2>/dev/null || true)" == "$$" ]]; then
        rm -f -- "${PID_FILE}"
    fi
}

cleanup() {
    local exit_code=$?

    trap - EXIT INT TERM
    log "Shutting down GPU stack"

    stop_owned_groups "dabom-gpu-odom" TERM || true
    stop_owned_groups "dabom-gpu-fastapi" TERM || true

    # FastAPI normally stops navigation launches in its shutdown hook. These
    # ownership sweeps are the fallback for forced/partial shutdowns.
    stop_owned_groups "dabom-gpu-navigation-MAPPING" TERM || true
    stop_owned_groups "dabom-gpu-navigation-DRIVING" TERM || true

    stop_own_group "${ODOM_PID}" TERM
    stop_own_group "${SERVER_PID}" TERM

    remove_own_pid_file
    exit "${exit_code}"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[[ -f "${ENV_FILE}" ]] || fail ".env not found: ${ENV_FILE}"

set -a
# shellcheck disable=SC1090
source <(sed 's/\r$//' "${ENV_FILE}")
set +a

required_env=(
    SERVER_HOST
    SERVER_PORT
    ROS_DOMAIN_ID
    ROS_LOCALHOST_ONLY
    LIDAR_ENABLE
    LIDAR_ROS_TOPIC
    ENCODER_ROS_ENABLE
    ENCODER_ROS_TOPIC
    WHEEL_DIAMETER_M
    WHEEL_TRACK_M
    ENCODER_TICKS_PER_REV
    WHEEL_TICKS_TOPIC
    ODOM_TOPIC
    ODOM_FRAME
    BASE_FRAME
)

for key in "${required_env[@]}"; do
    [[ -n "${!key:-}" ]] || fail "${key} is required"
done

case "${LIDAR_ENABLE,,}" in
    true|1|yes|on) ;;
    *) fail "LIDAR_ENABLE must be enabled for the final runtime" ;;
esac

case "${ENCODER_ROS_ENABLE,,}" in
    true|1|yes|on) ;;
    *) fail "ENCODER_ROS_ENABLE must be enabled for the final runtime" ;;
esac

[[ "${ROS_LOCALHOST_ONLY}" == "1" ]] \
    || fail "ROS_LOCALHOST_ONLY must be 1; Pi/GPU sensor transport uses WebSocket, not cross-host DDS"

[[ "${ENCODER_ROS_TOPIC}" == "${WHEEL_TICKS_TOPIC}" ]] \
    || fail "ENCODER_ROS_TOPIC and WHEEL_TICKS_TOPIC must match"

require_cmd python3
require_cmd curl
require_cmd flock
require_cmd setsid
require_cmd ps
require_cmd timeout

[[ -f /opt/ros/humble/setup.bash ]] || fail "ROS 2 Humble setup not found"
[[ -f "${ROOT_DIR}/navigation/ros/install/setup.bash" ]] \
    || fail "patrol_navigation is not built: navigation/ros/install/setup.bash is missing"
[[ -f "${ROOT_DIR}/server/app.py" ]] || fail "server/app.py is missing"
[[ -f "${ROOT_DIR}/server/wheel_odometry.py" ]] || fail "server/wheel_odometry.py is missing"
[[ -d "${ROOT_DIR}/frontend/templates" ]] || fail "frontend/templates is missing"
mkdir -p "${ROOT_DIR}/frontend/services/static"

set +u
# shellcheck disable=SC1091
source /opt/ros/humble/setup.bash
# shellcheck disable=SC1091
source "${ROOT_DIR}/navigation/ros/install/setup.bash"
set -u

export ROS_DOMAIN_ID ROS_LOCALHOST_ONLY ENCODER_ROS_ENABLE ENCODER_ROS_TOPIC
export WHEEL_DIAMETER_M WHEEL_TRACK_M ENCODER_TICKS_PER_REV
export WHEEL_TICKS_TOPIC ODOM_TOPIC ODOM_FRAME BASE_FRAME

python3 - <<'PY' >/dev/null 2>&1 || fail "Required Python packages for the GPU runtime are missing"
import fastapi
import rclpy
import uvicorn
PY

exec 9>"${LOCK_FILE}"
if ! flock -w 15 9; then
    fail "Could not acquire restart lock: ${LOCK_FILE}"
fi
RESTART_LOCKED=1

if [[ -f "${PID_FILE}" ]]; then
    old_supervisor="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [[ "${old_supervisor}" =~ ^[0-9]+$ ]] && [[ "${old_supervisor}" != "$$" ]]; then
        old_cmdline="$(proc_cmdline "${old_supervisor}")"
        if [[ "${old_cmdline}" == *"start_gpu_server.sh"* ]]; then
            log "Stopping previous GPU stack supervisor pid=${old_supervisor}"
            stop_pid "${old_supervisor}" TERM
        fi
    fi
fi

# Clean all tagged runtime roles before starting replacements. This makes a
# stack restart idempotent even if an earlier parent died before its children.
stop_owned_groups "dabom-gpu-odom" TERM || true
stop_owned_groups "dabom-gpu-fastapi" TERM || true
stop_owned_groups "dabom-gpu-navigation-MAPPING" TERM || true
stop_owned_groups "dabom-gpu-navigation-DRIVING" TERM || true

# Compatibility cleanup for removed launchers and stale workers.
stop_matching "server/scripts/start_gpu_server.sh" TERM || true
stop_matching "python3 -m uvicorn server.app:app" TERM || true
stop_matching "uvicorn server.app:app" TERM || true
stop_matching "server/wheel_odometry.py" TERM || true
stop_matching "ros2 launch patrol_navigation mapping.launch.py" TERM || true
stop_matching "ros2 launch patrol_navigation navigation.launch.py" TERM || true
stop_matching "ros2 launch patrol_navigation localization.launch.py" TERM || true
# One-time compatibility cleanup for ROS children that may have outlived an old
# launch parent before ownership tagging was introduced.
stop_matching "async_slam_toolbox_node" TERM || true
stop_matching "controller_server" TERM || true
stop_matching "smoother_server" TERM || true
stop_matching "planner_server" TERM || true
stop_matching "behavior_server" TERM || true
stop_matching "bt_navigator" TERM || true
stop_matching "waypoint_follower" TERM || true
stop_matching "velocity_smoother" TERM || true
stop_matching "nav2_command_bridge" TERM || true
stop_matching "map_server" TERM || true
stop_matching "amcl" TERM || true
# Old dashboard builds could create a standalone map_bridge. The current
# navigation launch owns map_bridge, so no standalone copy may survive restart.
stop_matching "ros2 run patrol_navigation map_bridge" TERM || true

printf '%s\n' "$$" > "${PID_FILE}"

cd "${ROOT_DIR}"

log "Starting FastAPI"
setsid env DABOM_PROCESS_OWNER="dabom-gpu-fastapi" python3 -m uvicorn \
    server.app:app \
    --host "${SERVER_HOST}" \
    --port "${SERVER_PORT}" &
SERVER_PID=$!

health_host="${SERVER_HOST}"
case "${health_host}" in
    0.0.0.0|::|\*) health_host="127.0.0.1" ;;
esac
health_url="http://${health_host}:${SERVER_PORT}/get_status"

server_ready=0
for _ in {1..30}; do
    if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
        break
    fi
    if curl --fail --silent --show-error --max-time 1 "${health_url}" >/dev/null 2>&1; then
        server_ready=1
        break
    fi
    sleep 0.5
done

if (( server_ready == 0 )); then
    fail "FastAPI did not become ready at ${health_url}"
fi

start_odometry() {
    log "Starting wheel odometry"
    setsid env DABOM_PROCESS_OWNER="dabom-gpu-odom" python3 server/wheel_odometry.py &
    ODOM_PID=$!

    local publisher_ready=0
    local topic_info
    for _ in {1..20}; do
        if ! kill -0 "${ODOM_PID}" 2>/dev/null; then
            break
        fi
        topic_info="$(ros2 topic info "${ODOM_TOPIC}" 2>/dev/null || true)"
        if grep -Eq 'Publisher count:[[:space:]]*[1-9][0-9]*' <<< "${topic_info}"; then
            publisher_ready=1
            break
        fi
        sleep 0.5
    done

    if (( publisher_ready == 0 )); then
        fail "wheel odometry publisher did not appear on ${ODOM_TOPIC}"
    fi
}

start_odometry

# A publisher object alone is not evidence of live odometry. If encoder data is
# already available, wait briefly for a real /odom sample because ROS discovery
# and the first encoder callback may take a few seconds. If Pi is not connected
# yet, keep the local runtime READY and explicitly report WAITING.
if timeout 3 ros2 topic echo "${WHEEL_TICKS_TOPIC}" --once >/dev/null 2>&1; then
    if timeout 10 ros2 topic echo "${ODOM_TOPIC}" --once >/dev/null 2>&1; then
        log "Odometry data READY: live ${WHEEL_TICKS_TOPIC} -> ${ODOM_TOPIC} confirmed"
    else
        fail "live encoder ticks exist but no odometry message was received on ${ODOM_TOPIC}"
    fi
else
    log "WAITING: no live encoder sample yet; ${ODOM_TOPIC} will start when Pi encoder telemetry arrives"
fi



flock -u 9
RESTART_LOCKED=0

log "READY: GPU local stack is running"
log "FastAPI PID=${SERVER_PID}"
log "Odometry PID=${ODOM_PID}"

if curl --fail --silent --max-time 1 "${health_url}" \
    | python3 -c 'import json,sys; data=json.load(sys.stdin); raise SystemExit(0 if data.get("updated_at") is not None else 1)' \
    >/dev/null 2>&1; then
    log "CONNECTED: Raspberry Pi status is already arriving"
else
    log "WAITING: Raspberry Pi stack may be started before or after this script"
fi

# FastAPI is the primary GPU service. wheel_odometry is owned by this
# supervisor and is automatically restarted if an external dashboard action or
# transient error terminates it. This prevents a child stop from collapsing the
# entire GPU stack.
while true; do
    if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
        warn "FastAPI exited unexpectedly"
        exit 1
    fi

    if ! kill -0 "${ODOM_PID}" 2>/dev/null; then
        warn "wheel odometry exited unexpectedly; restarting supervisor-owned worker"
        start_odometry
    fi

    sleep 1
done
