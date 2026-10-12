import pytest
from raspberry.controllers.pico_uart_v2_ack_bridge import StopOnlyV2Link, V2LinkError


class FakeSerial:
    def __init__(self, responses):
        self.responses = list(responses)
        self.written = []
        self.closed = False

    def write(self, data):
        self.written.append(data)

    def readline(self):
        return self.responses.pop(0) if self.responses else b""

    def close(self):
        self.closed = True


def link(responses):
    serial = FakeSerial([x.encode() for x in responses])
    return StopOnlyV2Link(serial, session="SESSION_A"), serial


def test_correlated_stop_and_ping():
    transport, serial = link(["ACK,SESSION_A,1,OK,STOP", "ACK,SESSION_A,2,OK,PONG"])
    assert transport.exchange("STOP") == "STOP"
    assert transport.exchange("PING") == "PONG"
    assert serial.written == [b"REQ,SESSION_A,1,STOP\n", b"REQ,SESSION_A,2,PING\n"]


@pytest.mark.parametrize("response", [
    "OK,STOP", "ACK,SESSION_A,2,OK,STOP", "ACK,OTHER,1,OK,STOP",
    "ACK,SESSION_A,1,OK,PONG", "ACK,SESSION_A,1,ERR,STOP",
    "ACK,SESSION_A,1,OK,STOP,EXTRA", "READY,PICO_W_MOTOR_ENCODER", "",
])
def test_rejects_invalid_ack_and_closes(response):
    transport, serial = link([response] if response else [])
    with pytest.raises(V2LinkError):
        transport.exchange("STOP")
    assert transport.failed and serial.closed
    with pytest.raises(V2LinkError):
        transport.exchange("PING")
    assert len(serial.written) == 1


def test_event_then_ack():
    transport, _ = link(["EVENT,ENC,1,2", "ACK,SESSION_A,1,OK,STOP"])
    assert transport.exchange("STOP") == "STOP"


def test_motion_never_transmitted():
    transport, serial = link([])
    with pytest.raises(V2LinkError):
        transport.exchange("DRIVE")
    assert not serial.written


def test_stale_ack_before_matching_ack_fails_closed():
    transport, serial = link(["ACK,SESSION_A,999,OK,STOP", "ACK,SESSION_A,1,OK,STOP"])
    with pytest.raises(V2LinkError):
        transport.exchange("STOP")
    assert serial.closed
