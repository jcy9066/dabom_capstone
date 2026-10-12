import pytest

from raspberry.controllers.motor_controller import (
    MotorController,
    MotorControllerError,
)
from raspberry.controllers.pico_uart_v2_router import (
    V2AckRouter,
    V2RouterError,
)


class FakeSerial:
    def __init__(self):
        self.is_open = True
        self.writes = []

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.is_open = False


def make_controller():
    controller = MotorController.__new__(MotorController)

    import threading
    from collections import deque
    from raspberry.controllers.pico_uart_protocol_v2 import RequestSequencer

    controller._command_lock = threading.RLock()
    controller._write_lock = threading.Lock()
    controller._state_lock = threading.RLock()
    controller._response_condition = threading.Condition()
    controller._response_queue = deque()
    controller._pico_reboot_event = threading.Event()
    controller._reader_running = False
    controller._reader_thread = None
    controller._serial = FakeSerial()
    controller.serial_timeout_sec = 0.01
    controller._v2_router = V2AckRouter()
    controller._v2_sequence = RequestSequencer()
    # Low-level timeout test exercises an already negotiated link.
    controller._v2_negotiated = True
    controller._v2_host_test_enabled = True
    controller.current_motion = "stop"
    controller.led_enabled = False

    return controller


def test_v2_motion_rejected_without_uart_write():
    controller = make_controller()

    with pytest.raises(MotorControllerError):
        controller.experimental_v2_exchange("DRIVE,1,1")

    assert controller._serial.writes == []


def test_v2_timeout_closes_uart():
    controller = make_controller()
    serial_device = controller._serial

    with pytest.raises(MotorControllerError):
        controller.experimental_v2_exchange("PING")

    assert serial_device.is_open is False


def test_v2_ack_not_forwarded_to_v1_queue():
    controller = make_controller()

    controller._handle_uart_line(
        "ACK,TESTSESSION,1,OK,PONG"
    )

    assert list(controller._response_queue) == []


def test_v2_ready_invalidates_pending_request():
    router = V2AckRouter()
    router.begin("TESTSESSION", 1)
    router.feed("READY,PICO")

    with pytest.raises(V2RouterError):
        router.wait(0.01)


def test_v2_wrong_ack_invalidates_router():
    router = V2AckRouter()
    router.begin("TESTSESSION", 1)
    router.feed("ACK,WRONGSESSION,1,OK,PONG")

    with pytest.raises(V2RouterError):
        router.wait(0.01)


def test_v2_router_rejects_parallel_requests():
    router = V2AckRouter()
    router.begin("TESTSESSION", 1)

    with pytest.raises(V2RouterError):
        router.begin("TESTSESSION", 2)
