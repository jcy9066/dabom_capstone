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
    OWNER_PREFIX = "dabom-gpu-navigation"
    LEGACY_CHILD_EXECUTABLES = {
        "MAPPING": frozenset({"async_slam_toolbox_node"}),
        "DRIVING": frozenset(
            {
                "map_server",
                "amcl",
                "controller_server",
                "smoother_server",
                "planner_server",
                "behavior_server",
                "bt_navigator",
                "waypoint_follower",
                "velocity_smoother",
                "nav2_command_bridge",
            }
        ),
    }

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = Path(root_dir)
        self.log_dir = self.root_dir / "logs" / "system_control"
        self.ros_install_setup = self.root_dir / "navigation" / "ros" / "install" / "setup.bash"
        self.start_timeout_sec = env_float("NAV_PROCESS_START_TIMEOUT_SEC", minimum=0.2)
        self.stop_timeout_sec = env_float("NAV_PROCESS_STOP_TIMEOUT_SEC", minimum=0.2)
        self._lock = threading.RLock()

    def status(self) -> dict[str, Any]:
        mapping = self._mode_processes("MAPPING")
        driving = self._mode_processes("DRIVING")
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
            "mapping_pids": sorted(mapping),
            "driving_pids": sorted(driving),
            "mapping_pgids": sorted(self._mode_pgids("MAPPING")),
            "driving_pgids": sorted(self._mode_pgids("DRIVING")),
        }

    def transition(
        self,
        mode: str,
        map_yaml: str | None = None,
        restart: bool = False,
    ) -> dict[str, Any]:
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
            owned_processes = self._owned_processes(normalized)
            orphaned_owned_children = bool(owned_processes) and not matches
            stale_mapping = (
                normalized == "MAPPING"
                and bool(matches or owned_processes)
                and not self._mapping_children_healthy()
            )

            if (
                restart
                or len(matches) > 1
                or orphaned_owned_children
                or stale_mapping
            ):
                self._stop_locked(normalized)
                matches = []

            if not matches:
                self._start_locked(normalized, map_yaml)

            status = self.status()
            if status["mode"] != normalized:
                self._stop_locked(normalized)
                raise NavigationProcessError(
                    f"{normalized} launch did not reach a single running state."
                )
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

    def _proc_environment(self, entry: Path) -> dict[str, str]:
        try:
            parts = (entry / "environ").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            return {}

        environment: dict[str, str] = {}
        for part in parts:
            if not part or b"=" not in part:
                continue
            key, value = part.split(b"=", 1)
            environment[
                key.decode("utf-8", errors="replace")
            ] = value.decode("utf-8", errors="replace")
        return environment

    def _owner_name(self, mode: str) -> str:
        return f"{self.OWNER_PREFIX}-{mode}"

    def _owned_processes(self, mode: str) -> set[int]:
        proc = Path("/proc")
        if not proc.is_dir():
            return set()
        owner = self._owner_name(mode)
        found: set[int] = set()
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            environment = self._proc_environment(entry)
            if environment.get("DABOM_PROCESS_OWNER") == owner:
                found.add(int(entry.name))
        return found

    def _legacy_child_processes(self, mode: str) -> set[int]:
        proc = Path("/proc")
        if not proc.is_dir():
            return set()

        wanted = self.LEGACY_CHILD_EXECUTABLES[mode]
        found: set[int] = set()
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            args = self._proc_args(entry)
            if any(Path(arg).name in wanted for arg in args):
                found.add(int(entry.name))
        return found

    def _mode_processes(self, mode: str) -> set[int]:
        found = self._owned_processes(mode)
        found.update(item["pid"] for item in self._matching(mode))

        # Backward-compatible recovery for processes created before ownership
        # tagging existed. Only use executable discovery when no tagged process
        # exists, so a current healthy mode cannot absorb unrelated legacy nodes.
        if not found:
            found.update(self._legacy_child_processes(mode))
        return found

    def _mode_pgids(self, mode: str) -> set[int]:
        pgids: set[int] = set()
        for pid in self._mode_processes(mode):
            try:
                pgids.add(os.getpgid(pid))
            except ProcessLookupError:
                continue
        return {pgid for pgid in pgids if pgid > 1}

    def _signal_mode_groups(self, mode: str, sig: signal.Signals) -> None:
        for pgid in self._mode_pgids(mode):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                continue

    def _wait_mode_exit(self, mode: str, timeout_sec: float) -> bool:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if not self._mode_processes(mode):
                return True
            time.sleep(0.1)
        return not self._mode_processes(mode)

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

    def _process_exists(self, *names: str) -> bool:
        proc = Path("/proc")
        if not proc.is_dir():
            return False
        wanted = {str(name).strip() for name in names if str(name).strip()}
        if not wanted:
            return False
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            args = self._proc_args(entry)
            for arg in args:
                base = Path(arg).name
                if base in wanted:
                    return True
        return False

    def _mapping_children_healthy(self) -> bool:
        # A lingering ros2 launch parent is not enough. Mapping is only usable
        # when slam_toolbox and the dashboard map bridge are both alive.
        return self._process_exists(
            "async_slam_toolbox_node",
        ) and self._process_exists(
            "map_bridge",
        )

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
            # /scan is supplied from the RC car through LidarRosBridge.
            # odom->base_link is supplied from live RC encoder telemetry.
        ]
        if mode == "MAPPING":
            command.append("start_lidar:=false")
            command.append("start_rviz:=false")
        if mode == "DRIVING":
            command.append(f"map:={map_yaml}")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"navigation_{mode.lower()}.log"
        process_env = os.environ.copy()
        process_env["DABOM_PROCESS_OWNER"] = self._owner_name(mode)
        process_env["DABOM_NAV_MODE"] = mode

        with log_path.open("ab") as log_file:
            subprocess.Popen(
                self._ros_command(command),
                cwd=self.root_dir,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=process_env,
            )
        time.sleep(self.start_timeout_sec)

    def _stop_locked(self, mode: str) -> None:
        if not self._mode_processes(mode):
            return

        self._signal_mode_groups(mode, signal.SIGTERM)
        if self._wait_mode_exit(mode, self.stop_timeout_sec):
            return

        self._signal_mode_groups(mode, signal.SIGKILL)
        if self._wait_mode_exit(mode, self.stop_timeout_sec):
            return

        survivors = sorted(self._mode_processes(mode))
        raise NavigationProcessError(
            f"{mode} processes survived SIGKILL: {survivors}"
        )
