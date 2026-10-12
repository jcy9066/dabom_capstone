"""Hardware-free tests for strict Pico STOP acknowledgements."""

import pytest
from unittest.mock import Mock

from raspberry.controllers.motor_controller import (
    MotorController,
    MotorControllerError,
)


@pytest.fixture
def motor(monkeypatch):
    monkeypatch.setenv("MAX_WHEEL_MPS", "0.17")
    controller = MotorController()
    controller._ensure_connected_locked = Mock()
    controller._close_serial_locked = Mock()
    return controller


def test_stop_accepts_expected_ack(motor):
    motor._exchange_locked = Mock(return_value="OK,STOP")
    motor.stop("test")
    motor._exchange_locked.assert_called_once_with("STOP,test")


@pytest.mark.parametrize(
    "unexpected_ack",
    [
        "OK,PONG",
        "OK,DRIVE",
        "OK,MOVE,forward",
        "OK,ENC",
    ],
)
def test_stop_rejects_unexpected_ack(motor, unexpected_ack):
    motor._exchange_locked = Mock(return_value=unexpected_ack)

    with pytest.raises(MotorControllerError):
        motor.stop("test")


def test_stop_rejects_uart_timeout(motor):
    motor._exchange_locked = Mock(
        side_effect=MotorControllerError("UART timeout")
    )

    with pytest.raises(MotorControllerError):
        motor.stop("test")


@pytest.mark.parametrize(
    "reason",
    ["pi_connected", "pico_reboot_recovery", "controller_close"],
)
@pytest.mark.parametrize("ack", ["OK,STOP", "OK,PONG", "OK,DRIVE"])
def test_internal_stop_ack_validation(motor, reason, ack):
    motor._exchange_locked = Mock(return_value=ack)

    if ack == "OK,STOP":
        motor._send_stop_locked(reason)
    else:
        with pytest.raises(MotorControllerError):
            motor._send_stop_locked(reason)

    motor._exchange_locked.assert_called_once_with(f"STOP,{reason}")



@pytest.mark.parametrize(
    "method_name,reason",
    [
        ("_ensure_connected_locked", "pi_connected"),
        ("recover_after_pico_reboot", "pico_reboot_recovery"),
        ("close", "controller_close"),
    ],
)
def test_stop_paths_use_common_ack_validation(method_name, reason):
    """Ensure every STOP path calls the shared strict ACK helper."""
    import ast
    import inspect
    import textwrap

    method = getattr(MotorController, method_name)
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))

    matching_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_send_stop_locked"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == reason
    ]

    assert len(matching_calls) == 1
