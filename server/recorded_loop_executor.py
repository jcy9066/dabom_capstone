"""Motor-command route executor with injection-only I/O, never direct Pi access.

The caller MUST provide fail-closed authorization, Pi-ACK-confirmed motor I/O,
physical STOP confirmation, fresh localization, obstacle clearance and Nav2
replan callbacks. Without those callbacks this engine cannot drive anything.
"""
from __future__ import annotations

import asyncio
import math
import time
from uuid import uuid4
from typing import Any, Awaitable, Callable

from server.recorded_loop_core import motor_command, pose_dict
from server.recorded_loop_runtime import Playback, ReplayStatus


class ReplayFailure(RuntimeError):
    pass


class RecordedMotorExecutor:
    TICK_SEC = 0.08
    ROUTE_TOLERANCE_M = 0.55
    GOAL_TOLERANCE_M = 0.40
    HEADING_TOLERANCE_RAD = 0.70
    CLEAR_CONFIRM_SEC = 1.0
    DIRECTION_HANDOFF_TIMEOUT_SEC = 3.5

    def __init__(
        self,
        *,
        authorize: Callable[[], Awaitable[bool]],
        drive: Callable[[dict], Awaitable[bool | dict | None]],
        stop: Callable[[], Awaitable[bool]],
        pose: Callable[[], Awaitable[dict | None]],
        blocked: Callable[[dict], Awaitable[bool | None]],
        replan: Callable[[dict, list[dict]], Awaitable[bool]],
        on_status: Callable[[dict], None] | None = None,
        follow_recorded_path: Callable[[list[dict], str], Awaitable[bool]] | None = None,
    ):
        self.authorize, self.drive, self.stop = authorize, drive, stop
        self.pose, self.blocked, self.replan = pose, blocked, replan
        self.on_status = on_status or (lambda state: None)
        self.follow_recorded_path = follow_recorded_path
        self.playback = Playback()
        self.cancelled = False

    def _emit(self):
        self.on_status(self.playback.viewer_state())

    async def _health(self) -> None:
        if self.cancelled:
            raise ReplayFailure("REPLAY_CANCELLED")
        if not await self.authorize():
            raise ReplayFailure("MOTOR_AUTHORIZATION_REVOKED")

    @staticmethod
    def _distance(a: dict, b: dict) -> float:
        return math.hypot(a["x"] - b["x"], a["y"] - b["y"])

    @staticmethod
    def _heading_error(a: dict, b: dict) -> float:
        delta = a["yaw"] - b["yaw"]
        return abs(math.atan2(math.sin(delta), math.cos(delta)))

    async def _current(self) -> dict:
        pose = await self.pose()
        if pose is None:
            raise ReplayFailure("LOCALIZATION_UNAVAILABLE")
        return pose_dict(pose)

    async def _verified_stop(self) -> None:
        try:
            confirmed = await asyncio.wait_for(self.stop(), timeout=4.0)
        except (asyncio.TimeoutError, OSError) as exc:
            raise ReplayFailure("STOP_NOT_CONFIRMED") from exc
        if not confirmed:
            raise ReplayFailure("STOP_NOT_CONFIRMED")

    async def _wait_clear(self, command: dict) -> None:
        """Pause clock while blocked. Unknown scan data is never clear."""
        await self._verified_stop()
        clear_since = None
        while not self.cancelled:
            await self._health()
            value = await self.blocked(command)
            now = time.monotonic()
            if value is False:
                if clear_since is None:
                    clear_since = now
                if now - clear_since >= self.CLEAR_CONFIRM_SEC:
                    self.playback.obstacle(False)
                    self._emit()
                    return
            else:
                clear_since = None
            await asyncio.sleep(0.10)
        raise ReplayFailure("REPLAY_CANCELLED")

    async def _replan(self, segment: dict, remaining: list[dict]) -> None:
        await self._verified_stop()
        await self._health()
        try:
            ok = await asyncio.wait_for(self.replan(segment, remaining), timeout=120.0)
        except (asyncio.TimeoutError, OSError) as exc:
            raise ReplayFailure("REPLAN_FAILED") from exc
        if not ok:
            raise ReplayFailure("REPLAN_FAILED")
        # Nav2 callback MUST finish with a physical STOP and a verified
        # pose at its advertised resume point. Resume only after re-checking.
        await self._health()

    async def run(self, segments: list[dict], policy: str = "WAIT",
                  max_laps: int | None = None) -> dict:
        if not segments or any(not s.get("poses") for s in segments):
            raise ReplayFailure("INVALID_RECORDED_ROUTE")
        if max_laps is not None and (type(max_laps) is not int or max_laps < 1):
            raise ReplayFailure("INVALID_LAPS")
        # A STOP arriving between task creation and run() must remain
        # effective; only __init__ initializes a new session's cancel flag.
        self.playback.start(policy)
        self._emit()
        try:
            # Mandatory physical STOP baseline for EVERY Recorded Loop start.
            # A stale historical stop or an old Pi ACK is not enough.
            # This runs before the first recorded motor command or Nav2 action.
            await self._verified_stop()
            await self._health()
            while not self.cancelled and (max_laps is None or self.playback.lap < max_laps):
                await self._health()
                segment = segments[self.playback.segment_index]
                commands = segment.get("commands")
                if not commands and segment.get("source") == "set_goal":
                    position = await self._current()
                    if (self._distance(position, segment["poses"][0]) > self.GOAL_TOLERANCE_M
                        or self._heading_error(position, segment["poses"][0]) > self.HEADING_TOLERANCE_RAD):
                        raise ReplayFailure("SEGMENT_START_MISMATCH")
                    if self.follow_recorded_path is None:
                        raise ReplayFailure("NAV2_FOLLOW_PATH_UNAVAILABLE")
                    await self._verified_stop()
                    await self._health()
                    followed = await asyncio.wait_for(
                        self.follow_recorded_path(segment["poses"], self.playback.policy.value),
                        timeout=120.0)
                    if not followed:
                        raise ReplayFailure("NAV2_FOLLOW_PATH_FAILED")
                    await self._verified_stop()
                    position = await self._current()
                    if self._distance(position, segment["poses"][-1]) > self.GOAL_TOLERANCE_M:
                        raise ReplayFailure("NAV2_END_POSE_MISMATCH")
                    self.playback.finish_segment(len(segments), True)
                    self._emit()
                    continue
                if not commands:
                    raise ReplayFailure("MANUAL_COMMANDS_REQUIRED")
                commands = [motor_command(c) for c in commands]
                command_ids = [uuid4().hex for _ in commands]
                position = await self._current()
                if (self._distance(position, segment["poses"][0]) > self.GOAL_TOLERANCE_M
                    or self._heading_error(position, segment["poses"][0]) > self.HEADING_TOLERANCE_RAD):
                    raise ReplayFailure("SEGMENT_START_MISMATCH")
                pending_since = None
                active_tick = None
                while self.playback.command_index < len(commands):
                    iteration_started = time.monotonic()
                    await self._health()
                    i = self.playback.command_index
                    command = commands[i]
                    position = await self._current()
                    # Open-loop replay cannot be trusted after substantial drift.
                    if min(self._distance(position, p) for p in segment["poses"]) > self.ROUTE_TOLERANCE_M:
                        raise ReplayFailure("ROUTE_DEVIATION")
                    obstacle = await self.blocked(command)
                    if obstacle is None:
                        raise ReplayFailure("OBSTACLE_SENSOR_UNAVAILABLE")
                    if obstacle:
                        self.playback.obstacle(True)
                        self._emit()
                        if self.playback.status == ReplayStatus.WAITING:
                            await self._wait_clear(command)
                            pending_since = None
                            active_tick = None
                            continue
                        if self.playback.status == ReplayStatus.REPLANNING:
                            await self._replan(segment, segment["poses"])
                            # Nav2 has reached this segment's recorded Goal,
                            # so never replay its now-obsolete motor tail.
                            position = await self._current()
                            if self._distance(position, segment["poses"][-1]) > self.GOAL_TOLERANCE_M:
                                raise ReplayFailure("REPLAN_END_POSE_MISMATCH")
                            self.playback.status = ReplayStatus.RUNNING
                            self.playback.reason = None
                            self.playback.command_index = len(commands)
                            self._emit()
                            break
                    now = time.monotonic()
                    if time.monotonic() - iteration_started > 0.5:
                        raise ReplayFailure("REPLAY_SCHEDULER_STALLED")
                    remaining_sec = max(0.0, command["duration"] - self.playback.command_elapsed)
                    preflight_elapsed = 0.0 if active_tick is None else now - active_tick
                    if preflight_elapsed > 0.5:
                        raise ReplayFailure("REPLAY_SCHEDULER_STALLED")
                    if active_tick is not None and preflight_elapsed > 0:
                        # The last DRIVE remained active during health/TF/scan
                        # reads. Credit that interval to its command before
                        # sending anything, and never consume an unsent tail.
                        self.playback.step(min(preflight_elapsed, remaining_sec), commands)
                        if self.playback.command_index != i:
                            active_tick = None
                            self._emit()
                            continue
                        remaining_sec = max(0.0, command["duration"] - self.playback.command_elapsed)
                    tick_started = time.monotonic()
                    applied = await self.drive({**command, "replay_command_id": command_ids[i]})
                    if isinstance(applied, dict):
                        progress = applied.get("elapsed")
                        if (type(progress) not in (int, float) or not math.isfinite(progress)
                            or not self.playback.command_elapsed <= progress <= command["duration"]):
                            raise ReplayFailure("INVALID_PI_REPLAY_PROGRESS")
                        if progress > self.playback.command_elapsed:
                            pending_since = None
                        elif pending_since is None:
                            pending_since = time.monotonic()
                        elif time.monotonic() - pending_since > self.DIRECTION_HANDOFF_TIMEOUT_SEC:
                            raise ReplayFailure("DIRECTION_HANDOFF_TIMEOUT")
                        self.playback.step(progress - self.playback.command_elapsed, commands)
                        active_tick = None  # Pi exposure is authoritative, never server RTT.
                        self._emit()
                        await asyncio.sleep(self.TICK_SEC)
                        continue
                    if applied is False:
                        raise ReplayFailure("PI_COMMAND_ACK_FAILED")
                    if applied is None:
                        # Interlock STOP/hold is acknowledged but applies no
                        # motion. Retry this command without consuming time.
                        if pending_since is None:
                            pending_since = time.monotonic()
                        active_tick = None
                        if time.monotonic() - pending_since > self.DIRECTION_HANDOFF_TIMEOUT_SEC:
                            raise ReplayFailure("DIRECTION_HANDOFF_TIMEOUT")
                        await asyncio.sleep(self.TICK_SEC)
                        continue
                    if applied is not True:
                        raise ReplayFailure("PI_COMMAND_ACK_FAILED")
                    pending_since = None
                    # Motor output starts before its ACK returns. Include
                    # that round trip in this command's allotted interval.
                    ack_elapsed = time.monotonic() - tick_started
                    await asyncio.sleep(max(0.0, min(self.TICK_SEC, remaining_sec) - ack_elapsed))
                    elapsed = time.monotonic() - tick_started
                    if elapsed > 0.5:
                        raise ReplayFailure("REPLAY_SCHEDULER_STALLED")
                    # Delayed ACKs belong to this command, never to a later
                    # command that has not yet been physically submitted.
                    # Generic test adapters retain the original clock; the
                    # real Pi adapter returns authoritative elapsed above.
                    self.playback.step(min(elapsed, remaining_sec), commands)
                    active_tick = (time.monotonic()
                                   if self.playback.command_index == i else None)
                    if self.playback.status == ReplayStatus.FAILED:
                        raise ReplayFailure(self.playback.reason or "REPLAY_FAILED")
                    self._emit()
                await self._verified_stop()
                position = await self._current()
                reached = (
                    self._distance(position, segment["poses"][-1]) <= self.GOAL_TOLERANCE_M
                    and self._heading_error(position, segment["poses"][-1]) <= self.HEADING_TOLERANCE_RAD
                )
                self.playback.finish_segment(len(segments), reached)
                if self.playback.status == ReplayStatus.FAILED:
                    raise ReplayFailure(self.playback.reason or "ENDPOINT_MISMATCH")
                self._emit()
            if self.cancelled:
                raise ReplayFailure("REPLAY_CANCELLED")
            await self._verified_stop()
            return {"ok": True, "laps": self.playback.lap}
        except (ReplayFailure, ValueError, RuntimeError) as exc:
            self.playback.fail(str(exc))
            self._emit()
            raise
        finally:
            # A failed final physical STOP MUST override successful lap
            # completion. Reporting success while wheels may still be moving
            # would defeat the E-STOP escalation in the API owner.
            try:
                await self._verified_stop()
            except Exception as exc:
                self.playback.fail("STOP_NOT_CONFIRMED")
                self._emit()
                raise ReplayFailure("STOP_NOT_CONFIRMED") from exc

    def cancel(self):
        self.cancelled = True
