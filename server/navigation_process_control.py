"""Mutually exclusive ROS launch lifecycle for mapping and driving modes."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from server.env_config import env_float, env_text


class NavigationProcessError(RuntimeError):
    pass


class NavigationProcessControl:
    """Starts exactly one mapping or localization+Nav2 launch process.

    Process mutations only happen when a navigation mode mutation API is called.
    Merely importing the FastAPI application never starts ROS.
    """

    MODES = frozenset({"MAPPING", "DRIVING"})

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = Path(root_dir)
        self.log_dir = self.root_dir / "logs" / "system_control"
        self.ros_install_setup = self.root_dir / "navigation" / "ros" / "install" / "setup.bash"
        self.start_timeout_sec = env_float("NAV_PROCESS_START_TIMEOUT_SEC", minimum=0.2)
        self.stop_timeout_sec = env_float("NAV_PROCESS_STOP_TIMEOUT_SEC", minimum=0.2)
        self._lock = threading.RLock()

    def status(self) -> dict[str, Any]:
        mapping = self._matching("MAPPING")
        driving = self._matching("DRIVING")
        if mapping and driving:
            mode = "CONFLICT"
        elif mapping:
            mode = "MAPPING"
        elif driving:
            mode = "DRIVING"
        else:
            mode = "STOPPED"
        return {
            "available": Path("/proc").is_dir(),
            "mode": mode,
            "mapping_pids": [item["pid"] for item in mapping],
            "driving_pids": [item["pid"] for item in driving],
        }

    def transition(self, mode: str, map_yaml: str | None = None) -> dict[str, Any]:
        normalized = str(mode).strip().upper()
        if normalized not in self.MODES:
            raise NavigationProcessError(f"Unsupported navigation mode: {mode}")
        if not Path("/proc").is_dir():
            raise NavigationProcessError("ROS process lifecycle control requires Linux /proc.")
        if normalized == "DRIVING" and not map_yaml:
            raise NavigationProcessError("Driving mode requires a saved map YAML path.")

        with self._lock:
            # Older dashboard builds could start `ros2 run ... map_bridge`
            # independently. It must not coexist with the map_bridge owned by
            # mapping/localization launch files.
            self._stop_legacy_map_bridges()

            other = "DRIVING" if normalized == "MAPPING" else "MAPPING"
            self._stop_locked(other)
            matches = self._matching(normalized)
            if len(matches) > 1:
                self._stop_locked(normalized)
                matches = []
            if not matches:
                self._start_locked(normalized, map_yaml)
            status = self.status()
            if status["mode"] != normalized:
                raise NavigationProcessError(f"{normalized} launch did not reach a running state.")
            return status

    def stop(self, mode: str | None = None) -> dict[str, Any]:
        with self._lock:
            targets = self.MODES if mode is None else {str(mode).strip().upper()}
            for target in targets:
                if target in self.MODES:
                    self._stop_locked(target)
            if mode is None:
                self._stop_legacy_map_bridges()
            return self.status()

    def _proc_args(self, entry: Path) -> list[str]:
        try:
            return [
                part.decode("utf-8", errors="replace")
                for part in (entry / "cmdline").read_bytes().split(b"\0")
                if part
            ]
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            return []

    def _matching(self, mode: str) -> list[dict[str, Any]]:
        proc = Path("/proc")
        if not proc.is_dir():
            return []
        launch_name = "mapping.launch.py" if mode == "MAPPING" else "navigation.launch.py"
        found: list[dict[str, Any]] = []
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            args = self._proc_args(entry)
            if any(
                Path(args[index]).name == "ros2"
                and args[index + 1:index + 4] == ["launch", "patrol_navigation", launch_name]
                for index in range(len(args))
            ):
                found.append({"pid": int(entry.name), "args": args})
        return found

    def _matching_legacy_map_bridges(self) -> list[dict[str, Any]]:
        proc = Path("/proc")
        if not proc.is_dir():
            return []
        found: list[dict[str, Any]] = []
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            args = self._proc_args(entry)
            if any(
                Path(args[index]).name == "ros2"
                and args[index + 1:index + 4] == ["run", "patrol_navigation", "map_bridge"]
                for index in range(len(args))
            ):
                found.append({"pid": int(entry.name), "args": args})
        return found

    def _stop_legacy_map_bridges(self) -> None:
        targets = self._matching_legacy_map_bridges()
        for item in targets:
            try:
                if os.getpgid(item["pid"]) == item["pid"]:
                    os.killpg(item["pid"], signal.SIGTERM)
                else:
                    os.kill(item["pid"], signal.SIGTERM)
            except ProcessLookupError:
                continue

        deadline = time.monotonic() + self.stop_timeout_sec
        while targets and time.monotonic() < deadline:
            time.sleep(0.1)
            targets = self._matching_legacy_map_bridges()

        for item in targets:
            try:
                if os.getpgid(item["pid"]) == item["pid"]:
                    os.killpg(item["pid"], signal.SIGKILL)
                else:
                    os.kill(item["pid"], signal.SIGKILL)
            except ProcessLookupError:
                continue

    def _ros_command(self, command: list[str]) -> list[str]:
        source_parts = ["source /opt/ros/humble/setup.bash"]
        if self.ros_install_setup.exists():
            source_parts.append(f"source {shlex.quote(str(self.ros_install_setup))}")
        source_parts.extend(
            [
                f"export ROS_DOMAIN_ID={shlex.quote(env_text('ROS_DOMAIN_ID'))}",
                f"export ROS_LOCALHOST_ONLY={shlex.quote(env_text('ROS_LOCALHOST_ONLY'))}",
                f"exec {shlex.join(command)}",
            ]
        )
        return ["bash", "-lc", " && ".join(source_parts)]

    def _start_locked(self, mode: str, map_yaml: str | None) -> None:
        launch_name = "mapping.launch.py" if mode == "MAPPING" else "navigation.launch.py"
        command = [
            "ros2",
            "launch",
            "patrol_navigation",
            launch_name,
            f"server_base_url:={env_text('SERVER_BASE_URL')}",
            f"robot_id:={env_text('ROBOT_ID')}",
            "start_lidar:=false",
            "start_fake_odom:=false",
        ]
        if mode == "MAPPING":
            command.append("start_rviz:=false")
        if mode == "DRIVING":
            command.append(f"map:={map_yaml}")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"navigation_{mode.lower()}.log"
        with log_path.open("ab") as log_file:
            subprocess.Popen(
                self._ros_command(command),
                cwd=self.root_dir,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        time.sleep(self.start_timeout_sec)

    def _stop_locked(self, mode: str) -> None:
        targets = self._matching(mode)
        for item in targets:
            try:
                if os.getpgid(item["pid"]) == item["pid"]:
                    os.killpg(item["pid"], signal.SIGTERM)
                else:
                    os.kill(item["pid"], signal.SIGTERM)
            except ProcessLookupError:
                continue
        deadline = time.monotonic() + self.stop_timeout_sec
        while targets and time.monotonic() < deadline:
            time.sleep(0.1)
            targets = self._matching(mode)
        for item in targets:
            try:
                if os.getpgid(item["pid"]) == item["pid"]:
                    os.killpg(item["pid"], signal.SIGKILL)
                else:
                    os.kill(item["pid"], signal.SIGKILL)
            except ProcessLookupError:
                continue
