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
