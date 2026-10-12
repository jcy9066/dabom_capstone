"""Host-only tests for UART V2 request construction."""

import pytest

from raspberry.controllers.pico_uart_protocol_v2 import (
    ProtocolError,
    build_request,
)


def test_build_stop_request():
    assert build_request(
        "STOP",
        "emergency_stop",
        session="A73F91C2",
        request_id=1042,
    ) == "REQ,A73F91C2,1042,STOP,emergency_stop"


def test_build_drive_request():
    assert build_request(
        "DRIVE",
        "0.100",
        "-0.100",
        session="A73F91C2",
        request_id=1043,
    ) == "REQ,A73F91C2,1043,DRIVE,0.100,-0.100"


@pytest.mark.parametrize(
    "session,request_id",
    [
        ("", 1),
        ("invalid,session", 1),
        ("A73F91C2", -1),
        ("A73F91C2", True),
        ("A73F91C2", 1.5),
    ],
)
def test_invalid_request_identity(session, request_id):
    with pytest.raises(ProtocolError):
        build_request(
            "STOP",
            "test",
            session=session,
            request_id=request_id,
        )


@pytest.mark.parametrize(
    "command,args",
    [
        ("", ()),
        ("STOP\nDRIVE", ()),
        ("STOP", ("bad,argument",)),
        ("STOP", ("bad\nargument",)),
        ("DRIVE", ("0.1\r", "0.2")),
        ("X" * 130, ()),
    ],
)
def test_invalid_request_contents(command, args):
    with pytest.raises(ProtocolError):
        build_request(
            command,
            *args,
            session="A73F91C2",
            request_id=1042,
        )


def test_request_fits_pico_receive_buffer():
    request = build_request(
        "MOVE",
        "forward",
        "0.300",
        session="A73F91C2",
        request_id=1042,
    )

    assert len(request.encode("ascii")) <= 127
