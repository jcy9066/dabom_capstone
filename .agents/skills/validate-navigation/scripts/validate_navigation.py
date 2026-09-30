from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


PACKAGE_REL = Path("navigation/ros/patrol_navigation")


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str


@dataclass(frozen=True)
class RuntimeResult:
    status: str
    message: str
    output: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate dabom ROS 2 navigation files."
    )
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument(
        "--runtime",
        action="store_true",
        help="Run read-only ROS 2 runtime inspection when available.",
    )
    parser.add_argument("--show-runtime-output", action="store_true")
    parser.add_argument("--strict-warnings", action="store_true")
    parser.add_argument("--runtime-timeout-sec", type=float, default=10.0)
    return parser.parse_args()


def _is_repo_root(path: Path) -> bool:
    return (
        (path / PACKAGE_REL / "package.xml").is_file()
        and (path / "server").is_dir()
        and (path / "raspberry").is_dir()
    )


def find_repo_root(explicit: Path | None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit.expanduser())
    candidates.append(Path.cwd())
    try:
        candidates.extend(Path(__file__).resolve().parents)
    except OSError:
        pass

    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        for current in (resolved, *resolved.parents):
            key = os.path.normcase(str(current))
            if key in seen:
                continue
            seen.add(key)
            if _is_repo_root(current):
                return current

    raise RuntimeError(
        "dabom repository root를 찾지 못했습니다. "
        "--repo-root로 repository root를 지정하세요."
    )


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def add(findings: list[Finding], severity: str, code: str, message: str) -> None:
    findings.append(Finding(severity, code, message))


def require_tokens(
    source: str,
    tokens: tuple[str, ...],
    findings: list[Finding],
    code: str,
    subject: str,
    severity: str = "ERROR",
) -> None:
    for token in tokens:
        if token not in source:
            add(
                findings,
                severity,
                code,
                f"{subject}에서 확인하지 못했습니다: {token}",
            )


def check_required_files(package_dir: Path, findings: list[Finding]) -> None:
    required = (
        "package.xml",
        "setup.py",
        "setup.cfg",
        "launch/lidar.launch.py",
        "launch/mapping.launch.py",
        "launch/localization.launch.py",
        "launch/navigation.launch.py",
        "config/rplidar_a1m8.yaml",
        "config/slam_toolbox.yaml",
        "config/amcl.yaml",
        "config/nav2_params.yaml",
        "patrol_navigation/map_bridge.py",
        "patrol_navigation/nav2_command_bridge.py",
    )
    for relative in required:
        if not (package_dir / relative).exists():
            add(
                findings,
                "ERROR",
                "required-file",
                f"필수 navigation 파일이 없습니다: {PACKAGE_REL / relative}",
            )

    if not (package_dir / "rviz" / "mapping.rviz").is_file():
        add(
            findings,
            "WARN",
            "rviz-config",
            "rviz/mapping.rviz를 찾지 못했습니다.",
        )


def check_python_syntax(package_dir: Path, findings: list[Finding]) -> None:
    files = list((package_dir / "launch").glob("*.py"))
    files += list((package_dir / "patrol_navigation").glob("*.py"))
    files += [package_dir / "setup.py"]

    for path in sorted(set(files)):
        if not path.is_file():
            continue
        try:
            ast.parse(read_text(path), filename=str(path))
        except SyntaxError as exc:
            add(
                findings,
                "ERROR",
                "python-syntax",
                f"{path}:{exc.lineno}:{exc.offset} {exc.msg}",
            )


def check_setup(package_dir: Path, findings: list[Finding]) -> None:
    path = package_dir / "setup.py"
    if not path.is_file():
        return
    source = read_text(path)
    require_tokens(
        source,
        (
            "map_bridge = patrol_navigation.map_bridge:main",
            "patrol_navigation.nav2_command_bridge:main",
            'glob("launch/*.launch.py")',
            'glob("config/*.yaml")',
            'glob("rviz/*.rviz")',
        ),
        findings,
        "setup-contract",
        "setup.py",
    )


def check_package_xml(package_dir: Path, findings: list[Finding]) -> None:
    path = package_dir / "package.xml"
    if not path.is_file():
        return

    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        add(findings, "ERROR", "package-xml", f"package.xml 파싱 실패: {exc}")
        return

    deps = [
        element.text.strip()
        for element in root.iter()
        if element.tag.endswith("_depend") and element.text
    ]
    required = {
        "ament_python",
        "geometry_msgs",
        "launch",
        "launch_ros",
        "nav_msgs",
        "nav2_amcl",
        "nav2_map_server",
        "nav2_lifecycle_manager",
        "nav2_controller",
        "nav2_smoother",
        "nav2_planner",
        "nav2_behaviors",
        "nav2_bt_navigator",
        "nav2_waypoint_follower",
        "nav2_velocity_smoother",
        "rclpy",
        "rviz2",
        "sensor_msgs",
        "slam_toolbox",
        "tf2_ros",
    }
    for dep in sorted(required - set(deps)):
        add(findings, "ERROR", "package-dependency", f"dependency 누락: {dep}")

    for dep in sorted({name for name in deps if deps.count(name) > 1}):
        add(findings, "WARN", "duplicate-dependency", f"dependency 중복: {dep}")

    lidar = package_dir / "launch" / "lidar.launch.py"
    if lidar.is_file():
        source = read_text(lidar)
        if 'default_value="rplidar_ros"' in source and "rplidar_ros" not in deps:
            add(
                findings,
                "WARN",
                "lidar-dependency",
                "lidar.launch.py 기본 driver가 rplidar_ros인데 package.xml에 dependency가 없습니다.",
            )


def _declared_argument(call: ast.Call) -> str | None:
    if not isinstance(call.func, ast.Name) or call.func.id != "DeclareLaunchArgument":
        return None
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def check_orphan_launch_arguments(package_dir: Path, findings: list[Finding]) -> None:
    for path in sorted((package_dir / "launch").glob("*.launch.py")):
        try:
            tree = ast.parse(read_text(path), filename=str(path))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            name = _declared_argument(node.value)
            if name is not None:
                add(
                    findings,
                    "ERROR",
                    "orphan-launch-argument",
                    (
                        f"{path.name}:{node.lineno} DeclareLaunchArgument({name!r})가 "
                        "LaunchDescription에 추가되지 않은 독립 expression입니다."
                    ),
                )


def check_launch_contracts(package_dir: Path, findings: list[Finding]) -> None:
    checks = {
        "lidar.launch.py": (
            'default_value="/dev/ttyUSB0"',
            'default_value="115200"',
            'default_value="laser"',
            '"static_transform_publisher"',
            '"base_link"',
        ),
        "mapping.launch.py": (
            "slam_toolbox",
            "online_async_launch.py",
            '"/map"',
            '"/scan"',
            '"map_bridge"',
            '"start_fake_odom"',
        ),
        "localization.launch.py": (
            '"nav2_map_server"',
            '"map_server"',
            '"nav2_amcl"',
            '"amcl"',
            '"localization"',
            '"localization_nav2"',
            '"send_scan": True',
            '"max_scan_points": 180',
        ),
        "navigation.launch.py": (
            '"localization_nav2"',
            '"nav2_controller"',
            '"nav2_smoother"',
            '"nav2_planner"',
            '"nav2_behaviors"',
            '"nav2_bt_navigator"',
            '"nav2_waypoint_follower"',
            '"nav2_velocity_smoother"',
            '"/cmd_vel_nav_dry_run"',
            '"nav2_command_bridge"',
        ),
    }

    for filename, tokens in checks.items():
        path = package_dir / "launch" / filename
        if path.is_file():
            require_tokens(
                read_text(path),
                tokens,
                findings,
                "launch-contract",
                filename,
            )

    localization = package_dir / "launch" / "localization.launch.py"
    if localization.is_file():
        localization_source = read_text(localization)
        obsolete_direct_lidar_tokens = (
            "sllidar_ros2",
            '"start_lidar"',
            '"serial_port"',
            '"serial_baudrate"',
            '"driver_package"',
            '"driver_executable"',
            "lidar.launch.py",
        )
        for token in obsolete_direct_lidar_tokens:
            if token in localization_source:
                add(
                    findings,
                    "ERROR",
                    "localization-direct-lidar",
                    (
                        "localization.launch.py는 기존 /scan을 사용해야 하며 "
                        f"직접 LiDAR 실행 설정을 포함하면 안 됩니다: {token}"
                    ),
                )

    navigation = package_dir / "launch" / "navigation.launch.py"
    if navigation.is_file() and '"/cmd_vel"' in read_text(navigation):
        add(
            findings,
            "ERROR",
            "live-cmd-vel",
            "navigation.launch.py에서 절대 topic /cmd_vel이 감지되었습니다.",
        )


def check_nodes(package_dir: Path, findings: list[Finding]) -> None:
    map_bridge = package_dir / "patrol_navigation" / "map_bridge.py"
    if map_bridge.is_file():
        source = read_text(map_bridge)
        require_tokens(
            source,
            (
                "qos_profile_sensor_data",
                "OccupancyGrid",
                "LaserScan",
                '"/map"',
                '"/scan"',
                '"map"',
                '"base_link"',
                "/navigation/map",
                "/navigation/pose",
                "/navigation/scan",
                'env_text("ROBOT_CONTROL_TOKEN")',
                '"X-Robot-Control-Token"',
            ),
            findings,
            "map-bridge-contract",
            "map_bridge.py",
        )
        for mode in ("mapping", "localization", "localization_nav2"):
            if f'"{mode}"' not in source:
                add(findings, "ERROR", "navigation-mode", f"map_bridge mode 누락: {mode}")

    bridge = package_dir / "patrol_navigation" / "nav2_command_bridge.py"
    if bridge.is_file():
        source = read_text(bridge)
        require_tokens(
            source,
            (
                '"/cmd_vel_nav_dry_run"',
                "wheel_track_m",
                "0.201",
                "max_wheel_mps",
                "0.50",
                "twist_timeout_sec",
                "server_request_enabled = True",
                'env_bool("MOTOR_OUTPUT_ENABLED", default=False)',
                'env_text("ROBOT_CONTROL_TOKEN")',
                '"X-Robot-Control-Token"',
                '"source": "nav2_command_bridge"',
            ),
            findings,
            "nav2-command-bridge-contract",
            "nav2_command_bridge.py",
        )
        if re.search(r"(?m)^\s*self\.motor_output_enabled\s*=\s*True\b", source):
            add(findings, "ERROR", "motor-safety", "motor_output_enabled=True가 감지되었습니다.")
        if re.search(
            r"env_bool\(\s*[\"']MOTOR_OUTPUT_ENABLED[\"']\s*,\s*default\s*=\s*True",
            source,
        ):
            add(findings, "ERROR", "motor-safety", "MOTOR_OUTPUT_ENABLED의 기본값이 True입니다.")


def check_configs(package_dir: Path, findings: list[Finding]) -> None:
    checks = {
        "amcl.yaml": ("global_frame_id: map", "odom_frame_id: odom", "base_frame_id: base_link", "scan_topic: scan"),
        "slam_toolbox.yaml": ("map_frame:", "odom_frame:", "base_frame:", "scan_topic:"),
        "nav2_params.yaml": ("controller_server:", "planner_server:", "behavior_server:", "bt_navigator:", "velocity_smoother:"),
        "rplidar_a1m8.yaml": ("serial_port", "serial_baudrate", "frame_id"),
    }
    for filename, tokens in checks.items():
        path = package_dir / "config" / filename
        if path.is_file():
            require_tokens(
                read_text(path),
                tokens,
                findings,
                "config-contract",
                filename,
            )


def check_maps(root: Path, findings: list[Finding]) -> None:
    map_dir = root / "navigation" / "maps"
    if not map_dir.is_dir() or not list(map_dir.glob("*.yaml")):
        add(
            findings,
            "WARN",
            "maps",
            "저장 지도 YAML을 확인하지 못했습니다. localization runtime 검증은 제한됩니다.",
        )


def run_runtime(root: Path, requested: bool, timeout_sec: float) -> RuntimeResult:
    if not requested:
        return RuntimeResult("SKIP", "--runtime을 지정하지 않아 runtime 검증을 생략했습니다.")
    if os.name == "nt":
        return RuntimeResult(
            "SKIP",
            "현재 Python이 Windows에서 실행 중이므로 ROS 2 runtime 검증을 생략했습니다.",
        )
    ros2 = shutil.which("ros2")
    if ros2 is None:
        return RuntimeResult("SKIP", "ros2 executable을 찾지 못했습니다.")

    outputs: list[str] = []
    warning = False
    for command in (
        [ros2, "pkg", "prefix", "patrol_navigation"],
        [ros2, "topic", "list"],
        [ros2, "node", "list"],
    ):
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=max(1.0, timeout_sec),
                shell=False,
                check=False,
            )
            output = completed.stdout or ""
            if completed.returncode != 0:
                warning = True
        except (OSError, subprocess.TimeoutExpired) as exc:
            output = str(exc)
            warning = True
        outputs.append("$ " + " ".join(command) + "\n" + output.rstrip())

    return RuntimeResult(
        "WARN" if warning else "PASS",
        "ROS 2 read-only runtime inspection 완료" if not warning else "ROS 2 runtime inspection 일부 실패",
        "\n\n".join(outputs),
    )


def main() -> int:
    args = parse_args()
    try:
        root = find_repo_root(args.repo_root)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    package_dir = root / PACKAGE_REL
    findings: list[Finding] = []
    check_required_files(package_dir, findings)
    check_python_syntax(package_dir, findings)
    check_setup(package_dir, findings)
    check_package_xml(package_dir, findings)
    check_orphan_launch_arguments(package_dir, findings)
    check_launch_contracts(package_dir, findings)
    check_nodes(package_dir, findings)
    check_configs(package_dir, findings)
    check_maps(root, findings)

    runtime = run_runtime(root, args.runtime, args.runtime_timeout_sec)

    print(f"dabom navigation validation: {root}")
    print("-" * 72)
    if findings:
        for finding in findings:
            print(f"[{finding.severity:<5}] {finding.code}: {finding.message}")
    else:
        print("[PASS ] static checks: no findings")
    print("-" * 72)
    print(f"[{runtime.status:<5}] runtime: {runtime.message}")
    if args.show_runtime_output and runtime.output.strip():
        print("-" * 72)
        print(runtime.output.rstrip())

    errors = sum(1 for item in findings if item.severity == "ERROR")
    warnings = sum(1 for item in findings if item.severity == "WARN")
    if runtime.status == "ERROR":
        errors += 1
    elif runtime.status == "WARN":
        warnings += 1

    print("-" * 72)
    print(f"summary: errors={errors}, warnings={warnings}, runtime={runtime.status.lower()}")

    if errors:
        return 1
    if args.strict_warnings and warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
