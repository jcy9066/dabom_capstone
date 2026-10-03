"""Hardware-free checks of the real launch -> Twist -> UART speed contract."""
import importlib.util
import math
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from raspberry.controllers.motor_controller import MotorController, MotorControllerError

ROOT = Path(__file__).resolve().parents[1]
ROS = ROOT / "navigation/ros/patrol_navigation"


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def motor(monkeypatch):
    monkeypatch.setenv("MAX_WHEEL_MPS", "0.17")
    controller = MotorController()
    # UART is intercepted: no serial port is opened and no motor is driven.
    controller._ensure_connected_locked = Mock()
    controller._exchange_locked = Mock(return_value="OK")
    return controller


@pytest.mark.parametrize("direction", ["forward", "backward"])
def test_manual_default_reaches_full_pwm(motor, direction):
    motor.move(direction)
    motor._exchange_locked.assert_called_once_with(f"MOVE,{direction},1.000")


@pytest.mark.parametrize("left,right,expected", [
    (.17, .17, "DRIVE,1.000,1.000"),
    (-.17, -.17, "DRIVE,-1.000,-1.000"),
    (.085, .085, "DRIVE,0.500,0.500"),
    (.017, .034, "DRIVE,0.250,0.500"),
    (.34, .17, "DRIVE,1.000,0.500"),
    (-.34, .17, "DRIVE,-1.000,0.500"),
    (0., 0., "STOP,zero_drive"),
])
def test_drive_preserves_fractional_output_curvature_and_stop(motor, left, right, expected):
    motor.drive(left, right)
    motor._exchange_locked.assert_called_once_with(expected)


def test_calibration_rejects_invalid_and_conflicting_overrides(monkeypatch):
    monkeypatch.setenv("MAX_WHEEL_MPS", "0.17")
    for value in (.5, float("nan"), float("inf"), 0., -.1):
        with pytest.raises(MotorControllerError):
            MotorController(max_wheel_mps=value)
    for value in ("nan", "inf", "0", "-1", "bad", ""):
        monkeypatch.setenv("MAX_WHEEL_MPS", value)
        with pytest.raises(RuntimeError):
            MotorController()


def test_motor_timeout_still_stops_full_output(motor, monkeypatch):
    motor.move("forward")
    monkeypatch.setattr("raspberry.controllers.motor_controller.time.monotonic", lambda: motor.last_command_at + 1)
    motor.failsafe_tick()
    motor._exchange_locked.assert_called_with("STOP,command_timeout")
    assert motor.current_motion == "stop"


@pytest.fixture
def ros_stubs(monkeypatch):
    """Record launch/Node parameters, with no ROS installation or worker thread."""
    monkeypatch.syspath_prepend(str(ROS))

    class Action:
        def __init__(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs

    class LaunchConfiguration(Action):
        def perform(self, context):
            return context[self.args[0]]

    class FakeNode:
        overrides = {}

        def __init__(self, name):
            self.parameters = {}

        def declare_parameter(self, name, default):
            self.parameters[name] = self.overrides.get(name, default)

        def get_parameter(self, name):
            return SimpleNamespace(value=self.parameters[name])

        def get_logger(self):
            return Mock()

        create_subscription = Mock()
        create_timer = Mock()

    modules = {
        "launch": {"LaunchDescription": Action},
        "launch.actions": {key: Action for key in ("DeclareLaunchArgument", "IncludeLaunchDescription", "TimerAction", "OpaqueFunction")},
        "launch.launch_description_sources": {"PythonLaunchDescriptionSource": Action},
        "launch.substitutions": {"LaunchConfiguration": LaunchConfiguration, "PathJoinSubstitution": Action},
        "launch.conditions": {"IfCondition": Action},
        "launch_ros": {},
        "launch_ros.actions": {"Node": Action},
        "launch_ros.substitutions": {"FindPackageShare": Action},
        "rclpy": {},
        "rclpy.node": {"Node": FakeNode},
        "rclpy.time": {"Time": Mock()},
        "nav_msgs": {},
        "nav_msgs.msg": {"Odometry": object},
        "tf2_ros": {"Buffer": Mock(), "TransformListener": Mock(), "TransformException": RuntimeError},
        "geometry_msgs": {},
        "geometry_msgs.msg": {"Twist": object},
    }
    for name, attrs in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    return FakeNode


@pytest.mark.parametrize("calibration", [.17, .23])
def test_launch_overrides_stale_limits_and_preserves_custom_dynamics(ros_stubs, monkeypatch, tmp_path, calibration):
    monkeypatch.setenv("MAX_WHEEL_MPS", str(calibration))
    module = load_module(ROS / "launch/navigation.launch.py", "speed_launch")
    config = yaml.safe_load((ROS / "config/nav2_params.yaml").read_text())
    smoother = config["velocity_smoother"]["ros__parameters"]
    smoother["max_velocity"] = [.5, 0., .42]
    smoother["min_velocity"] = [-.5, 0., -.37]
    custom = tmp_path / "custom.yaml"
    custom.write_text(yaml.safe_dump(config))
    actions = module.generate_launch_description().args[0]
    controller = next(a for a in actions if a.kwargs.get("name") == "controller_server")
    assert controller.kwargs["parameters"][-1] == {
        "FollowPath.max_vel_x": calibration, "FollowPath.max_speed_xy": calibration,
        "FollowPath.min_vel_x": calibration * .5, "FollowPath.min_speed_xy": calibration * .5,
    }
    bridge = next(a for a in actions if a.kwargs.get("name") == "nav2_command_bridge")
    assert bridge.kwargs["parameters"][0]["max_wheel_mps"] == calibration
    opaque = next(a for a in actions if "function" in a.kwargs)
    node = opaque.kwargs["function"]({"params_file": str(custom)})[0]
    assert node.kwargs["parameters"][-1] == {
        "max_velocity": [calibration, 0., .42], "min_velocity": [-calibration, 0., -.37],
    }
    # Original file remains first: accel/decel/feedback and all other config survive.
    assert node.kwargs["parameters"][0].perform({"params_file": str(custom)}) == str(custom)
    assert yaml.safe_load(custom.read_text()) == config


@pytest.mark.parametrize("path", [
    "server/nav2_command_bridge.py",
    "navigation/ros/patrol_navigation/patrol_navigation/nav2_command_bridge.py",
])
def test_bridge_to_uart_max_partial_turn_stop_and_timeout(ros_stubs, monkeypatch, motor, path):
    module = load_module(ROOT / path, "speed_bridge")
    monkeypatch.setattr(module.threading, "Thread", Mock())
    bridge = module.Nav2CommandBridge()
    assert bridge.max_wheel_mps == .17
    if hasattr(bridge, "_log_drive_diagnostics"):
        bridge._log_drive_diagnostics = Mock()
        assert bridge.min_auto_drive_pwm == .5
    bridge._queue_command = Mock(return_value=True)
    bridge._queue_stop = Mock(return_value=True)
    for linear, angular in ((.17, 0.), (.085, 0.), (-.17, 0.), (.17, .6)):
        bridge.on_twist(SimpleNamespace(linear=SimpleNamespace(x=linear), angular=SimpleNamespace(z=angular)))
        payload = bridge._queue_command.call_args.args[0]
        motor.drive(payload["left_mps"], payload["right_mps"])
        fields = motor._exchange_locked.call_args.args[0].split(",")
        left, right = map(float, fields[1:])
        assert max(abs(left), abs(right)) <= 1.
        if not angular:
            assert left == right == pytest.approx(linear / .17)
        else:
            half = bridge.wheel_track_m / 2
            assert right == 1.
            assert left / right == pytest.approx((linear-angular*half)/(linear+angular*half), abs=.001)
    bridge.on_twist(SimpleNamespace(linear=SimpleNamespace(x=0.), angular=SimpleNamespace(z=0.)))
    bridge._queue_stop.assert_called_with("nav2_zero_twist")
    bridge.last_twist_at -= 1.
    bridge.check_timeout()
    bridge._queue_stop.assert_called_with("nav2_twist_timeout")
    bridge.on_twist(SimpleNamespace(linear=SimpleNamespace(x=math.nan), angular=SimpleNamespace(z=0.)))
    bridge._queue_stop.assert_called_with("nav2_invalid_twist")
    ros_stubs.overrides = {"max_wheel_mps": .5}
    with pytest.raises(ValueError, match="MAX_WHEEL_MPS"):
        module.Nav2CommandBridge()


def test_pi_cli_cannot_override_shared_calibration(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "raspberry"))
    module = load_module(ROOT / "raspberry/robot_command_client.py", "speed_client")
    monkeypatch.setenv("MAX_WHEEL_MPS", "0.17")
    monkeypatch.setattr(sys, "argv", ["robot_command_client.py"])
    assert module.parse_args().max_wheel_mps == .17
    monkeypatch.setattr(sys, "argv", ["robot_command_client.py", "--max-wheel-mps", "0.5"])
    with pytest.raises(SystemExit):
        module.parse_args()


def test_server_requires_matching_pi_calibration_before_auto_mode(monkeypatch):
    # Pi must advertise the same full-PWM calibration before auto mode starts.
    pi_source = (ROOT / "raspberry/robot_command_client.py").read_text(
        encoding="utf-8"
    )
    assert '"max_wheel_mps": self.motor.max_wheel_mps' in pi_source

    monkeypatch.setenv("DASHBOARD_ESTOP_COOLDOWN_SEC", "1")
    monkeypatch.setenv("DASHBOARD_GOAL_REACHED_TOLERANCE_M", "0.15")
    from fastapi.testclient import TestClient
    import server.app as server

    monkeypatch.setattr(server, "MAX_WHEEL_MPS", .17)
    monkeypatch.setitem(server.robot_status, "robot_id", "pi-01")
    monkeypatch.setitem(server.robot_status, "max_wheel_mps", None)
    monkeypatch.setitem(server.robot_status, "updated_at", None)
    send_command = AsyncMock(return_value=True)
    monkeypatch.setattr(server.connections, "send_command", send_command)

    client = TestClient(server.app)
    headers = {"X-Robot-Control-Token": "test-robot-token"}
    mode_payload = {"type": "mode", "mode": "auto"}

    response = client.post(
        "/api/robots/pi-01/command",
        headers=headers,
        json=mode_payload,
    )
    assert response.status_code == 409
    assert "unavailable" in response.json()["error"]
    send_command.assert_not_awaited()

    response = client.post(
        "/status",
        headers=headers,
        json={"robot_id": "pi-01", "max_wheel_mps": .23},
    )
    assert response.status_code == 200
    assert server.robot_status["max_wheel_mps"] == .23

    response = client.post(
        "/api/robots/pi-01/command",
        headers=headers,
        json=mode_payload,
    )
    assert response.status_code == 409
    assert "MAX_WHEEL_MPS mismatch" in response.json()["error"]
    send_command.assert_not_awaited()

    monkeypatch.setitem(
        server.robot_status,
        "updated_at",
        server.time.time() - server.ROBOT_STATUS_TIMEOUT_SEC - 1.0,
    )
    response = client.post(
        "/api/robots/pi-01/command",
        headers=headers,
        json=mode_payload,
    )
    assert response.status_code == 409
    assert "status is stale" in response.json()["error"]
    send_command.assert_not_awaited()

    response = client.post(
        "/status",
        headers=headers,
        json={"robot_id": "pi-01", "max_wheel_mps": .17},
    )
    assert response.status_code == 200

    response = client.post(
        "/api/robots/pi-01/command",
        headers=headers,
        json=mode_payload,
    )
    assert response.status_code == 200
    assert response.json()["delivered"] is True
    send_command.assert_awaited_once()


@pytest.mark.parametrize("limit", [.17, .23])
def test_server_accepts_calibrated_limit_and_rejects_excess(monkeypatch, limit):
    # Existing global fixtures predate these required dashboard settings.
    monkeypatch.setenv("DASHBOARD_ESTOP_COOLDOWN_SEC", "1")
    monkeypatch.setenv("DASHBOARD_GOAL_REACHED_TOLERANCE_M", "0.15")
    from fastapi.testclient import TestClient
    import server.app as server

    monkeypatch.setattr(server, "MAX_WHEEL_MPS", limit)
    monkeypatch.setattr(server, "MOTOR_OUTPUT_ENABLED", False)
    client = TestClient(server.app)
    headers = {"X-Robot-Control-Token": "test-robot-token"}
    payload = {"type": "auto_drive", "left_mps": limit, "right_mps": limit}
    response = client.post("/api/robots/pi-01/command", headers=headers, json=payload)
    assert response.status_code == 200
    assert response.json()["blocked"] is True
    assert response.json()["delivered"] is False
    for left, right in ((limit + .001, limit), (limit, -limit - .001)):
        response = client.post("/api/robots/pi-01/command", headers=headers,
                               json={**payload, "left_mps": left, "right_mps": right})
        assert response.status_code == 400
        assert "wheel speed exceeds" in response.json()["error"]


def test_pico_full_pwm_and_direction_ratios_without_hardware(tmp_path):
    import shutil
    import subprocess

    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("Host C compiler unavailable")
    source = (ROOT / "raspberry/pico_w_sdk/main.c").read_text()

    def function(name):
        start = source.index("static ", source.rfind("\nstatic ", 0, source.index(name)) + 1)
        brace = source.index("{", start)
        depth = 1
        end = brace + 1
        while depth:
            depth += (source[end] == "{") - (source[end] == "}")
            end += 1
        return source[start:end]

    harness = '''
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <assert.h>
#include <math.h>
typedef unsigned int uint;
#define PWM_WRAP 999U
#define CURVE_INNER_RATIO 0.35f
typedef struct { uint pwm_pin, dir_pin, slice, channel; bool forward_dir_level; } motor_channel_t;
static uint16_t last_level;
static bool last_direction;
static void gpio_put(uint pin, bool value) { last_direction = value; }
static void pwm_set_chan_level(uint slice, uint channel, uint16_t value) { last_level = value; }
static float clamp_float(float value, float lo, float hi) { return fminf(hi, fmaxf(lo, value)); }
'''
    harness += function("motor_set_signed_speed(") + "\n" + function("direction_to_wheel_speeds(")
    harness += '''
int main(void) {
    motor_channel_t motor = {.forward_dir_level = true};
    float left, right;
    assert(direction_to_wheel_speeds("forward", 1.f, &left, &right));
    assert(left == 1.f && right == 1.f);
    motor_set_signed_speed(&motor, left);
    assert(last_level == PWM_WRAP && last_direction);
    assert(direction_to_wheel_speeds("backward", 1.f, &left, &right));
    assert(left == -1.f && right == -1.f);
    motor_set_signed_speed(&motor, left);
    assert(last_level == PWM_WRAP && !last_direction);
    assert(direction_to_wheel_speeds("forward_left", 1.f, &left, &right));
    assert(left == .35f && right == 1.f);
    assert(direction_to_wheel_speeds("rotate_left", 1.f, &left, &right));
    assert(left == -1.f && right == 1.f);
    motor_set_signed_speed(&motor, 0.f);
    assert(last_level == 0);
    return 0;
}
'''
    file = tmp_path / "pico_speed.c"
    file.write_text(harness)
    binary = tmp_path / "pico_speed"
    subprocess.run([compiler, str(file), "-lm", "-o", str(binary)], check=True, capture_output=True)
    subprocess.run([str(binary)], check=True, capture_output=True)
