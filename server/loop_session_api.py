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
from collections import OrderedDict
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from server.recorded_loop_core import RecordedLoopError, pose_dict
from server.recorded_loop_obstacle import path_blocked
from server.env_config import env_float

GOAL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
MAX_EVENTS = 10000
MAX_RECORD_SECONDS = 3600.0


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
        self._register(app)

    def _active(self) -> tuple[str, str]:
        return self.recorded._active()

    def _entry(self, key: tuple[str, str]) -> dict:
        with self._lock:
            return self._maps.setdefault(
                key, {"goals": OrderedDict(), "next": 0, "bags": OrderedDict(), "bag_next": 1})

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

    def snapshot(self) -> dict:
        key = self._active()
        entry = self._entry(key)
        with self._lock:
            return {
                "ok": True, "map_name": key[0], "mode": self._mode(),
                "goals": [{"name": name, "pose": dict(pose)}
                          for name, pose in entry["goals"].items()],
                "recordings": [{"name": name, "events": len(bag["events"]),
                                "seconds": bag["seconds"]}
                               for name, bag in entry["bags"].items()],
                "recording": self._record is not None,
                "playing": self.playing, "running_kind": self._running_kind,
                "policy": self._policy, "status": self._status,
                "lap": self._laps, "error": self._error,
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
        # Saving is a physical STOP operation; no start/end pose check.
        await self._verified_stop()
        with self._lock:
            if self._record is not record:
                raise RecordedLoopError("RECORDING_CHANGED", "Recording changed while saving")
            events = list(record["events"])
            if not any(event["type"] == "move" and event["speed"] > 0 for event in events):
                raise RecordedLoopError("EMPTY_RECORDING", "Record a physical MOVE before Save")
            if record["key"] != self._active():
                raise RecordedLoopError("MAP_CHANGED", "Map changed during recording")
            entry = self._entry(record["key"])
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

    async def _manual_send(self, packet: dict) -> None:
        if self._cancelled or self._mode() != "MANUAL":
            raise RuntimeError("MANUAL_REPLAY_INTERRUPTED")
        context = self.navigation.motion_safety_context()
        if context is None:
            raise RuntimeError("MANUAL_SAFETY_REVOKED")
        command = {**packet, "safety_session": context["safety_session"],
                   "safety_epoch": context["safety_epoch"]}
        delivered = await self.send(
            self.navigation._robot_id, command,
            preflight=lambda: (not self._cancelled and self._mode() == "MANUAL"
                               and self.navigation.motion_safety_context() == context))
        if not delivered or self._cancelled:
            raise RuntimeError("PI_MOTOR_ACK_FAILED")
        if packet["type"] == "move" and not self.navigation.note_authorized_motion(context):
            raise RuntimeError("MOTION_PERMISSION_REVOKED")

    async def _wait_obstacle_clear(self, direction: str) -> float:
        def obstacle() -> bool | None:
            return path_blocked(
                self.recorded.get_live_scan(), direction,
                mount_x=env_float("LIDAR_X"), mount_y=env_float("LIDAR_Y"),
                mount_yaw=env_float("LIDAR_YAW"))
        if obstacle() is False:
            return 0.0
        began = time.monotonic()
        await self._verified_stop()
        self._status = "WAITING"
        stable_since = None
        while not self._cancelled:
            if self._mode() != "MANUAL" or self.navigation.motion_safety_context() is None:
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

    async def _manual_bag(self, key: tuple[str, str], events: list[dict]) -> None:
        await self._verified_stop()
        while not self._cancelled:
            if self._active() != key:
                raise RuntimeError("MAP_CHANGED")
            lap_start = time.monotonic()
            paused = 0.0
            for event in events:
                while not self._cancelled:
                    remaining = lap_start + event["at"] + paused - time.monotonic()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(0.05, remaining))
                if self._cancelled:
                    break
                if self._active() != key:
                    raise RuntimeError("MAP_CHANGED")
                if event["type"] == "move":
                    if event["speed"] > 0:
                        paused += await self._wait_obstacle_clear(event["direction"])
                    await self._manual_send({
                        "type": "move", "direction": event["direction"],
                        "speed": event["speed"]})
                else:
                    await self._verified_stop()
            if not self._cancelled:
                await self._verified_stop()
                self._laps += 1
        # A non-closed manual bag is intentionally replayed from the next
        # actual position. Never require a pose match or Nav2 correction.

    async def _auto_goals(self, key: tuple[str, str], goals: list[dict]) -> None:
        if len(goals) == 2:
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
        # N>=3: use the existing single-goal Nav2 safety and completion
        # handshake for each leg. No independent motor-output implementation.
        index = 0
        while not self._cancelled:
            if self._active() != key:
                raise RuntimeError("MAP_CHANGED")
            goal = goals[index]
            await self.navigation.plan_goal({"goal": goal})
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
            self._status = "FAILED"
            self._error = str(exc)[:180]
        finally:
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
        self._error = None
        self._laps = 0
        self._running_kind = mode
        self._task = asyncio.create_task(
            self._run(key, mode, events, goals), name="loop-session-replay")
        return {"ok": True, "started": True, "mode": mode}

    async def stop(self) -> dict:
        task = self._task
        if task is None or task.done():
            return {"ok": True, "stopped": True}
        self._cancelled = True
        if self._running_kind == "AUTO":
            try:
                await self.navigation.cancel_goal()
            except Exception:
                try:
                    await self.navigation.emergency_stop(
                        "AUTO_LOOP_STOP_FAILED", automatic=True, force=True)
                except Exception:
                    pass
        else:
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
