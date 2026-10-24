from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ValidatorRun:
    name: str
    status: str
    returncode: int | None
    message: str
    output: str = ""


VALIDATOR_PATHS = {
    "validate-server": Path(
        ".agents/skills/validate-server/scripts/validate_server.py"
    ),
    "validate-navigation": Path(
        ".agents/skills/validate-navigation/scripts/validate_navigation.py"
    ),
    "validate-raspberry": Path(
        ".agents/skills/validate-raspberry/scripts/validate_uart_protocol.py"
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Review changed files and select dabom validators without changing Git state."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument(
        "--base",
        default=None,
        help=(
            "Optional base ref. When set, also include files changed in BASE...HEAD. "
            "Read-only Git operation only."
        ),
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Run available Python validators after selecting them.",
    )
    parser.add_argument("--show-validator-output", action="store_true")
    parser.add_argument("--strict-warnings", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--validator-timeout-sec", type=float, default=180.0)
    return parser.parse_args()


def _is_repo_root(path: Path) -> bool:
    return (
        (path / ".git").exists()
        and (path / "AGENTS.md").is_file()
        and (path / "server").is_dir()
        and (path / "frontend").is_dir()
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


def _normalize_repo_path(value: str) -> str:
    value = value.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value


def _decode_z_paths(raw: bytes) -> list[str]:
    paths: list[str] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        value = _normalize_repo_path(
            item.decode("utf-8", errors="replace")
        )
        if value:
            paths.append(value)
    return paths


def _git_z(root: Path, args: list[str]) -> tuple[int | None, list[str], str]:
    git = shutil.which("git")
    if git is None:
        return None, [], "git executable을 찾지 못했습니다."

    try:
        completed = subprocess.run(
            [git, *args],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20.0,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, [], str(exc)

    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    return completed.returncode, _decode_z_paths(completed.stdout), stderr


def collect_changed_files(root: Path, base: str | None) -> tuple[list[str], list[str]]:
    files: set[str] = set()
    warnings: list[str] = []

    commands = (
        ["diff", "--name-only", "-z", "--relative"],
        ["diff", "--cached", "--name-only", "-z", "--relative"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    )

    for command in commands:
        returncode, paths, error = _git_z(root, command)
        if returncode != 0:
            warnings.append(
                f"git {' '.join(command)} 실패: {error or f'returncode={returncode}'}"
            )
            continue
        files.update(paths)

    if base:
        command = ["diff", "--name-only", "-z", f"{base}...HEAD"]
        returncode, paths, error = _git_z(root, command)
        if returncode != 0:
            warnings.append(
                f"git {' '.join(command)} 실패: {error or f'returncode={returncode}'}"
            )
        else:
            files.update(paths)

    return sorted(files), warnings


def select_validators(changed_files: list[str]) -> tuple[list[str], list[str]]:
    validators: set[str] = set()
    reasons: list[str] = []

    for raw in changed_files:
        path = _normalize_repo_path(raw)

        if (
            path.startswith("server/")
            or path.startswith("data/database/")
            or path.startswith("tests/test_auth")
            or path.startswith("tests/test_server")
        ):
            validators.add("validate-server")

        if (
            path.startswith("frontend/")
            or path == "server/app.py"
            or path.startswith("tests/test_dashboard")
        ):
            validators.add("validate-dashboard")

        if (
            path.startswith("navigation/")
            or path.startswith("server/navigation_")
            or path in {
                "server/nav2_command_bridge.py",
                "server/lidar_ros_bridge.py",
                "server/encoder_ros_bridge.py",
                "server/wheel_odometry.py",
            }
            or path.startswith("tests/test_navigation")
        ):
            validators.add("validate-navigation")

        if (
            path.startswith("raspberry/")
            or path.startswith("tests/test_raspberry")
            or path.startswith("tests/test_uart")
        ):
            validators.add("validate-raspberry")

        if path.startswith(".agents/skills/validate-server/"):
            validators.add("validate-server")
        if path.startswith(".agents/skills/validate-dashboard/"):
            validators.add("validate-dashboard")
        if path.startswith(".agents/skills/validate-navigation/"):
            validators.add("validate-navigation")
        if path.startswith(".agents/skills/validate-raspberry/"):
            validators.add("validate-raspberry")

    if changed_files and not validators:
        reasons.append(
            "변경 파일은 있으나 server/dashboard/navigation/raspberry 검증 영역에는 해당하지 않습니다."
        )

    return sorted(validators), reasons


def run_python_validator(
    root: Path,
    name: str,
    timeout_sec: float,
) -> ValidatorRun:
    relative = VALIDATOR_PATHS.get(name)
    if relative is None:
        return ValidatorRun(
            name,
            "WARN",
            None,
            "Python validator가 없는 검증 영역입니다.",
        )

    script = root / relative
    if not script.is_file():
        return ValidatorRun(
            name,
            "ERROR",
            None,
            f"validator script가 없습니다: {relative}",
        )

    command = [sys.executable, str(script), "--repo-root", str(root)]

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
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout if isinstance(exc.stdout, str) else ""
        return ValidatorRun(
            name,
            "ERROR",
            None,
            "validator timeout",
            output,
        )
    except OSError as exc:
        return ValidatorRun(
            name,
            "WARN",
            None,
            f"validator process 시작 실패: {exc}",
        )

    return ValidatorRun(
        name,
        "PASS" if completed.returncode == 0 else "FAIL",
        completed.returncode,
        "validator 실행 완료",
        completed.stdout or "",
    )


def run_selected_validators(
    root: Path,
    validators: list[str],
    timeout_sec: float,
) -> list[ValidatorRun]:
    results: list[ValidatorRun] = []

    for name in validators:
        if name == "validate-dashboard":
            results.append(
                ValidatorRun(
                    name,
                    "MANUAL",
                    None,
                    (
                        "validate-dashboard는 Python script가 아니라 Codex + Playwright MCP로 "
                        "실행해야 합니다. dashboard_flows.md와 expected_states.md를 사용하세요."
                    ),
                )
            )
            continue

        results.append(
            run_python_validator(root, name, timeout_sec)
        )

    return results


def build_payload(
    root: Path,
    changed_files: list[str],
    validators: list[str],
    git_warnings: list[str],
    selection_notes: list[str],
    runs: list[ValidatorRun],
) -> dict:
    return {
        "repo_root": str(root),
        "changed_files": changed_files,
        "validators": validators,
        "git_warnings": git_warnings,
        "selection_notes": selection_notes,
        "runs": [
            {
                "name": run.name,
                "status": run.status,
                "returncode": run.returncode,
                "message": run.message,
            }
            for run in runs
        ],
    }


def main() -> int:
    args = parse_args()
    try:
        root = find_repo_root(args.repo_root)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    changed_files, git_warnings = collect_changed_files(root, args.base)
    validators, selection_notes = select_validators(changed_files)
    runs = (
        run_selected_validators(root, validators, args.validator_timeout_sec)
        if args.run
        else []
    )

    payload = build_payload(
        root,
        changed_files,
        validators,
        git_warnings,
        selection_notes,
        runs,
    )

    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"dabom change review: {root}")
        print("-" * 72)
        if changed_files:
            for path in changed_files:
                print(f"[CHANGED] {path}")
        else:
            print("[PASS ] changed files: none")

        print("-" * 72)
        if validators:
            for name in validators:
                print(f"[SELECT] {name}")
        else:
            print("[PASS ] validators: none required")

        for warning in git_warnings:
            print(f"[WARN ] git: {warning}")
        for note in selection_notes:
            print(f"[INFO ] selection: {note}")

        if args.run:
            print("-" * 72)
            for run in runs:
                print(f"[{run.status:<6}] {run.name}: {run.message}")
                if args.show_validator_output and run.output.strip():
                    print(run.output.rstrip())
                    print("-" * 72)

    errors = sum(
        1 for run in runs
        if run.status in {"ERROR", "FAIL"}
    )
    warnings = len(git_warnings) + sum(
        1 for run in runs
        if run.status in {"WARN", "MANUAL"}
    )

    if errors:
        return 1
    if args.strict_warnings and warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
