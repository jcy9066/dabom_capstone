"""Fail-closed recorded-loop timing and obstacle policy; no motor I/O."""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ReplayStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    REPLANNING = "REPLANNING"
    FAILED = "FAILED"


class ObstaclePolicy(str, Enum):
    WAIT = "WAIT"
    REPLAN = "REPLAN"


@dataclass
class Playback:
    policy: ObstaclePolicy = ObstaclePolicy.WAIT
    status: ReplayStatus = ReplayStatus.IDLE
    lap: int = 0
    segment_index: int = 0
    command_index: int = 0
    command_elapsed: float = 0.0
    reason: str | None = None

    def start(self, policy: str) -> None:
        if self.status not in {ReplayStatus.IDLE, ReplayStatus.FAILED}:
            raise RuntimeError("Replay already active: policy cannot change")
        self.policy = ObstaclePolicy(policy)
        self.status = ReplayStatus.RUNNING
        self.lap = self.segment_index = self.command_index = 0
        self.command_elapsed = 0.0
        self.reason = None

    def obstacle(self, blocked: bool) -> None:
        if self.status == ReplayStatus.RUNNING and blocked:
            self.status = ReplayStatus.WAITING if self.policy == ObstaclePolicy.WAIT else ReplayStatus.REPLANNING
            self.reason = "OBSTACLE"
        elif self.status == ReplayStatus.WAITING and not blocked:
            self.status = ReplayStatus.RUNNING
            self.reason = None

    def step(self, elapsed: float, commands: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Return a candidate only; caller must independently authorize Pi motors."""
        if self.status != ReplayStatus.RUNNING:
            return None
        if not math.isfinite(elapsed) or not 0 <= elapsed <= 0.5:
            self.fail("INVALID_TICK")
            return None
        if not commands or self.command_index >= len(commands):
            self.fail("INVALID_COMMAND_SEQUENCE")
            return None
        self.command_elapsed += elapsed
        while self.command_index < len(commands) and self.command_elapsed >= commands[self.command_index]["duration"]:
            self.command_elapsed -= commands[self.command_index]["duration"]
            self.command_index += 1
        if self.command_index >= len(commands):
            return None
        return dict(commands[self.command_index])

    def finish_segment(self, segment_count: int, at_endpoint: bool) -> None:
        if self.status != ReplayStatus.RUNNING:
            raise RuntimeError("Not running")
        if not at_endpoint or segment_count < 1:
            self.fail("ENDPOINT_MISMATCH")
            return
        self.segment_index = (self.segment_index + 1) % segment_count
        # One recorded Path runs exactly once; multi-segment closed Loops
        # retain their existing completed-lap accounting.
        self.lap += int(self.segment_index == 0)
        self.command_index = 0
        self.command_elapsed = 0.0

    def fail(self, reason: str) -> None:
        self.status = ReplayStatus.FAILED
        self.reason = reason

    def reset(self) -> None:
        self.status = ReplayStatus.IDLE
        self.reason = None
        self.command_index = self.segment_index = self.lap = 0
        self.command_elapsed = 0.0

    def viewer_state(self) -> dict[str, Any]:
        return {"state": self.status.value, "label": {
            ReplayStatus.IDLE: "준비",
            ReplayStatus.RUNNING: "순찰 중",
            ReplayStatus.WAITING: "대기 중",
            ReplayStatus.REPLANNING: "경로 재탐색 중",
            ReplayStatus.FAILED: "주행 중지",
        }[self.status], "policy": self.policy.value, "reason": self.reason, "lap": self.lap}
