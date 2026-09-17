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
    local pid="$1"
    local first_signal="${2:-TERM}"

    [[ -n "${pid}" ]] || return 0
    kill -0 "${pid}" 2>/dev/null || return 0

    kill "-${first_signal}" -- "-${pid}" 2>/dev/null || kill "-${first_signal}" "${pid}" 2>/dev/null || true
    if wait_pid_exit "${pid}" 30; then
        return 0
    fi

    kill -TERM -- "-${pid}" 2>/dev/null || kill -TERM "${pid}" 2>/dev/null || true
    if wait_pid_exit "${pid}" 20; then
        return 0
    fi

    kill -KILL -- "-${pid}" 2>/dev/null || kill -KILL "${pid}" 2>/dev/null || true
    wait_pid_exit "${pid}" 10 || true
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
    ENCODER_ROS_ENABLE
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

case "${ENCODER_ROS_ENABLE,,}" in
    true|1|yes|on) ;;
    *) fail "ENCODER_ROS_ENABLE must be enabled for the final runtime" ;;
esac

require_cmd python3
require_cmd curl
require_cmd flock
require_cmd setsid
require_cmd ps

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

export ROS_DOMAIN_ID ENCODER_ROS_ENABLE
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

# Compatibility cleanup for the removed legacy launcher and any stale workers.
stop_matching "server/scripts/start_gpu_server.sh" TERM || true
stop_matching "python3 -m uvicorn server.app:app" TERM || true
stop_matching "uvicorn server.app:app" TERM || true
stop_matching "server/wheel_odometry.py" TERM || true
stop_matching "ros2 launch patrol_navigation mapping.launch.py" TERM || true
stop_matching "ros2 launch patrol_navigation navigation.launch.py" TERM || true
stop_matching "ros2 launch patrol_navigation localization.launch.py" TERM || true

printf '%s\n' "$$" > "${PID_FILE}"

cd "${ROOT_DIR}"

log "Starting FastAPI"
setsid python3 -m uvicorn \
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

log "Starting wheel odometry"
setsid python3 server/wheel_odometry.py &
ODOM_PID=$!

odom_ready=0
for _ in {1..20}; do
    if ! kill -0 "${ODOM_PID}" 2>/dev/null; then
        break
    fi

    topic_info="$(ros2 topic info "${ODOM_TOPIC}" 2>/dev/null || true)"
    if grep -Eq 'Publisher count:[[:space:]]*[1-9][0-9]*' <<< "${topic_info}"; then
        odom_ready=1
        break
    fi
    sleep 0.5
done

if (( odom_ready == 0 )); then
    fail "wheel odometry is not publishing ${ODOM_TOPIC}"
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

set +e
wait -n "${SERVER_PID}" "${ODOM_PID}"
status=$?
set -e

if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
    warn "FastAPI exited unexpectedly"
fi
if ! kill -0 "${ODOM_PID}" 2>/dev/null; then
    warn "wheel odometry exited unexpectedly"
fi

exit "${status}"
