#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ROOT_DIR}/.env"
LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/dabom-pi-stack.lock"
PID_FILE="${XDG_RUNTIME_DIR:-/tmp}/dabom-pi-stack.pid"

ROBOT_PID=""
LIDAR_DRIVER_PID=""
LIDAR_SENDER_PID=""
CAMERA_PID=""

log() {
    printf '[pi-stack] %s\n' "$*"
}

warn() {
    printf '[pi-stack] WARN: %s\n' "$*" >&2
}

fail() {
    printf '[pi-stack] ERROR: %s\n' "$*" >&2
    exit 1
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

proc_cmdline() {
    local pid="$1"

    # /proc entries can disappear between directory enumeration and read.
    # Avoid shell input-redirection errors leaking to the terminal on that race.
    cat -- "/proc/${pid}/cmdline" 2>/dev/null | tr '\0' ' ' || true
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
        [[ "${pid}" == "$$" ]] && continue

        [[ -r "${proc}/environ" ]] || continue
        if cat -- "${proc}/environ" 2>/dev/null \
            | tr '\0' '\n' \
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

    for _ in {1..10}; do
        mapfile -t groups < <(list_owned_pgids "${owner}")
        (("${#groups[@]}" == 0)) && return 0
        sleep 0.1
    done

    for pgid in "${groups[@]}"; do
        kill -TERM -- "-${pgid}" 2>/dev/null || true
    done

    for _ in {1..5}; do
        mapfile -t groups < <(list_owned_pgids "${owner}")
        (("${#groups[@]}" == 0)) && return 0
        sleep 0.1
    done

    for pgid in "${groups[@]}"; do
        kill -KILL -- "-${pgid}" 2>/dev/null || true
    done

    for _ in {1..5}; do
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
    local pgid

    kill -0 "${pid}" 2>/dev/null || return 0

    # Snapshot the process group before signalling. If this PID is a setsid
    # leader, the leader can exit before its children; waiting only on the PID
    # would falsely report success while descendants remain alive.
    pgid="$(ps -o pgid= -p "${pid}" 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ "${pgid}" =~ ^[0-9]+$ ]] && [[ "${pgid}" == "${pid}" ]]; then
        stop_own_group "${pgid}" "${first_signal}"
        return
    fi

    signal_pid "${pid}" "${first_signal}"
    if wait_pid_exit "${pid}" 10; then
        return 0
    fi

    signal_pid "${pid}" TERM
    if wait_pid_exit "${pid}" 5; then
        return 0
    fi

    signal_pid "${pid}" KILL
    wait_pid_exit "${pid}" 5 || true
}

stop_matching() {
    local needle="$1"
    local first_signal="${2:-TERM}"
    local pid

    while IFS= read -r pid; do
        [[ -n "${pid}" ]] || continue
        log "Stopping existing process pid=${pid}: ${needle}"
        stop_pid "${pid}" "${first_signal}"
    done < <(list_matching_pids "${needle}")
}

stop_own_group() {
    local pgid="$1"
    local first_signal="${2:-TERM}"

    [[ -n "${pgid}" ]] || return 0

    if process_group_alive "${pgid}"; then
        kill "-${first_signal}" -- "-${pgid}" 2>/dev/null || true
        if wait_process_group_exit "${pgid}" 10; then
            return 0
        fi

        kill -TERM -- "-${pgid}" 2>/dev/null || true
        if wait_process_group_exit "${pgid}" 5; then
            return 0
        fi

        kill -KILL -- "-${pgid}" 2>/dev/null || true
        wait_process_group_exit "${pgid}" 5 || true
        return 0
    fi

    # Compatibility fallback for a child that was not a setsid group leader.
    kill -0 "${pgid}" 2>/dev/null || return 0
    stop_pid "${pgid}" "${first_signal}"
}

remove_own_pid_file() {
    if [[ -f "${PID_FILE}" ]] && [[ "$(cat "${PID_FILE}" 2>/dev/null || true)" == "$$" ]]; then
        rm -f -- "${PID_FILE}"
    fi
}

CLEANUP_STARTED=0

cleanup() {
    local exit_code="${1:-$?}"
    local job

    if (( CLEANUP_STARTED )); then
        return 0
    fi
    CLEANUP_STARTED=1

    # Prevent EXIT recursion and make repeated Ctrl+C harmless while the bounded
    # shutdown sequence finishes.
    trap - EXIT
    trap '' INT TERM
    log "Shutting down Pi stack"

    # Stop every independently-owned runtime role concurrently. Sequential
    # role-by-role waits made one Ctrl+C appear to hang for tens of seconds.
    local -a cleanup_jobs=()
    stop_owned_groups "dabom-pi-robot" INT &
    cleanup_jobs+=("$!")
    stop_owned_groups "dabom-pi-lidar-sender" TERM &
    cleanup_jobs+=("$!")
    stop_owned_groups "dabom-pi-lidar" TERM &
    cleanup_jobs+=("$!")
    stop_owned_groups "dabom-pi-camera" TERM &
    cleanup_jobs+=("$!")

    # Compatibility fallback for processes created before ownership tagging.
    stop_own_group "${ROBOT_PID}" INT &
    cleanup_jobs+=("$!")
    stop_own_group "${LIDAR_SENDER_PID}" TERM &
    cleanup_jobs+=("$!")
    stop_own_group "${LIDAR_DRIVER_PID}" TERM &
    cleanup_jobs+=("$!")
    stop_own_group "${CAMERA_PID}" TERM &
    cleanup_jobs+=("$!")

    set +e
    for job in "${cleanup_jobs[@]}"; do
        wait "${job}"
    done
    set -e

    remove_own_pid_file
    log "STOPPED: Pi local stack"
    exit "${exit_code}"
}

trap 'cleanup $?' EXIT
trap 'cleanup 130' INT
trap 'cleanup 143' TERM

[[ -f "${ENV_FILE}" ]] || fail ".env not found: ${ENV_FILE}"

set -a
# shellcheck disable=SC1090
source <(sed 's/\r$//' "${ENV_FILE}")
set +a

required_env=(
    SERVER_BASE_URL
    ROBOT_ID
    ROBOT_CONTROL_TOKEN
    ROS_DOMAIN_ID
    ROS_LOCALHOST_ONLY
    MOTOR_SERIAL_PORT
    MOTOR_SERIAL_BAUDRATE
    WHEEL_TICKS_TOPIC
    LIDAR_ENABLE
    LIDAR_SCAN_TOPIC
    LIDAR_WS_RECONNECT_SEC
    LIDAR_SERIAL_PORT
    LIDAR_SERIAL_BAUDRATE
    LIDAR_DRIVER_PACKAGE
    LIDAR_DRIVER_EXECUTABLE
    LIDAR_FRAME
    LIDAR_BASE_FRAME
    LIDAR_X
    LIDAR_Y
    LIDAR_Z
    LIDAR_ROLL
    LIDAR_PITCH
    LIDAR_YAW
    STREAM_WIDTH
    STREAM_HEIGHT
    STREAM_FPS
    STREAM_BITRATE
    STREAM_INFER
    STREAM_RETRY_SEC
    CURL_CONNECT_TIMEOUT_SEC
)

for key in "${required_env[@]}"; do
    [[ -n "${!key:-}" ]] || fail "${key} is required"
done

case "${LIDAR_ENABLE,,}" in
    true|1|yes|on) ;;
    *) fail "LIDAR_ENABLE must be enabled for the final runtime" ;;
esac

case "${STREAM_INFER,,}" in
    true|false|1|0|yes|no|on|off) ;;
    *) fail "STREAM_INFER must be a boolean value" ;;
esac

[[ "${ROS_LOCALHOST_ONLY}" == "1" ]] \
    || fail "ROS_LOCALHOST_ONLY must be 1; Pi/GPU sensor transport uses WebSocket, not cross-host DDS"

[[ "${MOTOR_SERIAL_PORT}" != "${LIDAR_SERIAL_PORT}" ]] \
    || fail "Pico and LiDAR cannot share the same serial port: ${MOTOR_SERIAL_PORT}"

require_cmd python3
require_cmd curl
require_cmd flock
require_cmd setsid
require_cmd ps
require_cmd timeout
require_cmd rpicam-vid
require_cmd systemctl

[[ -f /opt/ros/humble/setup.bash ]] || fail "ROS 2 Humble setup not found"
[[ -f "${ROOT_DIR}/navigation/ros/install/setup.bash" ]] \
    || fail "patrol_navigation is not built: navigation/ros/install/setup.bash is missing"
[[ -f "${ROOT_DIR}/raspberry/robot_command_client.py" ]] \
    || fail "raspberry/robot_command_client.py is missing"
[[ -f "${ROOT_DIR}/raspberry/lidar_scan_sender.py" ]] \
    || fail "raspberry/lidar_scan_sender.py is missing"
[[ -f "${ROOT_DIR}/raspberry/lidar_driver_supervisor.py" ]] \
    || fail "raspberry/lidar_driver_supervisor.py is missing"

[[ -e "${MOTOR_SERIAL_PORT}" ]] || fail "Pico UART device not found: ${MOTOR_SERIAL_PORT}"
[[ -r "${MOTOR_SERIAL_PORT}" && -w "${MOTOR_SERIAL_PORT}" ]] \
    || fail "No read/write permission for Pico UART: ${MOTOR_SERIAL_PORT}"
[[ -e "${LIDAR_SERIAL_PORT}" ]] || fail "LiDAR serial device not found: ${LIDAR_SERIAL_PORT}"
[[ -r "${LIDAR_SERIAL_PORT}" && -w "${LIDAR_SERIAL_PORT}" ]] \
    || fail "No read/write permission for LiDAR: ${LIDAR_SERIAL_PORT}"

set +u
# shellcheck disable=SC1091
source /opt/ros/humble/setup.bash
# shellcheck disable=SC1091
source "${ROOT_DIR}/navigation/ros/install/setup.bash"
set -u

export ROS_DOMAIN_ID ROS_LOCALHOST_ONLY PYTHONUNBUFFERED=1

python3 - <<'PY' >/dev/null 2>&1 || fail "Required Python packages for the Pi runtime are missing"
import dotenv
import requests
import serial
import websockets
import rclpy
PY

exec 9>"${LOCK_FILE}"
if ! flock -w 15 9; then
    fail "Could not acquire restart lock: ${LOCK_FILE}"
fi

if [[ -f "${PID_FILE}" ]]; then
    old_supervisor="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [[ "${old_supervisor}" =~ ^[0-9]+$ ]] && [[ "${old_supervisor}" != "$$" ]]; then
        old_cmdline="$(proc_cmdline "${old_supervisor}")"
        if [[ "${old_cmdline}" == *"start_pi_stack.sh"* ]]; then
            log "Stopping previous Pi stack supervisor pid=${old_supervisor}"
            stop_pid "${old_supervisor}" TERM
        fi
    fi
fi

# A legacy systemd service can otherwise restart robot_command_client.py behind
# this script and recreate the duplicate UART owner.
if systemctl is-active --quiet dabom-command.service 2>/dev/null; then
    log "Stopping legacy dabom-command.service"
    if [[ "${EUID}" -eq 0 ]]; then
        systemctl stop dabom-command.service
    elif command -v sudo >/dev/null 2>&1; then
        sudo systemctl stop dabom-command.service
    else
        fail "dabom-command.service is active and sudo is unavailable"
    fi
fi

# Remove any previous tagged runtime first. A new Pi stack owns exactly one
# logical instance of each role.
stop_owned_groups "dabom-pi-robot" INT || true
stop_owned_groups "dabom-pi-lidar-sender" TERM || true
stop_owned_groups "dabom-pi-lidar" TERM || true
stop_owned_groups "dabom-pi-camera" TERM || true

# Compatibility cleanup for the removed legacy launchers.
stop_matching "raspberry/scripts/start_camera_stream.sh" TERM
stop_matching "raspberry/scripts/start_lidar_sender.sh" TERM
stop_matching "raspberry/scripts/start_robot_command_client.sh" TERM

# Stop identical runtime roles. Robot gets SIGINT first for a graceful motor STOP.
stop_matching "raspberry/robot_command_client.py" INT
stop_matching "raspberry/lidar_scan_sender.py" TERM
stop_matching "ros2 launch patrol_navigation lidar.launch.py" TERM
stop_matching "rplidar_composition" TERM
stop_matching "static_transform_publisher" TERM
stop_matching "rpicam-vid" TERM
stop_matching "/stream/h264?robot_id=${ROBOT_ID}" TERM

# Verify that the Pico responds and is stopped before handing UART ownership to the robot client.
if ! python3 - "${MOTOR_SERIAL_PORT}" "${MOTOR_SERIAL_BAUDRATE}" <<'PY'
import serial
import sys
import time

port = sys.argv[1]
baudrate = int(sys.argv[2])

with serial.Serial(
    port=port,
    baudrate=baudrate,
    timeout=0.1,
    write_timeout=0.5,
) as device:
    device.write(b"ENC_STREAM,0\n")
    device.flush()
    time.sleep(0.1)

    def exchange(command: bytes, expected_prefix: str) -> None:
        device.reset_input_buffer()
        device.write(command + b"\n")
        device.flush()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            line = device.readline().decode("ascii", errors="replace").strip()
            if line.startswith(expected_prefix):
                return
            if line.startswith("ERR"):
                print(line, file=sys.stderr)
                raise SystemExit(2)
        raise SystemExit(1)

    exchange(b"PING", "OK,PONG")
    exchange(b"STOP,startup_preflight", "OK")
PY
then
    fail "Pico UART PING/STOP preflight failed on ${MOTOR_SERIAL_PORT}"
fi
log "Pico UART READY and motor STOP confirmed"

camera_list="$(timeout 8 rpicam-vid --list-cameras 2>&1 || true)"
if ! grep -Eq '^[[:space:]]*[0-9]+[[:space:]]*:' <<< "${camera_list}"; then
    printf '%s\n' "${camera_list}" >&2
    fail "No camera was detected by rpicam-vid"
fi
log "Camera hardware READY"

printf '%s\n' "$$" > "${PID_FILE}"

cd "${ROOT_DIR}"

log "Starting LiDAR driver supervisor"
setsid env DABOM_PROCESS_OWNER="dabom-pi-lidar" python3 "${ROOT_DIR}/raspberry/lidar_driver_supervisor.py" \
    --topic "${LIDAR_SCAN_TOPIC}" \
    --startup-timeout 15 \
    --stale-timeout "${LIDAR_SCAN_STALE_SEC:-3.0}" \
    --restart-delay 1 \
    ros2 launch patrol_navigation lidar.launch.py \
    serial_port:="${LIDAR_SERIAL_PORT}" \
    serial_baudrate:="${LIDAR_SERIAL_BAUDRATE}" \
    driver_package:="${LIDAR_DRIVER_PACKAGE}" \
    driver_executable:="${LIDAR_DRIVER_EXECUTABLE}" \
    frame_id:="${LIDAR_FRAME}" \
    base_frame:="${LIDAR_BASE_FRAME}" \
    laser_x:="${LIDAR_X}" \
    laser_y:="${LIDAR_Y}" \
    laser_z:="${LIDAR_Z}" \
    laser_roll:="${LIDAR_ROLL}" \
    laser_pitch:="${LIDAR_PITCH}" \
    laser_yaw:="${LIDAR_YAW}" &
LIDAR_DRIVER_PID=$!

scan_ready=0

if timeout 15 python3 - "${LIDAR_SCAN_TOPIC}" <<'PYSCAN'
import sys
import time

import rclpy
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import LaserScan

topic = sys.argv[1]

rclpy.init()
node = rclpy.create_node("dabom_lidar_preflight")

received = False


def on_scan(msg):
    global received
    received = True


subscription = node.create_subscription(
    LaserScan,
    topic,
    on_scan,
    qos_profile_sensor_data,
)

deadline = time.monotonic() + 12.0

try:
    while time.monotonic() < deadline and not received:
        rclpy.spin_once(
            node,
            timeout_sec=0.5,
        )
finally:
    node.destroy_node()

    if rclpy.ok():
        rclpy.shutdown()

raise SystemExit(0 if received else 1)
PYSCAN
then
    scan_ready=1
fi

if (( scan_ready == 0 )); then
    fail "LiDAR driver started but no LaserScan was received on ${LIDAR_SCAN_TOPIC}"
fi

log "LiDAR scan READY"

log "Starting LiDAR sender"
setsid env DABOM_PROCESS_OWNER="dabom-pi-lidar-sender" python3 "${ROOT_DIR}/raspberry/lidar_scan_sender.py" &
LIDAR_SENDER_PID=$!
sleep 0.5
kill -0 "${LIDAR_SENDER_PID}" 2>/dev/null \
    || fail "LiDAR sender exited during startup"

log "Starting robot command client"
setsid env DABOM_PROCESS_OWNER="dabom-pi-robot" python3 "${ROOT_DIR}/raspberry/robot_command_client.py" &
ROBOT_PID=$!
sleep 0.5
kill -0 "${ROBOT_PID}" 2>/dev/null \
    || fail "Robot command client exited during startup"

STREAM_URL="${SERVER_BASE_URL%/}/stream/h264?robot_id=${ROBOT_ID}&infer=${STREAM_INFER}"
export STREAM_URL

camera_stream_loop() {
    set +e

    while true; do
        local runtime_dir fifo_path camera_child curl_child curl_status
        runtime_dir="$(mktemp -d "${TMPDIR:-/tmp}/dabom-h264.XXXXXX")"
        fifo_path="${runtime_dir}/camera.h264"
        mkfifo "${fifo_path}"

        rpicam-vid \
            -t 0 \
            --nopreview \
            --codec h264 \
            --inline \
            --width "${STREAM_WIDTH}" \
            --height "${STREAM_HEIGHT}" \
            --framerate "${STREAM_FPS}" \
            --bitrate "${STREAM_BITRATE}" \
            -o "${fifo_path}" &
        camera_child=$!

        curl \
            --fail \
            --silent \
            --show-error \
            --http1.1 \
            --no-buffer \
            --connect-timeout "${CURL_CONNECT_TIMEOUT_SEC}" \
            --request POST \
            --upload-file "${fifo_path}" \
            --header "Content-Type: video/H264" \
            --header "Transfer-Encoding: chunked" \
            "${STREAM_URL}" &
        curl_child=$!

        wait "${curl_child}"
        curl_status=$?

        kill "${camera_child}" 2>/dev/null || true
        wait "${camera_child}" 2>/dev/null || true
        rm -rf -- "${runtime_dir}"

        if (( curl_status == 0 )); then
            printf '[pi-stack] WARN: camera stream ended; reconnecting in %ss\n' "${STREAM_RETRY_SEC}" >&2
        else
            printf '[pi-stack] WARN: GPU camera upload unavailable; reconnecting in %ss\n' "${STREAM_RETRY_SEC}" >&2
        fi

        sleep "${STREAM_RETRY_SEC}"
    done
}

log "Starting camera stream"
setsid env DABOM_PROCESS_OWNER="dabom-pi-camera" bash -c "$(declare -f camera_stream_loop); camera_stream_loop" &
CAMERA_PID=$!
sleep 0.5
kill -0 "${CAMERA_PID}" 2>/dev/null \
    || fail "Camera stream supervisor exited during startup"

flock -u 9

log "READY: Pi local stack is running"
log "Robot PID=${ROBOT_PID}"
log "LiDAR driver PID=${LIDAR_DRIVER_PID}"
log "LiDAR sender PID=${LIDAR_SENDER_PID}"
log "Camera PID=${CAMERA_PID}"

if curl --fail --silent --max-time 2 "${SERVER_BASE_URL%/}/get_status" >/dev/null 2>&1; then
    robot_connected=0
    camera_connected=0
    lidar_connected=0
    encoder_connected=0

    for _ in {1..5}; do
        robot_json="$(curl --fail --silent --max-time 1 "${SERVER_BASE_URL%/}/get_status" 2>/dev/null || true)"
        camera_json="$(curl --fail --silent --max-time 1 "${SERVER_BASE_URL%/}/api/stream_status" 2>/dev/null || true)"
        lidar_json="$(curl --fail --silent --max-time 1 "${SERVER_BASE_URL%/}/api/lidar/bridge" 2>/dev/null || true)"
        encoder_json="$(curl --fail --silent --max-time 1 "${SERVER_BASE_URL%/}/api/encoder/bridge" 2>/dev/null || true)"

        if [[ -n "${robot_json}" ]] && python3 -c \
            'import json,sys; d=json.loads(sys.argv[1]); raise SystemExit(0 if d.get("updated_at") is not None else 1)' \
            "${robot_json}" >/dev/null 2>&1; then
            robot_connected=1
        fi

        if [[ -n "${camera_json}" ]] && python3 -c \
            'import json,sys; d=json.loads(sys.argv[1]); raise SystemExit(0 if d.get("camera_connected") is True else 1)' \
            "${camera_json}" >/dev/null 2>&1; then
            camera_connected=1
        fi

        if [[ -n "${lidar_json}" ]] && python3 -c \
            'import json,sys,time; d=json.loads(sys.argv[1]); st=d.get("stats") or {}; last=st.get("last_received_at"); fresh=last is not None and time.time()-float(last) <= 3.0; raise SystemExit(0 if st.get("connected") is True and int(st.get("received") or 0) > 0 and fresh else 1)' \
            "${lidar_json}" >/dev/null 2>&1; then
            lidar_connected=1
        fi

        if [[ -n "${encoder_json}" ]] && python3 -c \
            'import json,sys,time; d=json.loads(sys.argv[1]); st=d.get("stats") or {}; last=st.get("last_received_at"); fresh=last is not None and time.time()-float(last) <= 3.0; raise SystemExit(0 if d.get("enabled") is True and int(st.get("published") or 0) > 0 and fresh else 1)' \
            "${encoder_json}" >/dev/null 2>&1; then
            encoder_connected=1
        fi

        if (( robot_connected && camera_connected && lidar_connected && encoder_connected )); then
            break
        fi
        sleep 0.5
    done

    if (( robot_connected && camera_connected && lidar_connected && encoder_connected )); then
        log "CONNECTED: GPU receives robot status, camera, LiDAR, and encoder telemetry"
    else
        (( robot_connected )) || warn "GPU is reachable but robot status is not arriving"
        (( camera_connected )) || warn "GPU is reachable but camera stream is not confirmed"
        (( lidar_connected )) || warn "GPU is reachable but LiDAR stream is not confirmed"
        (( encoder_connected )) || warn "GPU is reachable but fresh encoder telemetry is not confirmed"
        log "READY: local stack remains active and will keep reconnecting"
    fi
else
    log "WAITING: GPU server may be started before or after this script"
fi

set +e
wait -n "${ROBOT_PID}" "${LIDAR_DRIVER_PID}" "${LIDAR_SENDER_PID}" "${CAMERA_PID}"
status=$?
set -e

if ! kill -0 "${ROBOT_PID}" 2>/dev/null; then
    warn "Robot command client exited unexpectedly"
fi
if ! kill -0 "${LIDAR_DRIVER_PID}" 2>/dev/null; then
    warn "LiDAR driver exited unexpectedly"
fi
if ! kill -0 "${LIDAR_SENDER_PID}" 2>/dev/null; then
    warn "LiDAR sender exited unexpectedly"
fi
if ! kill -0 "${CAMERA_PID}" 2>/dev/null; then
    warn "Camera stream supervisor exited unexpectedly"
fi

exit "${status}"
