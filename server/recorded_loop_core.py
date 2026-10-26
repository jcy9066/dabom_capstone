"""Map-bound recorded path storage. Never sends physical motion."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_DIRECTIONS = frozenset({"forward", "backward", "left", "right", "forward_left",
                         "forward_right", "backward_left", "backward_right"})
MAX_COMMANDS = 6000
MAX_POSES = 10000
MAX_DURATION = 0.45


class RecordedLoopError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def identifier(value: Any, field: str = "id") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RecordedLoopError("INVALID_ID", f"Invalid {field}")
    return value


def finite(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise RecordedLoopError("INVALID_NUMBER", f"{field} cannot be boolean")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RecordedLoopError("INVALID_NUMBER", f"Invalid {field}") from exc
    if not math.isfinite(result):
        raise RecordedLoopError("INVALID_NUMBER", f"Nonfinite {field}")
    return result


def pose_dict(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        raise RecordedLoopError("INVALID_POSE", "Pose must be an object")
    return {name: finite(value.get(name), name) for name in ("x", "y", "yaw")}


def motor_command(value: Any) -> dict[str, float | str]:
    if not isinstance(value, dict) or value.get("direction") not in _DIRECTIONS:
        raise RecordedLoopError("INVALID_COMMAND", "Unsupported direction")
    speed = finite(value.get("speed"), "speed")
    duration = finite(value.get("duration"), "duration")
    if not 0 < speed <= 0.30 or not 0 < duration <= MAX_DURATION:
        raise RecordedLoopError("INVALID_COMMAND", "Unsafe command limits")
    return {"direction": value["direction"], "speed": speed, "duration": duration}


def map_digest(yaml_path: Path) -> str:
    """Check immutable YAML and image bytes, not only a mutable map name."""
    yaml_path = Path(yaml_path).resolve(strict=True)
    raw = yaml_path.read_bytes()
    image_name = None
    for line in raw.decode("utf-8").splitlines():
        if line.lstrip().startswith("image:"):
            image_name = line.split(":", 1)[1].strip().strip("\"'")
            break
    if not image_name or Path(image_name).is_absolute() or ".." in Path(image_name).parts:
        raise RecordedLoopError("INVALID_MAP", "Image path must be relative")
    image_path = (yaml_path.parent / image_name).resolve(strict=True)
    if image_path.parent != yaml_path.parent or not image_path.is_file():
        raise RecordedLoopError("INVALID_MAP", "Image must remain beside YAML")
    digest = hashlib.sha256(raw)
    with image_path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class RecordedRouteStore:
    """Atomic, thread-safe, bounded persistent Goal/Segment/Route registry."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "maps": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecordedLoopError("STORE_CORRUPT", "Recorded route database unreadable") from exc
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("maps"), dict):
            raise RecordedLoopError("STORE_CORRUPT", "Invalid route database")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".recorded_", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _map(self, data: dict[str, Any], name: str, digest: str, create: bool) -> dict[str, Any]:
        maps = data["maps"]
        if name not in maps:
            if not create:
                return {"digest": digest, "goals": {}, "segments": {}, "routes": {}}
            maps[name] = {"digest": digest, "goals": {}, "segments": {}, "routes": {}}
        entry = maps[name]
        if entry.get("digest") != digest:
            raise RecordedLoopError("MAP_CHANGED", "Map content changed; cannot replay saved routes")
        return entry

    def snapshot(self, name: str, digest: str) -> dict[str, Any]:
        identifier(name, "map_name")
        with self._lock:
            return json.loads(json.dumps(self._map(self._load(), name, digest, False)))

    def put_goal(self, name: str, digest: str, goal_id: str, pose: dict) -> dict:
        identifier(name, "map_name")
        identifier(goal_id, "goal_id")
        validated = pose_dict(pose)
        with self._lock:
            db = self._load()
            self._map(db, name, digest, True)["goals"][goal_id] = validated
            self._save(db)
        return validated

    def put_segment(self, name: str, digest: str, segment_id: str, source: str,
                    start: str, end: str, commands: list[dict], poses: list[dict]) -> dict:
        identifier(name, "map_name")
        for key, val in (("segment_id", segment_id), ("start", start), ("end", end)):
            identifier(val, key)
        if start == end or source not in {"manual", "set_goal"}:
            raise RecordedLoopError("INVALID_SEGMENT", "Invalid direction/source")
        if not isinstance(commands, list) or len(commands) > MAX_COMMANDS:
            raise RecordedLoopError("INVALID_SEGMENT", "Too many motor commands")
        if not isinstance(poses, list) or not 2 <= len(poses) <= MAX_POSES:
            raise RecordedLoopError("INVALID_SEGMENT", "Physical trace requires >=2 poses")
        normalized = [motor_command(c) for c in commands]
        if source == "manual" and not normalized:
            raise RecordedLoopError("INVALID_SEGMENT", "Manual segment has no motor commands")
        clean_poses = [pose_dict(p) for p in poses]
        for a, b in zip(clean_poses, clean_poses[1:]):
            if math.hypot(b["x"] - a["x"], b["y"] - a["y"]) > 0.45:
                raise RecordedLoopError("LOCALIZATION_JUMP", "Recorded path jumped")
        with self._lock:
            db = self._load()
            entry = self._map(db, name, digest, True)
            if start not in entry["goals"] or end not in entry["goals"]:
                raise RecordedLoopError("GOAL_MISSING", "Save both Goals before a Segment")
            for goal_id, p in ((start, clean_poses[0]), (end, clean_poses[-1])):
                g = entry["goals"][goal_id]
                if math.hypot(p["x"] - g["x"], p["y"] - g["y"]) > 0.40:
                    raise RecordedLoopError("ENDPOINT_MISMATCH", f"Trace does not reach {goal_id}")
            saved = {"id": segment_id, "source": source, "start": start,
                     "end": end, "commands": normalized, "poses": clean_poses,
                     "saved_at": time.time()}
            entry["segments"][segment_id] = saved
            self._save(db)
        return saved

    def put_route(self, name: str, digest: str, route_id: str, segment_ids: list[str]) -> dict:
        identifier(name, "map_name")
        identifier(route_id, "route_id")
        if not isinstance(segment_ids, list) or not 2 <= len(segment_ids) <= 32:
            raise RecordedLoopError("INVALID_ROUTE", "Requires 2-32 segments")
        ids = [identifier(i, "segment_id") for i in segment_ids]
        with self._lock:
            db = self._load()
            entry = self._map(db, name, digest, False)
            segments = []
            for sid in ids:
                if sid not in entry["segments"]:
                    raise RecordedLoopError("SEGMENT_MISSING", f"Missing {sid}")
                segments.append(entry["segments"][sid])
            for prev, nxt in zip(segments, segments[1:] + segments[:1]):
                if prev["end"] != nxt["start"]:
                    raise RecordedLoopError("ROUTE_DISCONNECTED", "Segments must form a closed loop")
            entry = self._map(db, name, digest, True)
            saved = {"id": route_id, "segments": ids, "saved_at": time.time()}
            entry["routes"][route_id] = saved
            self._save(db)
        return saved

    def delete(self, name: str, digest: str, kind: str, item_id: str) -> None:
        identifier(name, "map_name")
        identifier(item_id)
        if kind not in {"goals", "segments", "routes"}:
            raise RecordedLoopError("INVALID_KIND", "Unknown kind")
        with self._lock:
            db = self._load()
            entry = self._map(db, name, digest, False)
            if kind == "goals" and any(
                s["start"] == item_id or s["end"] == item_id
                for s in entry["segments"].values()
            ):
                raise RecordedLoopError("GOAL_IN_USE", "Goal belongs to a segment")
            if kind == "segments" and any(
                item_id in r["segments"] for r in entry["routes"].values()
            ):
                raise RecordedLoopError("SEGMENT_IN_USE", "Segment belongs to a route")
            if item_id not in entry[kind]:
                raise RecordedLoopError("NOT_FOUND", "Record not found")
            del entry[kind][item_id]
            self._save(db)
