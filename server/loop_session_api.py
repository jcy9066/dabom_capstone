"""Session-only LOOP controller: named goals, Nav2 multi-goal patrol and manual command bags.

Manual replay reissues ACK-confirmed Pi MOVE/STOP commands at recorded offsets.
It does not use pose endpoints or trajectory correction. The independent legacy
RecordedLoopApi is preserved for compatibility, not used by this controller.
"""
from __future__ import annotations

import asyncio
import math
import threading
import time
from uuid import uuid4
from collections import OrderedDict
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from server.recorded_loop_core import RecordedLoopError, pose_dict, map_identifier, map_digest
from pathlib import Path
from server.recorded_loop_obstacle import path_blocked
from server.manual_loop_segments import compile_manual_segments
from server.env_config import env_float

GOAL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
MAX_EVENTS = 10000
MAX_RECORD_SECONDS = 3600.0
MAX_SAVED_BAGS_PER_MAP = 24
MANUAL_PULSE_MAX_MS = 120
MANUAL_ACK_TIMEOUT_SEC = 2.5


class LoopSessionApi:
    def __init__(self, app: FastAPI, navigation: Any, recorded: Any,
                 csrf_failure: Any, send_robot_command: Any):
        self.navigation = navigation
        self.recorded = recorded
        self.csrf_failure = csrf_failure
        self.send = send_robot_command
        self._lock = threading.RLock()
        self._maps: dict[tuple[str, str], dict] = {}
        self._record: dict | None = None
        self._task: asyncio.Task | None = None
        self._cancelled = False
        self._policy = "WAIT"
        self._status = "IDLE"
        self._error: str | None = None
        self._laps = 0
        self._running_kind: str | None = None
        self._resume_event = asyncio.Event()
        self._requires_resume = False
        self._connection_generation = 0
        self._replay_generation = 0
        self._manual_session_id = ""
        self._manual_segment_index = 0
        self._manual_elapsed_ms = 0.0
        self._manual_pulse_index = 0
        self._manual_pi_safety_session = None
        self._register(app)

    def _selected_map(self) -> tuple[str, str]:
        """Resolve map data without requiring the robot or Pi to be online."""
        active = self.navigation.state_response().get("active_map")
        if not isinstance(active, dict):
            raise RecordedLoopError("MAP_REQUIRED", "No active map")
        name = map_identifier(active.get("map_name"))
        selected = self.recorded.map_api.resolve_map(name)
        return name, map_digest(Path(selected.yaml_path))

    def _active(self) -> tuple[str, str]:
        # Write operations retain their existing DRIVING / map requirements.
        if self.navigation.state_response().get("navigation_mode") != "DRIVING":
            raise RecordedLoopError("DRIVING_REQUIRED", "Load a map in DRIVING mode")
        return self._selected_map()

    def _entry(self, key: tuple[str, str]) -> dict:
        with self._lock:
            return self._maps.setdefault(
                key, {"goals": OrderedDict(), "next": 0, "bags": OrderedDict(), "bag_next": 1,
                 "auto_paths": {}})

    def _idle(self) -> None:
        if self.playing:
            raise RecordedLoopError("LOOP_BUSY", "Stop the active Loop first")

    @property
    def playing(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def manual_playing(self) -> bool:
        return self.playing and self._running_kind == "MANUAL"

    def _mode(self) -> str:
        return str(self.navigation.state_response().get("robot_mode", "")).upper()

    def note_pi_connection(self, connected: bool) -> None:
        """External Pi WebSocket generation, not the status HTTP polling clock."""
        with self._lock:
            self._connection_generation += 1
            if self.manual_playing:
                self._requires_resume = True
                self._replay_generation += 1
                self._resume_event.clear()
                self._status = "PAUSED_CONNECTION" if not connected else "RESUME_REQUIRED"

    def resume(self, _payload: dict | None = None) -> dict:
        if not self.manual_playing or not self._requires_resume:
            raise RecordedLoopError("RESUME_NOT_REQUIRED", "No paused manual Loop to resume")
        state = self.navigation.state_response()
        if (state.get("connected") is not True
            or state.get("navigation_mode") != "DRIVING"
            or self._mode() != "MANUAL"
            or state.get("emergency_stop")
            or state.get("pi_safety", {}).get("fresh") is not True
            or state.get("pi_safety", {}).get("emergency_stop") is not False):
            raise RecordedLoopError("RESUME_UNSAFE", "Fresh Pi safety and MANUAL/DRIVING required")
        # Waking the worker is not motion authorization: STOP, new encoder
        # evidence, obstacle, and current safety generation are rechecked.
        self._resume_event.set()
        return {"ok": True, "resume_requested": True}

    def snapshot(self) -> dict:
        """Read-only modal state must work with Pi disconnected or no map."""
        state = self.navigation.state_response()
        active_map = state.get("active_map")
        try:
            key = self._selected_map()
        except (RecordedLoopError, OSError, ValueError, TypeError):
            key = None
        # Never create a cache entry for a nonexistent map. An existing map's
        # goals and bags remain visible even after a Pi disconnect.
        entry = self._entry(key) if key is not None else None
        with self._lock:
            goals = entry["goals"] if entry else {}
            bags = entry["bags"] if entry else {}
            return {
                "ok": True,
                "map_name": key[0] if key else (
                    str(active_map.get("map_name") or "") if isinstance(active_map, dict) else None
                ),
                "mode": str(state.get("robot_mode") or "UNKNOWN").upper(),
                "connected": state.get("connected") is True,
                "navigation_mode": state.get("navigation_mode"),
                "goals": [{"name": name, "pose": dict(pose)}
                          for name, pose in goals.items()],
                "recordings": [{"name": name, "events": len(bag["events"]),
                                "seconds": bag["seconds"]}
                               for name, bag in bags.items()],
                "recording": self._record is not None,
                "playing": self.playing, "running_kind": self._running_kind,
                "policy": self._policy, "status": self._status,
                "lap": self._laps, "error": self._error,
                "segment_index": self._manual_segment_index if self.manual_playing else None,
                "segment_elapsed_ms": round(self._manual_elapsed_ms, 2) if self.manual_playing else None,
                "replay_generation": self._replay_generation if self.manual_playing else None,
                "resume_required": self._requires_resume,
            }

    def toggle_goal(self, data: dict) -> dict:
        self._idle()
        key = self._active()
        pose = pose_dict(data.get("pose"))
        entry = self._entry(key)
        with self._lock:
            if self._record is not None:
                raise RecordedLoopError("RECORDING_ACTIVE", "Save or Delete recording first")
            for name, current in list(entry["goals"].items()):
                if math.hypot(current["x"] - pose["x"], current["y"] - pose["y"]) <= 0.20:
                    del entry["goals"][name]
                    return {"ok": True, "removed": name}
            # Overwrite the oldest name after A..Z,a..z. Preserve deterministic
            # insertion order so AUTO Loop runs in displayed goal order.
            name = GOAL_ALPHABET[entry["next"] % len(GOAL_ALPHABET)]
            entry["next"] += 1
            entry["goals"].pop(name, None)
            entry["goals"][name] = pose
            return {"ok": True, "added": name}

    def remove_goal(self, data: dict) -> dict:
        self._idle()
        key = self._active()
        name = data.get("name")
        with self._lock:
            goals = self._entry(key)["goals"]
            if name not in goals:
                raise RecordedLoopError("GOAL_MISSING", "Goal not found")
            del goals[name]
        return {"ok": True, "removed": name}

    def record(self) -> dict:
        self._idle()
        if self.navigation.state_response().get("connected") is not True:
            raise RecordedLoopError("PI_OFFLINE", "Pi connection required to record motion")
        old_task = getattr(self.recorded, "_play_task", None)
        if old_task is not None and not old_task.done():
            raise RecordedLoopError("LOOP_BUSY", "Stop legacy replay before recording")
        if self._mode() != "MANUAL":
            raise RecordedLoopError("MANUAL_REQUIRED", "Record a bag while the robot is in MANUAL")
        key = self._active()
        with self._lock:
            if self._record is not None:
                raise RecordedLoopError("ALREADY_RECORDING", "A recording is already active")
            self._record = {"key": key, "start": time.monotonic(),
                            "events": [], "error": None}
        return {"ok": True}

    def note_motion_ack(self, command: dict, delivered: bool) -> None:
        """Observe authenticated command-id-matched ACKs, never queued requests."""
        if not delivered or not isinstance(command, dict):
            return
        kind = command.get("type")
        if kind not in {"move", "stop", "emergency_stop"}:
            return
        # Dashboard STOP/E-STOP is an external operator interrupt. The
        # replay engine's own STOPs bypass the dashboard observer.
        if kind in {"stop", "emergency_stop"} and self.manual_playing:
            self._cancelled = True
        with self._lock:
            recording = self._record
            if recording is None or recording["error"]:
                return
            elapsed = time.monotonic() - recording["start"]
            if elapsed > MAX_RECORD_SECONDS or len(recording["events"]) >= MAX_EVENTS:
                recording["error"] = "RECORDING_LIMIT"
                return
            if kind == "emergency_stop":
                recording["error"] = "EMERGENCY_STOP"
                return
            if kind == "move":
                try:
                    speed = float(command["speed"])
                    direction = str(command["direction"]).strip().lower()
                    if (not math.isfinite(speed) or not 0 <= speed <= 1
                        or direction not in {"forward", "backward", "left", "right",
                            "forward_left", "forward_right", "backward_left",
                            "backward_right", "rotate_left", "rotate_right"}):
                        raise ValueError()
                except (ValueError, TypeError, KeyError):
                    recording["error"] = "INVALID_MOTOR_COMMAND"
                    return
                event = {"type": "move", "direction": direction,
                         "speed": min(0.30, speed)}
            else:
                event = {"type": "stop"}
            recording["events"].append({"at": round(elapsed, 5), **event})

    async def _verified_stop(self) -> None:
        if not await self.recorded._play_stop():
            raise RecordedLoopError("STOP_NOT_CONFIRMED", "Pi STOP/encoder proof unavailable")

    async def save(self) -> dict:
        with self._lock:
            record = self._record
            if record is None:
                raise RecordedLoopError("NOT_RECORDING", "No active manual recording")
            if record["error"]:
                raise RecordedLoopError(record["error"], "Discard invalid recording")
        # Capture the STOP request timestamp BEFORE waiting for encoder
        # standstill. Encoder confirmation latency is not motor-on time.
        stop_offset = round(time.monotonic() - record["start"], 5)
        await self._verified_stop()
        with self._lock:
            # The last bag command stops at the operator's Save request.
            record["events"].append({"at": stop_offset, "type": "stop"})
            if self._record is not record:
                raise RecordedLoopError("RECORDING_CHANGED", "Recording changed while saving")
            events = list(record["events"])
            if not any(event["type"] == "move" and event["speed"] > 0 for event in events):
                raise RecordedLoopError("EMPTY_RECORDING", "Record a physical MOVE before Save")
            if record["key"] != self._active():
                raise RecordedLoopError("MAP_CHANGED", "Map changed during recording")
            entry = self._entry(record["key"])
            if len(entry["bags"]) >= MAX_SAVED_BAGS_PER_MAP:
                raise RecordedLoopError(
                    "BAG_LIMIT", "Delete an old Loop before saving another recording",
                )
            name = "Loop " + str(entry["bag_next"])
            entry["bag_next"] += 1
            entry["bags"][name] = {"events": events, "seconds": events[-1]["at"]}
            self._record = None
            return {"ok": True, "name": name}

    def delete(self, data: dict) -> dict:
        self._idle()
        key = self._active()
        with self._lock:
            if self._record is not None:
                self._record = None
                return {"ok": True, "discarded": True}
            name = data.get("name")
            bags = self._entry(key)["bags"]
            if name not in bags:
                raise RecordedLoopError("BAG_MISSING", "Select a saved recording")
            del bags[name]
            return {"ok": True, "deleted": name}

    def _log_manual_safety_diagnostic(self, stage: str) -> None:
        """Log safety state without changing authorization decisions."""
        try:
            import logging
            import time
            nav = self.navigation
            with nav._lock:
                now = time.time()
                last = nav._stop_confirmation_last_at
                data = {
                    "stage": stage,
                    "connected": nav._connected,
                    "robot_mode": nav._robot_mode,
                    "navigation_state": nav._state.get("navigation_state"),
                    "server_estop": nav._state.get("emergency_stop"),
                    "pi_estop": nav._pi_estop_latched,
                    "encoder_stop_violation": nav._encoder_stop_violation,
                    "patrol_transition": nav._patrol_transition is not None,
                    "patrol_transition_error": nav._patrol_transition_error is not None,
                    "sensor_recovery": nav._sensor_recovery is not None,
                    "pending_nav_success": nav._pending_nav_success is not None,
                    "stop_requested": nav._stop_commanded_at is not None,
                    "stop_sequence_present": nav._stop_confirmation_after_sequence is not None,
                    "stop_samples": nav._stop_confirmation_samples,
                    "stop_sample_age_sec": None if last is None else round(now - last, 3),
                    "stop_confirmed": nav._stop_confirmed_locked(now),
                    "pi_age_sec": nav._age(now, nav._pi_updated_at),
                    "safety_session_present": bool(nav._pi_safety_session),
                    "safety_epoch_valid": (
                        type(nav._pi_safety_epoch) is int and nav._pi_safety_epoch >= 0
                    ),
                }
            logging.getLogger(__name__).warning("manual safety diagnostic: %s", data)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("manual safety diagnostic failed")

    def _manual_state(self) -> dict:
        state = self.navigation.state_response()
        if self._cancelled:
            raise RuntimeError("REPLAY_CANCELLED")
        if (state.get("navigation_mode") != "DRIVING"
            or str(state.get("robot_mode") or "").upper() != "MANUAL"
            or state.get("emergency_stop")):
            raise RuntimeError("MANUAL_REPLAY_INTERRUPTED")
        return state

    async def _wait_connection_resume(self) -> None:
        state = self._manual_state()
        pi = state.get("pi_safety") or {}
        healthy = (
            state.get("connected") is True
            and pi.get("fresh") is True
            and pi.get("emergency_stop") is False
            and bool(pi.get("safety_session"))
        )
        replaced = (
            self._manual_pi_safety_session is not None
            and pi.get("safety_session") != self._manual_pi_safety_session
        )
        if (not healthy or replaced) and not self._requires_resume:
            self._requires_resume = True
            self._replay_generation += 1
            self._resume_event.clear()
        while self._requires_resume and not self._cancelled:
            state = self._manual_state()
            pi = state.get("pi_safety") or {}
            ready = (
                state.get("connected") is True
                and pi.get("fresh") is True
                and pi.get("emergency_stop") is False
                and bool(pi.get("safety_session"))
            )
            if not ready:
                self._status = "PAUSED_CONNECTION"
            else:
                self._status = "RESUME_REQUIRED"
                if self._resume_event.is_set():
                    self._resume_event.clear()
                    # A reconnect never inherits the previous STOP gate.
                    # Fresh Pi ACK and NEW encoder samples are mandatory.
                    await self._verified_stop()
                    context = self.navigation.motion_safety_context()
                    if context is None or context.get("safety_session") != pi["safety_session"]:
                        raise RuntimeError("MANUAL_RESUME_SAFETY_FAILED")
                    self._manual_pi_safety_session = pi["safety_session"]
                    self._replay_generation += 1
                    self._requires_resume = False
                    self._status = "RUNNING"
                    return
            await asyncio.sleep(0.10)
        if self._cancelled:
            raise RuntimeError("REPLAY_CANCELLED")

    async def _manual_send(self, packet: dict) -> float:
        """Return *only* Pi-confirmed applied milliseconds for one pulse."""
        if self._cancelled or self._mode() != "MANUAL" or self._requires_resume:
            raise RuntimeError("MANUAL_REPLAY_INTERRUPTED")
        context = self.navigation.motion_safety_context()
        if context is None or not self.navigation.motion_allowed():
            self._log_manual_safety_diagnostic("before_manual_pulse")
            raise RuntimeError("MANUAL_SAFETY_REVOKED")
        generation = self._replay_generation
        connection = self._connection_generation
        pulse_id = packet["pulse_id"]
        command = {
            **packet,
            "type": "manual_loop_pulse",
            "replay_generation": generation,
            "safety_session": context["safety_session"],
            "safety_epoch": context["safety_epoch"],
        }
        def preflight() -> bool:
            return (
                not self._cancelled and not self._requires_resume
                and self._replay_generation == generation
                and self._connection_generation == connection
                and self._mode() == "MANUAL"
                and self.navigation.motion_safety_context() == context
            )
        try:
            reply = await asyncio.wait_for(
                self.send(
                    self.navigation._robot_id, command,
                    motion_ack=True, preflight=preflight,
                ),
                timeout=MANUAL_ACK_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("MANUAL_PULSE_ACK_UNCERTAIN") from exc
        if not preflight():
            raise RuntimeError(
                "REPLAY_CANCELLED" if self._cancelled
                else "MANUAL_PULSE_ACK_UNCERTAIN"
            )
        if not self.navigation._pi_command_delivered(reply):
            # The Pi may have moved before its ACK was lost. Never fabricate
            # exposure time or replay this pulse using a new identity.
            raise RuntimeError("MANUAL_PULSE_ACK_UNCERTAIN")
        observed = reply.get("manual_loop_pulse") if isinstance(reply, dict) else None
        if not isinstance(observed, dict):
            raise RuntimeError("MANUAL_PULSE_ACK_INVALID")
        elapsed = observed.get("elapsed_ms")
        duration = packet["duration_ms"]
        if (observed.get("pulse_id") != pulse_id
            or observed.get("replay_generation") != generation
            or observed.get("duration_ms") != duration
            or observed.get("stopped") is not True
            or type(elapsed) is not int
            or not 0 <= elapsed <= duration):
            raise RuntimeError("MANUAL_PULSE_ACK_INVALID")
        if not self.navigation.note_authorized_motion(context):
            raise RuntimeError("MANUAL_MOTION_PERMISSION_REVOKED")
        self._manual_pi_safety_session = context["safety_session"]
        return float(elapsed)

    async def _wait_obstacle_clear(self, direction: str) -> float:
        def obstacle() -> bool | None:
            return path_blocked(
                self.recorded.get_live_scan(), direction,
                mount_x=env_float("LIDAR_X"), mount_y=env_float("LIDAR_Y"),
                mount_yaw=env_float("LIDAR_YAW"),
            )
        if obstacle() is False:
            return 0.0
        began = time.monotonic()
        await self._verified_stop()
        self._status = "PAUSED_OBSTACLE"
        stable_since = None
        while not self._cancelled:
            await self._wait_connection_resume()
            state = self._manual_state()
            if state.get("pi_safety", {}).get("fresh") is not True:
                continue
            if self.navigation.motion_safety_context() is None:
                raise RuntimeError("MANUAL_SAFETY_REVOKED")
            clear = obstacle()
            now = time.monotonic()
            if clear is False:
                stable_since = stable_since or now
                if now - stable_since >= 1.0:
                    self._status = "RUNNING"
                    return time.monotonic() - began
            else:
                stable_since = None
            await asyncio.sleep(0.10)
        raise RuntimeError("REPLAY_CANCELLED")

    async def _manual_idle_wait(self, duration_ms: float) -> None:
        deadline = time.monotonic() + duration_ms / 1000.0
        while not self._cancelled:
            wait_started = time.monotonic()
            await self._wait_connection_resume()
            resume_delay = time.monotonic() - wait_started
            if resume_delay > 0.15:
                deadline += resume_delay
            left = deadline - time.monotonic()
            if left <= 0:
                return
            before = time.monotonic()
            await asyncio.sleep(min(0.05, left))
            # A communication pause is not counted as the recorded STOP gap.
            if self._requires_resume:
                deadline += time.monotonic() - before
        raise RuntimeError("REPLAY_CANCELLED")

    async def _manual_bag(self, key: tuple[str, str], events: list[dict]) -> None:
        segments = compile_manual_segments(events)  # O(N), once per replay
        await self._wait_connection_resume()
        await self._verified_stop()
        self._status = "RUNNING"
        previous_direction = None
        last_map_check = 0.0
        while not self._cancelled:
            if self._active() != key:
                raise RuntimeError("MAP_CHANGED")
            for index, segment in enumerate(segments):
                if self._cancelled:
                    break
                await self._wait_connection_resume()
                self._manual_segment_index = index
                remaining_ms = float(segment["duration_ms"])
                self._manual_elapsed_ms = 0.0
                if segment["type"] == "stop":
                    await self._verified_stop()
                    previous_direction = None
                    await self._manual_idle_wait(remaining_ms)
                    continue
                direction = segment["direction"]
                if direction != previous_direction:
                    # A changed direction must not inherit a possibly rotating
                    # wheel, even if Pico's 120ms electrical deadtime passed.
                    await self._verified_stop()
                previous_direction = direction
                idle_pulses = 0
                while remaining_ms > 0.001 and not self._cancelled:
                    await self._wait_connection_resume()
                    if time.monotonic() - last_map_check >= 2.0:
                        if self._active() != key:
                            raise RuntimeError("MAP_CHANGED")
                        last_map_check = time.monotonic()
                    # A bounded Pi pulse makes obstacle reaction latency at
                    # most the pulse duration + transport (never 5s/event).
                    await self._wait_obstacle_clear(direction)
                    await self._wait_connection_resume()
                    duration = min(MANUAL_PULSE_MAX_MS, max(1, math.ceil(remaining_ms)))
                    self._manual_pulse_index += 1
                    pulse_id = (
                        f"{self._manual_session_id}:{self._replay_generation}:"
                        f"{self._laps}:{index}:{self._manual_pulse_index}"
                    )
                    try:
                        applied_ms = await self._manual_send({
                            "pulse_id": pulse_id,
                            "pulse_sequence": self._manual_pulse_index,
                            "direction": direction, "speed": segment["speed"],
                            "duration_ms": duration,
                        })
                    except RuntimeError as exc:
                        if str(exc) == "MANUAL_PULSE_ACK_UNCERTAIN":
                            # STOP may have executed locally, but unconfirmed
                            # exposure must never be re-sent or counted twice.
                            await self._verified_stop()
                        raise
                    if applied_ms < 1:
                        idle_pulses += 1
                        if idle_pulses >= 5:
                            raise RuntimeError("MANUAL_MOTOR_NO_PROGRESS")
                    else:
                        idle_pulses = 0
                    remaining_ms = max(0.0, remaining_ms - applied_ms)
                    self._manual_elapsed_ms += applied_ms
                await self._wait_connection_resume()
                await self._verified_stop()
            if not self._cancelled:
                await self._wait_connection_resume()
                await self._verified_stop()
                self._laps += 1
        # Not using Nav2 and never correcting the saved open-loop trajectory.

    @staticmethod
    def _certified_auto_trace(trace: Any, origin: dict, destination: dict) -> list[dict]:
        """Certify recorded AMCL motion, never replace it with a planned path."""
        if not isinstance(trace, list) or not 4 <= len(trace) <= 1500:
            return []
        try:
            poses = [pose_dict(point) for point in trace]
        except (ValueError, TypeError, KeyError):
            return []
        if (math.hypot(poses[0]["x"] - origin["x"],
                       poses[0]["y"] - origin["y"]) > 0.40
            or math.hypot(poses[-1]["x"] - destination["x"],
                          poses[-1]["y"] - destination["y"]) > 0.40
            or abs(math.atan2(
                math.sin(poses[-1]["yaw"] - destination["yaw"]),
                math.cos(poses[-1]["yaw"] - destination["yaw"]),
            )) > 0.70):
            return []
        distance = 0.0
        for previous, current in zip(poses, poses[1:]):
            step = math.hypot(current["x"] - previous["x"],
                              current["y"] - previous["y"])
            if step > 0.45:
                return []
            distance += step
        return poses if distance >= 0.20 else []

    async def _wait_for_cached_path(self, path: list[dict], key: tuple[str, str]) -> bool:
        """WAIT preserves a blocked learned route; REPLAN discards it."""
        ros = self.recorded.map_api.ros_control
        if self._policy == "REPLAN":
            return bool(await asyncio.to_thread(ros.loop_path_valid, path))
        clear_since = None
        self._status = "WAITING"
        while not self._cancelled:
            if self._active() != key or self._mode() != "AUTO":
                raise RuntimeError("AUTO_LOOP_CONTEXT_LOST")
            state = self.navigation.state_response()
            if state.get("emergency_stop") or not state.get("nav2_ready"):
                raise RuntimeError("AUTO_LOOP_SAFETY_LOST")
            valid = await asyncio.to_thread(ros.loop_path_valid, path)
            now = time.monotonic()
            if valid:
                clear_since = clear_since or now
                if now - clear_since >= 1.0:
                    self._status = "RUNNING"
                    return True
            else:
                clear_since = None
            await asyncio.sleep(0.20)
        return False

    async def _auto_goals(self, key: tuple[str, str], goals: list[dict]) -> None:
        if self._cancelled:
            return
        if len(goals) == 2:
            # PR #93's physical A<->B trace and pivot cache are unchanged.
            await self.navigation.plan_goal({"goal": goals[0], "loop_goals": goals})
            if self._cancelled:
                return
            await self.navigation.start_navigation()
            self._status = "RUNNING"
            while not self._cancelled:
                await asyncio.sleep(0.20)
                state = self.navigation.state_response()
                if state.get("navigation_state") in {"FAILED", "CANCELED", "EMERGENCY_STOPPED"}:
                    raise RuntimeError("AUTO_LOOP_FAILED: " + str(state.get("last_error")))
                if not state.get("loop_goals") and state.get("navigation_state") in {"READY", "SUCCEEDED"}:
                    raise RuntimeError("AUTO_LOOP_STOPPED")
            return

        # A complete multi-Goal lap consists of the learned physical segments
        # A->B, B->C, ... last->A. The first unanchored approach is not cached.
        ros = self.recorded.map_api.ros_control
        cache_key = tuple((round(g["x"], 4), round(g["y"], 4),
                           round(g["yaw"], 4)) for g in goals)
        entry = self._entry(key)
        with self._lock:
            paths = entry["auto_paths"].setdefault(cache_key, {})
        index = 0
        current_pose = self.recorded._pose()
        if current_pose is not None and math.hypot(
            current_pose["x"] - goals[0]["x"],
            current_pose["y"] - goals[0]["y"]) <= 0.35:
            index = 1

        while not self._cancelled:
            if self._active() != key or self._mode() != "AUTO":
                raise RuntimeError("AUTO_LOOP_CONTEXT_LOST")
            destination = goals[index]
            origin = goals[(index - 1) % len(goals)]
            current_pose = self.recorded._pose()
            anchored = (
                current_pose is not None
                and math.hypot(current_pose["x"] - origin["x"],
                              current_pose["y"] - origin["y"]) <= 0.35
            )
            path = paths.get(index) if anchored else None
            if path:
                # WAIT preserves the recorded path until unobstructed.
                # REPLAN invalidates blocked cache before requesting a new path.
                valid = await self._wait_for_cached_path(path, key)
                if self._cancelled:
                    return
                if self._policy == "REPLAN" and not valid:
                    paths.pop(index, None)
                    path = None
            if self._cancelled:
                return
            try:
                if path is None:
                    await self.navigation.plan_goal({"goal": destination})
                else:
                    await self.navigation.plan_goal(
                        {"goal": destination}, session_cached_path=path,
                    )
            except Exception as exc:
                code = getattr(exc, "error_code", "")
                if self._policy == "REPLAN" and path and code in {
                    "LOOP_PATH_BLOCKED", "LOOP_PATH_MISMATCH", "LOOP_PATH_INVALID",
                }:
                    paths.pop(index, None)
                    path = None
                    await self.navigation.plan_goal({"goal": destination})
                else:
                    raise
            if self._cancelled:
                return
            await self.navigation.start_navigation()
            self._status = "RUNNING"
            deadline = time.monotonic() + 300.0
            while not self._cancelled:
                await asyncio.sleep(0.20)
                state = self.navigation.state_response()
                nav_state = state.get("navigation_state")
                if nav_state == "SUCCEEDED":
                    break
                if nav_state in {"FAILED", "CANCELED", "EMERGENCY_STOPPED"}:
                    raise RuntimeError("AUTO_GOAL_FAILED: " + str(state.get("last_error")))
                if time.monotonic() > deadline:
                    raise RuntimeError("AUTO_GOAL_TIMEOUT")
            if self._cancelled:
                return
            # Only physically driven, stationary-completed traces may become
            # cache entries. No path is invented from planned geometry.
            trace = await asyncio.to_thread(ros.finish_loop_trace)
            if anchored and not path:
                certified = self._certified_auto_trace(trace, origin, destination)
                if certified:
                    paths[index] = certified
            index = (index + 1) % len(goals)
            if index == 0:
                self._laps += 1

    async def _run(self, key: tuple[str, str], kind: str,
                   bag: list[dict] | None, goals: list[dict] | None) -> None:
        try:
            if kind == "MANUAL":
                await self._manual_bag(key, bag or [])
            else:
                await self._auto_goals(key, goals or [])
            self._status = "STOPPED"
        except asyncio.CancelledError:
            self._status = "STOPPED"
        except Exception as exc:
            if str(exc) in {"REPLAY_CANCELLED", "MANUAL_REPLAY_INTERRUPTED"} and self._cancelled:
                self._status = "STOPPED"
            else:
                self._status = "FAILED"
                self._error = str(exc)[:180]
        finally:
            if kind == "AUTO" and self._status == "FAILED":
                # Never leave an active Nav2 Goal moving after a worker fault.
                # cancel_goal() re-enters the registered stop callback; the
                # callback's from_navigation path detects current_task below.
                try:
                    await self.navigation.cancel_goal()
                except Exception:
                    try:
                        await self.navigation.emergency_stop(
                            "LOOP_AUTO_ABORT_FAILED", automatic=True, force=True)
                    except Exception:
                        pass
            if getattr(self.navigation, "_loop_session_owner", None) is asyncio.current_task():
                self.navigation._loop_session_owner = None
            if kind == "MANUAL":
                try:
                    await self._verified_stop()
                except Exception:
                    self._status = "FAILED"
                    self._error = "STOP_NOT_CONFIRMED"
                    try:
                        await self.navigation.emergency_stop(
                            "LOOP_MANUAL_STOP_NOT_CONFIRMED", automatic=True, force=True)
                    except Exception:
                        pass

    def start(self, data: dict) -> dict:
        self._idle()
        if self.navigation.state_response().get("connected") is not True:
            raise RecordedLoopError("PI_OFFLINE", "Pi connection required to start LOOP")
        old_task = getattr(self.recorded, "_play_task", None)
        if old_task is not None and not old_task.done():
            raise RecordedLoopError(
                "LOOP_BUSY", "Stop legacy recorded playback before session Loop")
        if self._record is not None:
            raise RecordedLoopError("RECORDING_ACTIVE", "Save or Delete the recording first")
        key = self._active()
        entry = self._entry(key)
        mode = self._mode()
        policy = data.get("policy", "WAIT")
        if policy not in {"WAIT", "REPLAN"}:
            raise RecordedLoopError("INVALID_POLICY", "Choose Wait or Replan")
        if mode == "MANUAL":
            if policy == "REPLAN":
                raise RecordedLoopError("MANUAL_REPLAN_UNAVAILABLE",
                    "A manual bag has no navigation goal for Nav2 Replan. Choose Wait")
            name = data.get("name")
            bag = entry["bags"].get(name)
            if not bag:
                raise RecordedLoopError("BAG_MISSING", "Select a recorded manual Loop")
            events = [dict(event) for event in bag["events"]]
            goals = None
        elif mode == "AUTO":
            state = self.navigation.state_response()
            if not state.get("localization_ready") or not state.get("nav2_ready"):
                raise RecordedLoopError("NAVIGATION_NOT_READY", "AUTO requires AMCL/Nav2 readiness")
            names = data.get("goals")
            if not isinstance(names, list) or not 2 <= len(names) <= 52:
                raise RecordedLoopError("GOALS_REQUIRED", "Select at least two Goals")
            if len(names) != len(set(names)) or any(name not in entry["goals"] for name in names):
                raise RecordedLoopError("GOAL_MISSING", "Invalid Goal selection")
            goals = [dict(entry["goals"][name]) for name in names]
            if len(goals) == 2 and math.hypot(
                goals[0]["x"] - goals[1]["x"], goals[0]["y"] - goals[1]["y"]) < 1.20:
                raise RecordedLoopError("GOALS_TOO_CLOSE", "A and B must be 1.2m apart")
            events = None
        else:
            raise RecordedLoopError("DRIVE_MODE_REQUIRED", "Select AUTO or MANUAL")
        self._cancelled = False
        self._policy = policy
        self._status = "STARTING"
        self._requires_resume = False
        self._resume_event.clear()
        self._manual_session_id = uuid4().hex[:20]
        self._replay_generation += 1
        self._manual_segment_index = 0
        self._manual_elapsed_ms = 0
        self._manual_pulse_index = 0
        self._manual_pi_safety_session = None
        self._error = None
        self._laps = 0
        self._running_kind = mode
        self._task = asyncio.create_task(
            self._run(key, mode, events, goals), name="loop-session-replay")
        # The same Nav2 Goal ownership exclusion applies to MANUAL replay.
        self.navigation._loop_session_owner = self._task
        return {"ok": True, "started": True, "mode": mode}

    async def stop(self, from_navigation: bool = False) -> dict:
        task = self._task
        if task is None or task.done():
            return {"ok": True, "stopped": True}
        self._cancelled = True
        self._resume_event.set()
        if self._running_kind == "AUTO":
            with self.navigation._lock:
                self.navigation._loop_session_cancel_token += 1
        if from_navigation and task is asyncio.current_task():
            # Called re-entrantly while the AUTO task performs a fault STOP.
            # Awaiting itself would deadlock; cancel_goal owns the STOP.
            return {"ok": True, "stopped": True}
        if self._running_kind == "AUTO":
            if from_navigation:
                # NavigationControlApi calls this BEFORE switching Pi mode.
                # Cancel the active ROS action and physically STOP before
                # releasing the transition. This path must not call
                # navigation.cancel_goal(), which would recurse into the
                # registered stop callback.
                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            self.recorded.map_api.ros_control.cancel_navigation),
                        timeout=4.0)
                    safe_terminal = (
                        isinstance(result, dict)
                        and result.get("completed") is True
                        and result.get("terminal") == "SUCCEEDED"
                    )
                    if not isinstance(result, dict) or not (
                        result.get("confirmed") is True or safe_terminal
                    ):
                        raise RuntimeError("NAV2_CANCEL_UNCONFIRMED")
                    await self._verified_stop()
                except Exception as exc:
                    try:
                        await self.navigation.emergency_stop(
                            "AUTO_LOOP_STOP_UNCONFIRMED", automatic=True, force=True)
                    except Exception:
                        pass
                    raise RecordedLoopError(
                        "AUTO_STOP_UNCONFIRMED", "Nav2 cancel/Pi STOP not confirmed") from exc
            else:
                # cancel_goal itself will re-enter the callback in the
                # guarded from_navigation path above.
                await self.navigation.cancel_goal()
        elif self._running_kind == "MANUAL":
            await self._verified_stop()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
        return {"ok": True, "stopped": True}

    async def close(self) -> None:
        if self.playing:
            await self.stop()
        with self._lock:
            self._maps.clear()
            self._record = None

    def _register(self, app: FastAPI) -> None:
        async def handle(request: Request, operation: Any, write: bool = True):
            if not request.session.get("user"):
                return JSONResponse({"ok": False, "error": "AUTH_REQUIRED"}, status_code=401)
            if write and (response := self.csrf_failure(request)):
                return response
            try:
                data = await request.json() if write else {}
                if not isinstance(data, dict):
                    raise RecordedLoopError("INVALID_BODY", "Expected JSON object")
                result = operation(data)
                if asyncio.iscoroutine(result):
                    result = await result
                return result
            except RecordedLoopError as exc:
                return JSONResponse({"ok": False, "error": str(exc),
                                     "error_code": exc.code}, status_code=409)
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)[:180],
                                     "error_code": "LOOP_OPERATION_FAILED"}, status_code=409)

        @app.get("/api/navigation/loop/state")
        async def get_state(request: Request):
            return await handle(request, lambda _: self.snapshot(), False)

        @app.post("/api/navigation/loop/goal")
        async def set_goal(request: Request):
            return await handle(request, self.toggle_goal)

        @app.post("/api/navigation/loop/goal/delete")
        async def delete_goal(request: Request):
            return await handle(request, self.remove_goal)

        @app.post("/api/navigation/loop/record")
        async def record(request: Request):
            return await handle(request, lambda _: self.record())

        @app.post("/api/navigation/loop/save")
        async def save(request: Request):
            return await handle(request, lambda _: self.save())

        @app.post("/api/navigation/loop/delete")
        async def delete(request: Request):
            return await handle(request, self.delete)

        @app.post("/api/navigation/loop/start")
        async def start(request: Request):
            return await handle(request, self.start)

        @app.post("/api/navigation/loop/stop")
        async def stop(request: Request):
            return await handle(request, lambda _: self.stop())

        @app.post("/api/navigation/loop/resume")
        async def resume(request: Request):
            return await handle(request, self.resume)
