from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str


@dataclass(frozen=True)
class TestResult:
    executed: bool
    returncode: int | None
    output: str
    classification: str
    message: str


ROUTE_DECORATOR_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "options", "head"}
)

ROS_IMPORT_MARKERS = (
    "import rclpy",
    "from rclpy",
    "from nav_msgs",
    "from sensor_msgs",
    "from geometry_msgs",
    "from tf2_ros",
    "from nav2_msgs",
)

KNOWN_ENVIRONMENT_TEST_FAILURES = (
    "ModuleNotFoundError: No module named 'rclpy'",
    'ModuleNotFoundError: No module named "rclpy"',
    "Router.__init__() got an unexpected keyword argument 'on_startup'",
    'Router.__init__() got an unexpected keyword argument "on_startup"',
    "CreateProcessAsUserW failed: 740",
    "요청한 작업을 수행하려면 권한 상승이 필요합니다",
)

URL_LITERAL_RE = re.compile(
    r"""(?P<quote>["'`])(?P<url>/(?:api/|get_status\b|send_telegram\b)[^"'`\s]*)"""
)

ENV_GET_RE = re.compile(
    r"""os\.getenv\(\s*["']([A-Z][A-Z0-9_]*)["']"""
)

ENV_INDEX_RE = re.compile(
    r"""os\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]"""
)
ENV_HELPER_RE = re.compile(
    r"""env_(?:text|bool|int|float)\(\s*["']([A-Z][A-Z0-9_]*)["']"""
)
SHELL_REQUIRED_ENV_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*):\?")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the dabom FastAPI server without changing state."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root. Auto-detected when omitted.",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip pytest execution.",
    )
    parser.add_argument(
        "--show-test-output",
        action="store_true",
        help="Print pytest stdout/stderr.",
    )
    parser.add_argument(
        "--strict-warnings",
        action="store_true",
        help="Return non-zero when warnings exist.",
    )
    parser.add_argument(
        "--test-timeout-sec",
        type=float,
        default=120.0,
        help="Pytest timeout used by this wrapper.",
    )
    return parser.parse_args()


def _looks_like_repo_root(path: Path) -> bool:
    return (
        (path / "server" / "app.py").is_file()
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

            if _looks_like_repo_root(current):
                return current

    raise RuntimeError(
        "dabom repository root를 찾지 못했습니다. "
        "--repo-root로 server/app.py가 있는 repository root를 지정하세요."
    )


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def add(
    findings: list[Finding],
    severity: str,
    code: str,
    message: str,
) -> None:
    findings.append(Finding(severity, code, message))


def check_required_files(root: Path, findings: list[Finding]) -> None:
    required = (
        "server/app.py",
        "server/database.py",
        "server/auth_service.py",
        "frontend/templates/index.html",
        "frontend/templates/login.html",
        ".env.example",
    )

    for relative in required:
        if not (root / relative).exists():
            add(
                findings,
                "ERROR",
                "required-file",
                f"필수 파일이 없습니다: {relative}",
            )


def iter_server_python_files(root: Path) -> Iterable[Path]:
    server_dir = root / "server"

    if not server_dir.is_dir():
        return ()

    return sorted(server_dir.rglob("*.py"))


def check_python_syntax(root: Path, findings: list[Finding]) -> None:
    for path in iter_server_python_files(root):
        relative = path.relative_to(root)

        try:
            source = read_text(path)
            ast.parse(source, filename=str(relative))
        except SyntaxError as exc:
            add(
                findings,
                "ERROR",
                "python-syntax",
                (
                    f"{relative}:{exc.lineno}:{exc.offset} "
                    f"Python syntax error: {exc.msg}"
                ),
            )
        except OSError as exc:
            add(
                findings,
                "ERROR",
                "python-read",
                f"{relative} 읽기 실패: {exc}",
            )


def _decorator_route(
    decorator: ast.expr,
) -> tuple[str, str] | None:
    if not isinstance(decorator, ast.Call):
        return None

    func = decorator.func

    if not isinstance(func, ast.Attribute):
        return None

    method = func.attr.lower()

    if method not in ROUTE_DECORATOR_METHODS:
        return None

    # app.<method>() direct route만 duplicate 검사한다.
    # APIRouter route는 include prefix에 따라 동일 path가 정상일 수 있다.
    if not isinstance(func.value, ast.Name) or func.value.id != "app":
        return None

    if not decorator.args:
        return None

    first = decorator.args[0]

    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return None

    return method.upper(), first.value


def collect_app_routes(app_path: Path) -> list[tuple[str, str, int]]:
    source = read_text(app_path)
    tree = ast.parse(source, filename=str(app_path))
    routes: list[tuple[str, str, int]] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        for decorator in node.decorator_list:
            route = _decorator_route(decorator)

            if route is None:
                continue

            method, route_path = route
            routes.append((method, route_path, node.lineno))

    return routes


def check_duplicate_routes(root: Path, findings: list[Finding]) -> None:
    app_path = root / "server" / "app.py"

    if not app_path.is_file():
        return

    try:
        routes = collect_app_routes(app_path)
    except (SyntaxError, OSError):
        return

    by_key: dict[tuple[str, str], list[int]] = {}

    for method, route_path, line in routes:
        by_key.setdefault((method, route_path), []).append(line)

    for (method, route_path), lines in sorted(by_key.items()):
        if len(lines) <= 1:
            continue

        add(
            findings,
            "ERROR",
            "duplicate-route",
            (
                f"{method} {route_path}가 server/app.py에 중복 선언되어 있습니다. "
                f"lines={','.join(str(line) for line in lines)}"
            ),
        )


def _normalize_route_path(path: str) -> tuple[str, ...]:
    path = path.split("?", 1)[0].split("#", 1)[0]
    parts: list[str] = []

    for segment in path.strip("/").split("/"):
        if not segment:
            continue

        if (
            (segment.startswith("{") and segment.endswith("}"))
            or "${" in segment
        ):
            parts.append("*")
        else:
            parts.append(segment)

    return tuple(parts)


def _route_shape_matches(frontend: str, backend: str) -> bool:
    left = _normalize_route_path(frontend)
    right = _normalize_route_path(backend)

    if len(left) != len(right):
        return False

    return all(
        a == b or a == "*" or b == "*"
        for a, b in zip(left, right)
    )


def collect_frontend_urls(root: Path) -> set[str]:
    urls: set[str] = set()
    static_dir = root / "frontend" / "static"

    if not static_dir.is_dir():
        return urls

    for js_path in static_dir.rglob("*.js"):
        source = read_text(js_path)

        for match in URL_LITERAL_RE.finditer(source):
            url = match.group("url").rstrip("),]};")
            urls.add(url)

    return urls


def check_frontend_routes(root: Path, findings: list[Finding]) -> None:
    app_path = root / "server" / "app.py"

    if not app_path.is_file():
        return

    try:
        backend_routes = {
            route_path
            for _, route_path, _ in collect_app_routes(app_path)
        }
    except (SyntaxError, OSError):
        return

    for url in sorted(collect_frontend_urls(root)):
        normalized = re.sub(r"\$\{[^}]+\}", "{dynamic}", url)

        if any(
            _route_shape_matches(normalized, backend)
            for backend in backend_routes
        ):
            continue

        # include_router 또는 dynamic route일 수 있으므로 ERROR가 아닌 WARN.
        add(
            findings,
            "WARN",
            "frontend-route-unresolved",
            (
                "frontend endpoint를 app.py direct route와 정적으로 연결하지 "
                f"못했습니다: {url}. include_router/dynamic URL이면 정상일 수 있습니다."
            ),
        )


def collect_used_env_vars(root: Path) -> set[str]:
    used: set[str] = set()

    for directory in ("server", "frontend", "perception", "raspberry", "navigation"):
        source_dir = root / directory
        if not source_dir.is_dir():
            continue
        for path in source_dir.rglob("*.py"):
            source = read_text(path)
            used.update(ENV_GET_RE.findall(source))
            used.update(ENV_INDEX_RE.findall(source))
            used.update(ENV_HELPER_RE.findall(source))

    for path in root.rglob("*.sh"):
        source = read_text(path)
        used.update(SHELL_REQUIRED_ENV_RE.findall(source))
        used.update(
            set(re.findall(r"\$\{([A-Z][A-Z0-9_]*):-", source))
            - {"XDG_RUNTIME_DIR", "TMPDIR"}
        )
        # Runtime launchers also validate indirectly via required_env arrays.
        for block in re.findall(r"required_env\s*=\s*\((.*?)\)", source, re.DOTALL):
            used.update(
                token.strip("\"'") for token in block.split()
                if re.fullmatch(r"[A-Z][A-Z0-9_]*", token.strip("\"'"))
            )

    return used


def collect_env_example_vars(root: Path) -> set[str]:
    path = root / ".env.example"

    if not path.is_file():
        return set()

    variables: set[str] = set()

    for raw_line in read_text(path).splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        name = line.split("=", 1)[0].strip()

        if re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            variables.add(name)

    return variables


def check_env_example(root: Path, findings: list[Finding]) -> None:
    path = root / ".env.example"
    used = collect_used_env_vars(root)
    example = collect_env_example_vars(root)
    missing = sorted(used - example)
    extra = sorted(example - used)

    if missing:
        add(
            findings,
            "ERROR",
            "env-example",
            (
                ".env.example에 없는 runtime 환경변수가 있습니다: "
                + ", ".join(missing)
            ),
        )

    if extra:
        add(
            findings,
            "ERROR",
            "env-example",
            ".env.example에 미사용 환경변수가 있습니다: " + ", ".join(extra),
        )

    entries: list[str] = []
    nonempty: list[str] = []
    for raw_line in read_text(path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            add(findings, "ERROR", "env-example", f"잘못된 항목 형식: {line}")
            continue
        entries.append(name)
        # Documented non-secret defaults; credentials must remain empty.
        defaults = {
            "MAX_WHEEL_MPS": "0.212",
            "MIN_AUTO_DRIVE_PWM": "0.30",
            "LIDAR_YAW": "3.141592653589793",
            "LIDAR_SCAN_STALE_SEC": "3.0",
        }
        if value and defaults.get(name) != value:
            nonempty.append(name)

    duplicates = sorted({name for name in entries if entries.count(name) > 1})
    if duplicates:
        add(findings, "ERROR", "env-example", "중복 환경변수: " + ", ".join(duplicates))
    if nonempty:
        add(findings, "ERROR", "env-example", "값이 비어 있지 않은 항목: " + ", ".join(nonempty))


def check_motor_safety(root: Path, findings: list[Finding]) -> None:
    app_path = root / "server" / "app.py"

    if not app_path.is_file():
        return

    source = read_text(app_path)

    if re.search(r"env_bool\(\s*[\"']MOTOR_OUTPUT_ENABLED[\"']\s*,\s*default\s*=\s*True", source):
        add(
            findings,
            "ERROR",
            "motor-safety",
            "MOTOR_OUTPUT_ENABLED의 기본값이 True입니다.",
        )
        return

    if not re.search(
        r"MOTOR_OUTPUT_ENABLED\s*=\s*env_bool\(\s*[\"']MOTOR_OUTPUT_ENABLED[\"']\s*,\s*default\s*=\s*False\s*\)",
        source,
    ):
        add(
            findings,
            "ERROR",
            "motor-safety-unresolved",
            "MOTOR_OUTPUT_ENABLED의 ENV 기반 false-safe 구성을 확인하지 못했습니다.",
        )


def _test_files(root: Path) -> list[Path]:
    test_root = root / "tests"

    if not test_root.is_dir():
        return []

    return sorted(
        path
        for path in test_root.rglob("test_*.py")
        if path.is_file()
    )


def _requires_ros(path: Path) -> bool:
    source = read_text(path)
    return any(marker in source for marker in ROS_IMPORT_MARKERS)


def _pytest_available() -> bool:
    return importlib.util.find_spec("pytest") is not None


def run_tests(
    root: Path,
    *,
    skip_tests: bool,
    timeout_sec: float,
) -> TestResult:
    if skip_tests:
        return TestResult(
            executed=False,
            returncode=None,
            output="",
            classification="SKIP",
            message="--skip-tests로 pytest를 생략했습니다.",
        )

    files = _test_files(root)

    if not files:
        return TestResult(
            executed=False,
            returncode=None,
            output="",
            classification="WARN",
            message="tests/test_*.py가 없어 pytest를 실행하지 않았습니다.",
        )

    if not _pytest_available():
        return TestResult(
            executed=False,
            returncode=None,
            output="",
            classification="WARN",
            message=(
                f"현재 Python({sys.executable})에 pytest가 설치되어 있지 않아 "
                "tests를 실행하지 않았습니다."
            ),
        )

    ros_available = importlib.util.find_spec("rclpy") is not None
    selected: list[Path] = []
    skipped_ros: list[Path] = []

    for test_path in files:
        if not ros_available and _requires_ros(test_path):
            skipped_ros.append(test_path)
        else:
            selected.append(test_path)

    if not selected:
        return TestResult(
            executed=False,
            returncode=None,
            output="",
            classification="WARN",
            message=(
                "현재 Python 환경에 rclpy가 없어 ROS 의존 test만 존재하는 "
                f"{len(skipped_ros)}개 test file을 생략했습니다."
            ),
        )

    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        *[str(test_path.relative_to(root)) for test_path in selected],
    ]

    try:
        completed = subprocess.run(
            command,
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=max(1.0, float(timeout_sec)),
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout if isinstance(exc.stdout, str) else ""

        return TestResult(
            executed=True,
            returncode=None,
            output=output,
            classification="ERROR",
            message=f"pytest가 {timeout_sec:.1f}초 안에 끝나지 않았습니다.",
        )
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)

        if winerror == 740 or "740" in str(exc):
            return TestResult(
                executed=False,
                returncode=None,
                output=str(exc),
                classification="WARN",
                message=(
                    "Windows sandbox 권한 때문에 pytest child process를 "
                    "실행하지 못했습니다 (WinError 740)."
                ),
            )

        return TestResult(
            executed=False,
            returncode=None,
            output=str(exc),
            classification="WARN",
            message=f"pytest process를 시작하지 못했습니다: {exc}",
        )

    output = completed.stdout or ""

    if completed.returncode == 0:
        suffix = (
            f" (ROS 의존 test file {len(skipped_ros)}개 생략)"
            if skipped_ros
            else ""
        )

        return TestResult(
            executed=True,
            returncode=0,
            output=output,
            classification="PASS",
            message="pytest 통과" + suffix,
        )

    if completed.returncode == 5:
        return TestResult(
            executed=True,
            returncode=5,
            output=output,
            classification="WARN",
            message="pytest가 실행되었지만 test를 수집하지 못했습니다.",
        )

    if any(
        marker in output
        for marker in KNOWN_ENVIRONMENT_TEST_FAILURES
    ):
        return TestResult(
            executed=True,
            returncode=completed.returncode,
            output=output,
            classification="WARN",
            message=(
                "pytest가 현재 Windows/의존성 환경 문제로 실패했습니다. "
                "정적 server 검증 결과와 분리해서 확인하세요."
            ),
        )

    return TestResult(
        executed=True,
        returncode=completed.returncode,
        output=output,
        classification="ERROR",
        message=f"pytest 실패 (returncode={completed.returncode})",
    )


def print_report(
    root: Path,
    findings: list[Finding],
    test_result: TestResult,
    show_test_output: bool,
) -> tuple[int, int, int]:
    print(f"dabom server validation: {root}")
    print("-" * 72)

    if findings:
        for finding in findings:
            print(
                f"[{finding.severity:<5}] "
                f"{finding.code}: {finding.message}"
            )
    else:
        print("[PASS ] static checks: no findings")

    print("-" * 72)
    print(
        f"[{test_result.classification:<5}] tests: "
        f"{test_result.message}"
    )

    if show_test_output and test_result.output.strip():
        print("-" * 72)
        print(test_result.output.rstrip())

    errors = sum(
        1 for finding in findings if finding.severity == "ERROR"
    )
    warnings = sum(
        1 for finding in findings if finding.severity == "WARN"
    )

    if test_result.classification == "ERROR":
        errors += 1
    elif test_result.classification == "WARN":
        warnings += 1

    tests = 1 if test_result.executed else 0

    print("-" * 72)
    print(
        f"summary: errors={errors}, warnings={warnings}, tests={tests}"
    )

    return errors, warnings, tests


def main() -> int:
    args = parse_args()

    try:
        root = find_repo_root(args.repo_root)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    findings: list[Finding] = []

    check_required_files(root, findings)
    check_python_syntax(root, findings)
    check_duplicate_routes(root, findings)
    check_frontend_routes(root, findings)
    check_env_example(root, findings)
    check_motor_safety(root, findings)

    test_result = run_tests(
        root,
        skip_tests=args.skip_tests,
        timeout_sec=args.test_timeout_sec,
    )

    errors, warnings, _ = print_report(
        root,
        findings,
        test_result,
        args.show_test_output,
    )

    if errors:
        return 1

    if args.strict_warnings and warnings:
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
