#!/usr/bin/env bash
# DABOM demo launcher: keep running hardware services untouched; start only missing roles.
set -Eeuo pipefail
ROOT=/home/dabom_user/dabom_capstone
PI_SESSION=dabom-server
AUDIO_SESSION=dabom-audio

[[ -f "$ROOT/start_pi_stack.sh" && -f "$ROOT/raspberry/audio_intercom_client.py" && -f "$ROOT/.env" ]] || {
  echo '[demo] ERROR: required Pi runtime files are missing' >&2
  exit 1
}
command -v tmux >/dev/null || { echo '[demo] ERROR: tmux not installed' >&2; exit 1; }
command -v flock >/dev/null || { echo '[demo] ERROR: flock not installed' >&2; exit 1; }

# Prevent two callers from scheduling the same role simultaneously.
exec 8>"${XDG_RUNTIME_DIR:-/tmp}/dabom-demo-launch.lock"
flock -n 8 || { echo '[demo] Another launcher is running; retry after it finishes.' >&2; exit 1; }

# Only match the *actual argv* of an executable, never a shell's `bash -c` text,
# tmux command, grep/pgrep arguments, or a shell kept open after a script exits.
find_python_role() {
  local suffix="$1" proc pid arg
  local -a argv
  for proc in /proc/[0-9]*/cmdline; do
    argv=()
    mapfile -d '' -t argv < "$proc" 2>/dev/null || continue
    ((${#argv[@]} >= 2)) || continue
    case "${argv[0]##*/}" in python|python[0-9]*|pypy*) ;; *) continue ;; esac
    for arg in "${argv[@]:1}"; do
      if [[ "$arg" == "$ROOT/$suffix" || "$arg" == "$suffix" || "$arg" == */"$suffix" ]]; then
        pid="${proc#/proc/}"; printf '%s\n' "${pid%%/*}"; return 0
      fi
    done
  done
  return 1
}

find_pi_supervisor() {
  local proc pid arg
  local -a argv
  for proc in /proc/[0-9]*/cmdline; do
    argv=()
    mapfile -d '' -t argv < "$proc" 2>/dev/null || continue
    ((${#argv[@]} >= 2)) || continue
    case "${argv[0]##*/}" in bash) ;; *) continue ;; esac
    arg="${argv[1]}"
    if [[ "$arg" == 'start_pi_stack.sh' || "$arg" == "$ROOT/start_pi_stack.sh" ]]; then
      pid="${proc#/proc/}"; printf '%s\n' "${pid%%/*}"; return 0
    fi
  done
  return 1
}

launch_window() {
  local session="$1" name="$2" command="$3" id
  if tmux has-session -t "$session" 2>/dev/null; then
    id="$(tmux new-window -d -P -F '#{window_id}' -t "$session" -n "$name" "$command")"
  else
    id="$(tmux new-session -d -P -F '#{window_id}' -s "$session" -n "$name" "$command")"
  fi
  tmux set-window-option -t "$id" automatic-rename off
  printf '[demo] Started %-12s session=%s window=%s (%s)\n' "$name" "$session" "$id" "$name"
}

robot_pid="$(find_python_role 'raspberry/robot_command_client.py' || true)"
supervisor_pid="$(find_pi_supervisor || true)"
if [[ -n "$robot_pid" ]]; then
  echo "[demo] Pi robot already running pid=$robot_pid: skip"
elif [[ -n "$supervisor_pid" ]]; then
  echo "[demo] Pi supervisor already starting pid=$supervisor_pid: skip; check its log"
else
  PI_CMD="cd '$ROOT'; bash start_pi_stack.sh; rc=\$?; echo '[demo] Pi stack exited code='\$rc; exec bash"
  launch_window "$PI_SESSION" "exhibit-pi-$(date +%H%M%S)" "$PI_CMD"
fi

if audio_pid="$(find_python_role 'raspberry/audio_intercom_client.py')"; then
  echo "[demo] Audio already running pid=$audio_pid: skip"
else
  AUDIO_CMD="cd '$ROOT'; set -a; source <(sed 's/\\r\$//' .env); set +a; python3 raspberry/audio_intercom_client.py; rc=\$?; echo '[demo] Audio exited code='\$rc; exec bash"
  launch_window "$AUDIO_SESSION" "exhibit-audio-$(date +%H%M%S)" "$AUDIO_CMD"
fi

echo '[demo] Actual runtime processes:'
robot_pid="$(find_python_role 'raspberry/robot_command_client.py' || true)"
audio_pid="$(find_python_role 'raspberry/audio_intercom_client.py' || true)"
printf '[demo] robot=%s audio=%s\n' "${robot_pid:-not running yet}" "${audio_pid:-not running yet}"
echo '[demo] tmux windows:'
tmux list-windows -t "$PI_SESSION" -F 'pi: #{window_id} #{window_name}' 2>/dev/null || true
tmux list-windows -t "$AUDIO_SESSION" -F 'audio: #{window_id} #{window_name}' 2>/dev/null || true
