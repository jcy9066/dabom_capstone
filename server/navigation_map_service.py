"""Validated saved-map registry and persistent active-map state."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.initial_pose_safety import decode_grid, evaluate_initial_pose_grid, PoseGridError


MAP_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class NavigationMapError(Exception):
    def __init__(self, error_code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise NavigationMapError("INVALID_MAP_METADATA", f"{field} must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise NavigationMapError("INVALID_MAP_METADATA", f"{field} must be a number.") from exc
    if not math.isfinite(number):
        raise NavigationMapError("INVALID_MAP_METADATA", f"{field} must be finite.")
    return number


@dataclass(frozen=True)
class SavedNavigationMap:
    map_name: str
    meta_path: Path
    yaml_path: Path
    pgm_path: Path
    raw_path: Path
    width: int
    height: int
    resolution: float
    origin_x: float
    origin_y: float
    origin_yaw: float
    saved_at_iso: str | None
    location: dict[str, Any] | None = None

    def public_dict(self, root_dir: Path) -> dict[str, Any]:
        return {
            "map_name": self.map_name,
            "saved_at": self.saved_at_iso,
            "width": self.width,
            "height": self.height,
            "resolution": self.resolution,
            "origin": {
                "x": self.origin_x,
                "y": self.origin_y,
                "yaw": self.origin_yaw,
            },
            "yaml": str(self.yaml_path.relative_to(root_dir)),
            "location": dict(self.location) if isinstance(self.location, dict) else None,
        }


class NavigationMapService:
    """Resolves only complete, in-repository saved maps."""

    def __init__(self, root_dir: Path, map_dir: Path):
        self.root_dir = root_dir.resolve()
        self.map_dir = map_dir.resolve()
        self.state_path = self.root_dir / "data" / "runtime" / "navigation_active_map.json"

    def list_maps(self) -> list[dict[str, Any]]:
        maps: list[SavedNavigationMap] = []
        for meta_path in self.map_dir.glob("*.meta.json"):
            try:
                maps.append(self._read_map(meta_path))
            except NavigationMapError:
                continue
        maps.sort(key=lambda item: item.saved_at_iso or "", reverse=True)
        return [item.public_dict(self.root_dir) for item in maps]

    def get_map(self, map_name: Any) -> SavedNavigationMap:
        if not isinstance(map_name, str) or not MAP_NAME_PATTERN.fullmatch(map_name):
            raise NavigationMapError("INVALID_MAP_NAME", "The selected map name is invalid.")
        meta_path = self.map_dir / f"{map_name}.meta.json"
        if not meta_path.exists():
            raise NavigationMapError("MAP_DOES_NOT_EXIST", "The selected map does not exist.", 404)
        return self._read_map(meta_path)

    def preview_map(self, map_name: Any) -> dict[str, Any]:
        selected_map = self.get_map(map_name)
        raw = self._verify_raw_map(selected_map.raw_path)
        revision = hashlib.sha256(
            selected_map.meta_path.read_bytes()
            + selected_map.raw_path.read_bytes()
            + selected_map.pgm_path.read_bytes()
        ).hexdigest()
        return {
            "map_name": selected_map.map_name,
            "width": selected_map.width,
            "height": selected_map.height,
            "resolution": selected_map.resolution,
            "origin": {"x": selected_map.origin_x,
                       "y": selected_map.origin_y,
                       "yaw": selected_map.origin_yaw},
            "data_encoding": raw.get("data_encoding", "raw"),
            "data": raw.get("data"),
            "map_revision": revision,
            "verified": True,
        }

    def validate_initial_pose(
        self,
        selected_map: SavedNavigationMap,
        payload: Any,
    ) -> dict[str, float]:
        if payload is None:
            raise NavigationMapError(
                "INITIAL_POSE_REQUIRED",
                "Initial pose is required for localization.",
            )
        value = payload
        if not isinstance(value, dict):
            raise NavigationMapError("INVALID_INITIAL_POSE", "Initial pose must be an object.")

        if any(field not in value for field in ("x", "y", "yaw_degrees")):
            raise NavigationMapError(
                "INITIAL_POSE_REQUIRED",
                "Initial pose x, y, and yaw_degrees are required.",
            )

        x = _finite_number(value.get("x"), "initial_pose.x")
        y = _finite_number(value.get("y"), "initial_pose.y")
        yaw_degrees = _finite_number(value.get("yaw_degrees"), "initial_pose.yaw_degrees")
        if not -180.0 <= yaw_degrees <= 180.0:
            raise NavigationMapError("INVALID_INITIAL_POSE", "Initial heading must be between -180 and 180 degrees.")

        dx = x - selected_map.origin_x
        dy = y - selected_map.origin_y
        cos_yaw = math.cos(selected_map.origin_yaw)
        sin_yaw = math.sin(selected_map.origin_yaw)
        local_x = cos_yaw * dx + sin_yaw * dy
        local_y = -sin_yaw * dx + cos_yaw * dy
        max_x = selected_map.width * selected_map.resolution
        max_y = selected_map.height * selected_map.resolution
        if not 0.0 <= local_x < max_x or not 0.0 <= local_y < max_y:
            raise NavigationMapError(
                "INITIAL_POSE_OUT_OF_BOUNDS",
                "Initial position is outside the selected map bounds.",
            )

        try:
            raw = self._verify_raw_map(selected_map.raw_path)
            cells = decode_grid(
                raw["data"], raw.get("data_encoding", "raw"),
                selected_map.width, selected_map.height,
            )
            risk = evaluate_initial_pose_grid(
                width=selected_map.width, height=selected_map.height,
                resolution=selected_map.resolution,
                origin={"x": selected_map.origin_x, "y": selected_map.origin_y,
                        "yaw": selected_map.origin_yaw},
                cells=cells, x=x, y=y,
            )
        except (KeyError, TypeError, ValueError, PoseGridError) as exc:
            raise NavigationMapError(
                "INVALID_MAP_DATA", f"Cannot validate map occupancy: {exc}", 409,
            ) from exc
        if risk["risk"] == "BLOCKED":
            raise NavigationMapError(
                "INITIAL_POSE_OCCUPIED", f"Initial position is blocked: {risk['reason']}", 409,
            )
        if risk["risk"] == "WARNING" and value.get("warning_acknowledged") is not True:
            raise NavigationMapError(
                "INITIAL_POSE_CONFIRMATION_REQUIRED",
                f"Initial position requires confirmation: {risk['reason']}", 409,
            )

        return {
            "x": x, "y": y,
            "yaw_degrees": yaw_degrees,
            "yaw": math.radians(yaw_degrees),
        }

    def mark_selected(self, selected_map: SavedNavigationMap) -> None:
        state = self._read_state()
        state["last_selected_map"] = selected_map.map_name
        state["updated_at"] = self._now_iso()
        self._write_state(state)

    def mark_active(
        self,
        selected_map: SavedNavigationMap,
        initial_pose: dict[str, float],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        active = selected_map.public_dict(self.root_dir)
        active["loaded_at"] = self._now_iso()
        active["initial_pose"] = {
            "x": initial_pose["x"],
            "y": initial_pose["y"],
            "yaw_degrees": initial_pose["yaw_degrees"],
            "yaw": initial_pose["yaw"],
        }
        active["verification"] = verification
        state = {
            "version": 1,
            "last_selected_map": selected_map.map_name,
            "active_map": active,
            "updated_at": self._now_iso(),
        }
        self._write_state(state)
        return active

    def rename_map(self, map_name: Any, new_map_name: Any) -> SavedNavigationMap:
        """Rename one complete saved-map bundle without accepting arbitrary paths."""
        selected_map = self.get_map(map_name)
        if not isinstance(new_map_name, str) or not MAP_NAME_PATTERN.fullmatch(new_map_name):
            raise NavigationMapError(
                "INVALID_MAP_NAME",
                "The new map name must use 1-64 letters, numbers, dots, hyphens, or underscores.",
            )
        if selected_map.map_name.casefold() == new_map_name.casefold():
            raise NavigationMapError("MAP_NAME_UNCHANGED", "The new map name is the same as the current name.")

        target_paths = {
            "pgm": self.map_dir / f"{new_map_name}.pgm",
            "yaml": self.map_dir / f"{new_map_name}.yaml",
            "meta": self.map_dir / f"{new_map_name}.meta.json",
            "raw": self.map_dir / f"{new_map_name}.raw.json",
        }
        if any(path.exists() or path.is_symlink() for path in target_paths.values()):
            raise NavigationMapError(
                "MAP_NAME_ALREADY_EXISTS",
                "A saved map or map file already uses the requested name.",
                409,
            )

        source_paths = {
            "pgm": selected_map.pgm_path,
            "yaml": selected_map.yaml_path,
            "meta": selected_map.meta_path,
            "raw": selected_map.raw_path,
        }
        target_contents = self._renamed_contents(selected_map, new_map_name)
        previous_state = self._read_state()
        updated_state = self._renamed_state(previous_state, selected_map.map_name, new_map_name, target_paths["yaml"])
        staged_paths = self._stage_renamed_files(target_contents)
        backup_paths: dict[str, Path] = {}
        committed_paths: list[Path] = []
        try:
            for key, source_path in source_paths.items():
                backup_path = self.map_dir / f".{source_path.name}.{uuid.uuid4().hex}.rename-backup"
                os.replace(source_path, backup_path)
                backup_paths[key] = backup_path
            for key, target_path in target_paths.items():
                os.replace(staged_paths[key], target_path)
                committed_paths.append(target_path)
            self._write_state(updated_state)
        except Exception:
            self._rollback_rename(source_paths, backup_paths, committed_paths)
            try:
                self._write_state(previous_state)
            except Exception:
                # The original exception remains more useful to the API caller.
                pass
            raise
        finally:
            for staged_path in staged_paths.values():
                if staged_path.exists():
                    staged_path.unlink()

        for backup_path in backup_paths.values():
            if backup_path.exists():
                backup_path.unlink()
        return self.get_map(new_map_name)

    def saved_state(self) -> dict[str, Any]:
        state = self._read_state()
        return {
            "last_selected_map": state.get("last_selected_map"),
            "active_map": state.get("active_map"),
            "updated_at": state.get("updated_at"),
        }

    def _renamed_contents(self, selected_map: SavedNavigationMap, new_map_name: str) -> dict[str, bytes]:
        try:
            metadata = json.loads(selected_map.meta_path.read_text(encoding="utf-8"))
            yaml_text = selected_map.yaml_path.read_text(encoding="utf-8")
            raw_payload = json.loads(selected_map.raw_path.read_text(encoding="utf-8"))
            pgm_bytes = selected_map.pgm_path.read_bytes()
        except (OSError, json.JSONDecodeError) as exc:
            raise NavigationMapError("INVALID_MAP_DATA", "The selected map files cannot be read.") from exc
        if not isinstance(metadata, dict) or not isinstance(raw_payload, dict):
            raise NavigationMapError("INVALID_MAP_DATA", "The selected map files are invalid.")

        image_line_index = next(
            (index for index, line in enumerate(yaml_text.splitlines()) if line.strip().startswith("image:")),
            None,
        )
        if image_line_index is None:
            raise NavigationMapError("INVALID_MAP_DATA", "Map YAML has no image entry.")
        yaml_lines = yaml_text.splitlines()
        indentation = yaml_lines[image_line_index][: len(yaml_lines[image_line_index]) - len(yaml_lines[image_line_index].lstrip())]
        yaml_lines[image_line_index] = f"{indentation}image: {new_map_name}.pgm"
        renamed_yaml = "\n".join(yaml_lines) + ("\n" if yaml_text.endswith(("\n", "\r")) else "")

        files = metadata.get("files")
        if not isinstance(files, dict):
            raise NavigationMapError("INVALID_MAP_METADATA", "Map file metadata is missing.")
        renamed_files = dict(files)
        renamed_files.update(
            {
                "pgm": self._relative_map_path(new_map_name, ".pgm"),
                "yaml": self._relative_map_path(new_map_name, ".yaml"),
                "meta": self._relative_map_path(new_map_name, ".meta.json"),
                "raw": self._relative_map_path(new_map_name, ".raw.json"),
            }
        )
        renamed_metadata = dict(metadata)
        renamed_metadata["map_name"] = new_map_name
        renamed_metadata["files"] = renamed_files
        if "map_name" in raw_payload:
            raw_payload = dict(raw_payload)
            raw_payload["map_name"] = new_map_name

        yaml_bytes = renamed_yaml.encode("utf-8")
        raw_bytes = (json.dumps(raw_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        # Rename changes YAML's image reference and possibly the RAW name.
        # Preserve the saved-map integrity contract by rehashing the new files.
        if int(renamed_metadata.get("bundle_version") or 1) >= 2:
            renamed_metadata["checksums"] = {
                "pgm": hashlib.sha256(pgm_bytes).hexdigest(),
                "yaml": hashlib.sha256(yaml_bytes).hexdigest(),
                "raw": hashlib.sha256(raw_bytes).hexdigest(),
            }

        return {
            "pgm": pgm_bytes,
            "yaml": yaml_bytes,
            "meta": (json.dumps(renamed_metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            "raw": raw_bytes,
        }

    def _relative_map_path(self, map_name: str, suffix: str) -> str:
        return (self.map_dir / f"{map_name}{suffix}").relative_to(self.root_dir).as_posix()

    def _stage_renamed_files(self, contents: dict[str, bytes]) -> dict[str, Path]:
        staged_paths: dict[str, Path] = {}
        try:
            for key, content in contents.items():
                fd, temporary_name = tempfile.mkstemp(prefix=".navigation-map-rename-", suffix=".tmp", dir=self.map_dir)
                with os.fdopen(fd, "wb") as temporary_file:
                    temporary_file.write(content)
                staged_paths[key] = Path(temporary_name)
            return staged_paths
        except Exception:
            for staged_path in staged_paths.values():
                if staged_path.exists():
                    staged_path.unlink()
            raise

    def _renamed_state(
        self,
        previous_state: dict[str, Any],
        old_name: str,
        new_name: str,
        new_yaml_path: Path,
    ) -> dict[str, Any]:
        updated_state = dict(previous_state)
        if updated_state.get("last_selected_map") == old_name:
            updated_state["last_selected_map"] = new_name
        active = updated_state.get("active_map")
        if isinstance(active, dict) and active.get("map_name") == old_name:
            renamed_active = dict(active)
            renamed_active["map_name"] = new_name
            renamed_active["yaml"] = str(new_yaml_path.relative_to(self.root_dir))
            updated_state["active_map"] = renamed_active
        updated_state["updated_at"] = self._now_iso()
        return updated_state

    @staticmethod
    def _rollback_rename(
        source_paths: dict[str, Path],
        backup_paths: dict[str, Path],
        committed_paths: list[Path],
    ) -> None:
        for target_path in reversed(committed_paths):
            if target_path.exists():
                target_path.unlink()
        for key, backup_path in backup_paths.items():
            if backup_path.exists():
                os.replace(backup_path, source_paths[key])

    def _read_map(self, meta_path: Path) -> SavedNavigationMap:
        meta_path = self._inside_map_dir(meta_path, "INVALID_MAP_METADATA")
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NavigationMapError("INVALID_MAP_METADATA", "Map metadata cannot be read.") from exc
        if not isinstance(metadata, dict):
            raise NavigationMapError("INVALID_MAP_METADATA", "Map metadata must be an object.")

        map_name = metadata.get("map_name")
        expected_name = meta_path.name.removesuffix(".meta.json")
        if map_name != expected_name or not MAP_NAME_PATTERN.fullmatch(str(map_name)):
            raise NavigationMapError("INVALID_MAP_METADATA", "Map metadata name does not match its file.")

        width = int(_finite_number(metadata.get("width"), "width"))
        height = int(_finite_number(metadata.get("height"), "height"))
        resolution = _finite_number(metadata.get("resolution"), "resolution")
        if width <= 0 or height <= 0 or resolution <= 0:
            raise NavigationMapError("INVALID_MAP_METADATA", "Map dimensions and resolution must be positive.")

        origin = metadata.get("origin")
        if not isinstance(origin, dict):
            raise NavigationMapError("INVALID_MAP_METADATA", "Map origin is missing.")
        origin_x = _finite_number(origin.get("x"), "origin.x")
        origin_y = _finite_number(origin.get("y"), "origin.y")
        origin_yaw = _finite_number(origin.get("yaw", 0.0), "origin.yaw")

        files = metadata.get("files")
        if not isinstance(files, dict):
            raise NavigationMapError("INVALID_MAP_METADATA", "Map file metadata is missing.")
        yaml_path = self._resolve_registered_file(files.get("yaml"), ".yaml")
        pgm_path = self._resolve_registered_file(files.get("pgm"), ".pgm")
        raw_path = self._resolve_registered_file(files.get("raw"), ".json")
        if (
            yaml_path.name != f"{map_name}.yaml"
            or pgm_path.name != f"{map_name}.pgm"
            or raw_path.name != f"{map_name}.raw.json"
        ):
            raise NavigationMapError("INVALID_MAP_METADATA", "Map file names do not match the selected map.")
        self._verify_yaml_image(yaml_path, pgm_path)
        raw_payload = self._verify_raw_map(raw_path)
        self._verify_bundle_integrity(
            metadata, pgm_path, yaml_path, raw_path, raw_payload,
            width, height, resolution, origin_x, origin_y, origin_yaw,
        )

        return SavedNavigationMap(
            map_name=map_name,
            meta_path=meta_path,
            yaml_path=yaml_path,
            pgm_path=pgm_path,
            raw_path=raw_path,
            width=width,
            height=height,
            resolution=resolution,
            origin_x=origin_x,
            origin_y=origin_y,
            origin_yaw=origin_yaw,
            saved_at_iso=metadata.get("saved_at_iso") if isinstance(metadata.get("saved_at_iso"), str) else None,
            location=dict(metadata["location"]) if isinstance(metadata.get("location"), dict) else None,
        )

    def _resolve_registered_file(self, value: Any, suffix: str) -> Path:
        if not isinstance(value, str) or not value:
            raise NavigationMapError("INVALID_MAP_METADATA", "Map file metadata is invalid.")
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts or relative.suffix != suffix:
            raise NavigationMapError("INVALID_MAP_METADATA", "Map file path is not allowed.")
        return self._inside_map_dir(self.root_dir / relative, "INVALID_MAP_METADATA")

    def _inside_map_dir(self, path: Path, error_code: str) -> Path:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.map_dir)
        except (OSError, RuntimeError, ValueError) as exc:
            raise NavigationMapError(error_code, "Map file is outside the saved-map directory.") from exc
        if not resolved.is_file():
            raise NavigationMapError(error_code, "Map file does not exist.")
        return resolved

    def _verify_yaml_image(self, yaml_path: Path, expected_pgm: Path) -> None:
        try:
            content = yaml_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise NavigationMapError("INVALID_MAP_DATA", "Map YAML cannot be read.") from exc
        image_line = next((line for line in content.splitlines() if line.strip().startswith("image:")), None)
        if image_line is None:
            raise NavigationMapError("INVALID_MAP_DATA", "Map YAML has no image entry.")
        image_value = image_line.split(":", 1)[1].strip().strip("'\"")
        image_path = Path(image_value)
        if not image_value or image_path.is_absolute() or ".." in image_path.parts:
            raise NavigationMapError("INVALID_MAP_DATA", "Map YAML image path is not allowed.")
        resolved_image = self._inside_map_dir(yaml_path.parent / image_path, "INVALID_MAP_DATA")
        if resolved_image != expected_pgm:
            raise NavigationMapError("INVALID_MAP_DATA", "Map YAML does not reference its registered PGM file.")

    def _verify_raw_map(self, raw_path: Path) -> None:
        try:
            payload = json.loads(raw_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NavigationMapError("INVALID_MAP_DATA", "Raw map data cannot be read.") from exc
        if not isinstance(payload, dict):
            raise NavigationMapError("INVALID_MAP_DATA", "Raw map data must be an object.")
        return payload

    def _verify_bundle_integrity(
        self, metadata, pgm_path, yaml_path, raw_path, raw_payload,
        width, height, resolution, origin_x, origin_y, origin_yaw,
    ) -> None:
        try:
            bundle_version = int(metadata.get("bundle_version") or 1)
        except (TypeError, ValueError) as exc:
            raise NavigationMapError(
                "INVALID_MAP_METADATA", "Invalid map bundle version."
            ) from exc
        if bundle_version >= 2:
            checksums = metadata.get("checksums")
            if not isinstance(checksums, dict):
                raise NavigationMapError("INVALID_MAP_DATA", "Map bundle checksum manifest is missing.")
            for name, path in (("pgm", pgm_path), ("yaml", yaml_path), ("raw", raw_path)):
                expected = checksums.get(name)
                try:
                    actual = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError as exc:
                    raise NavigationMapError(
                        "INVALID_MAP_DATA", f"Cannot read map {name}."
                    ) from exc
                if not isinstance(expected, str) or len(expected) != 64 or actual != expected:
                    raise NavigationMapError(
                        "MAP_CHECKSUM_MISMATCH", f"Map {name} checksum mismatch."
                    )
        else:
            # Historical maps predate RAW occupancy and bundle checksums.
            # Their legacy PGM/YAML verification still runs in get_map().
            return

        try:
            if (int(raw_payload["width"]) != width
                or int(raw_payload["height"]) != height
                or abs(float(raw_payload["resolution"]) - resolution) > 1e-9):
                raise ValueError("RAW map dimensions differ from metadata")
            origin = raw_payload["origin"]
            for actual, expected in ((origin["x"], origin_x), (origin["y"], origin_y),
                                     (origin.get("yaw", 0), origin_yaw)):
                if abs(float(actual) - expected) > 1e-8:
                    raise ValueError("RAW map origin differs from metadata")
            data = raw_payload["data"]
            if not isinstance(data, list):
                raise ValueError("RAW map data is missing")
            if raw_payload.get("data_encoding") == "rle":
                if any(
                    not isinstance(run, list) or len(run) != 2
                    or int(run[1]) <= 0
                    for run in data
                ):
                    raise ValueError("Invalid RAW map RLE data")
                count = sum(int(run[1]) for run in data)
            else:
                count = len(data)
            if count != width * height:
                raise ValueError("RAW map cell count differs from metadata")

            pgm = pgm_path.read_bytes()
            header, sep, pixels = pgm.partition(b"\n255\n")
            if (not sep or not header.startswith(b"P5\n")
                or f"{width} {height}".encode("ascii") not in header
                or len(pixels) != width * height):
                raise ValueError("PGM image dimensions or raster length mismatch")

            config = yaml_path.read_text(encoding="utf-8")
            lines = dict(
                line.split(":", 1) for line in config.splitlines()
                if ":" in line and not line.lstrip().startswith("#")
            )
            if abs(float(lines["resolution"].strip()) - resolution) > 1e-9:
                raise ValueError("YAML resolution mismatch")
            parsed_origin = ast.literal_eval(lines["origin"].strip())
            if len(parsed_origin) != 3 or any(
                abs(float(x) - y) > 1e-8
                for x, y in zip(parsed_origin, (origin_x, origin_y, origin_yaw))
            ):
                raise ValueError("YAML map origin mismatch")
        except (OSError, ValueError, TypeError, KeyError, IndexError, SyntaxError) as exc:
            raise NavigationMapError(
                "INVALID_MAP_DATA", f"Saved map data is inconsistent: {exc}"
            ) from exc

    def _read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=".navigation_active_map.",
            suffix=".tmp",
            dir=self.state_path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as temporary_file:
                json.dump(state, temporary_file, ensure_ascii=False, indent=2)
                temporary_file.write("\n")
            os.replace(temporary_name, self.state_path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
