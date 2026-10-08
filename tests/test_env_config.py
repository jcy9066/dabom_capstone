import ast
import importlib
import re
from pathlib import Path

import pytest

from server.env_config import EnvConfigurationError, env_bool, env_float, env_int


ROOT_DIR = Path(__file__).resolve().parents[1]
SOURCE_GLOBS = (
    "server/**/*.py",
    "frontend/**/*.py",
    "perception/**/*.py",
    "raspberry/**/*.py",
    "navigation/**/*.py",
)
ENV_READERS = {"env_text", "env_bool", "env_int", "env_float"}


def runtime_env_keys() -> set[str]:
    keys: set[str] = set()
    for pattern in SOURCE_GLOBS:
        for path in ROOT_DIR.glob(pattern):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in ENV_READERS
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    keys.add(node.args[0].value)

    required_shell_env = re.compile(r"\$\{([A-Z][A-Z0-9_]*):\?")
    required_env_array = re.compile(
        r"required_env\s*=\s*\((.*?)\)",
        re.DOTALL,
    )
    shell_env_name = re.compile(r"^[A-Z][A-Z0-9_]*$")

    for path in ROOT_DIR.glob("**/*.sh"):
        source = path.read_text(encoding="utf-8-sig")
        keys.update(required_shell_env.findall(source))
        keys.update(
            set(re.findall(r"\$\{([A-Z][A-Z0-9_]*):-", source))
            - {"XDG_RUNTIME_DIR", "TMPDIR"}
        )

        # Root runtime launchers keep required .env keys in a shell array and
        # validate them indirectly via ${!key:-}. Include those keys in the
        # same exact-key contract as Python env_* readers.
        for block in required_env_array.findall(source):
            for token in block.split():
                name = token.strip("'\"")
                if shell_env_name.fullmatch(name):
                    keys.add(name)
    return keys


def example_entries() -> list[tuple[str, str]]:
    entries = []
    for line in (ROOT_DIR / ".env.example").read_text(encoding="utf-8-sig").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator == "=", f"Invalid .env.example line: {line}"
        entries.append((key, value))
    return entries


def test_env_example_matches_runtime_keys_and_marks_provisional_calibration():
    entries = example_entries()
    keys = [key for key, _ in entries]

    assert len(keys) == len(set(keys))
    assert dict(entries)["MAX_WHEEL_MPS"] == "0.212"
    defaults = {
        "MAX_WHEEL_MPS": "0.212",
        "MIN_AUTO_DRIVE_PWM": "0.30",
        "LIDAR_YAW": "3.141592653589793",
        "LIDAR_SCAN_STALE_SEC": "3.0",
    }
    assert all(value == defaults.get(key, "") for key, value in entries)
    assert set(keys) == runtime_env_keys()


def test_boolean_parser_is_strict_and_motor_default_is_safe(monkeypatch):
    monkeypatch.delenv("MOTOR_OUTPUT_ENABLED", raising=False)
    assert env_bool("MOTOR_OUTPUT_ENABLED", default=False) is False

    monkeypatch.setenv("MOTOR_OUTPUT_ENABLED", "not-a-boolean")
    with pytest.raises(EnvConfigurationError):
        env_bool("MOTOR_OUTPUT_ENABLED", default=False)


def test_numeric_parsers_reject_invalid_or_out_of_range_values(monkeypatch):
    monkeypatch.setenv("TEST_INTEGER", "0")
    with pytest.raises(EnvConfigurationError):
        env_int("TEST_INTEGER", minimum=1)

    monkeypatch.setenv("TEST_FLOAT", "invalid")
    with pytest.raises(EnvConfigurationError):
        env_float("TEST_FLOAT")


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
@pytest.mark.parametrize(
    "module_name",
    ["server.env_config", "raspberry.env_config", "perception.env_config"],
)
def test_float_parsers_reject_non_finite_values(monkeypatch, raw, module_name):
    module = importlib.import_module(module_name)
    monkeypatch.setenv("TEST_FLOAT", raw)
    with pytest.raises(module.EnvConfigurationError):
        module.env_float("TEST_FLOAT")


def test_navigation_watchdog_interval_is_capped_at_half_second(monkeypatch):
    from server.navigation_control_api import NavigationWatchdogConfig

    monkeypatch.setenv("NAV_WATCHDOG_INTERVAL_SEC", "0.75")
    with pytest.raises(EnvConfigurationError):
        NavigationWatchdogConfig.from_env()


def test_stream_fps_uses_float_contract_in_active_server_runtime():
    server_source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")
    contract = 'env_float("STREAM_FPS", minimum=0.1)'
    assert contract in server_source


def test_shared_timeout_constraints_match_server_and_frontend():
    server_source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")
    frontend_source = (ROOT_DIR / "frontend" / "config.py").read_text(
        encoding="utf-8"
    )
    for name in ("CAMERA_TIMEOUT_SEC", "ROBOT_STATUS_TIMEOUT_SEC"):
        contract = f'env_float("{name}", minimum=0.1)'
        assert contract in server_source
        assert contract in frontend_source


def test_dashboard_navigation_values_use_required_non_negative_float_contracts():
    source = (ROOT_DIR / "server" / "navigation_control_api.py").read_text(
        encoding="utf-8"
    )
    for name in (
        "DASHBOARD_ESTOP_COOLDOWN_SEC",
        "DASHBOARD_GOAL_REACHED_TOLERANCE_M",
    ):
        assert f'env_float("{name}", minimum=0.0)' in source


def test_env_validator_accepts_only_provisional_speed_default(tmp_path):
    import runpy

    validator = runpy.run_path(str(
        ROOT_DIR / ".agents/skills/validate-server/scripts/validate_server.py"
    ))
    (tmp_path / "start.sh").write_text(
        "required_env=(MAX_WHEEL_MPS ROBOT_CONTROL_TOKEN)\n"
    )
    example = tmp_path / ".env.example"
    example.write_text("MAX_WHEEL_MPS=0.212\nROBOT_CONTROL_TOKEN=\n")
    findings = []
    validator["check_env_example"](tmp_path, findings)
    assert findings == []

    example.write_text("MAX_WHEEL_MPS=0.212\nROBOT_CONTROL_TOKEN=example-secret\n")
    validator["check_env_example"](tmp_path, findings)
    assert any("ROBOT_CONTROL_TOKEN" in finding.message for finding in findings)
