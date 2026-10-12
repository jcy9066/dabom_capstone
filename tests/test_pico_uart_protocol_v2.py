"""Host-only regression tests for Pico UART Protocol V2."""

import pytest

from raspberry.controllers.pico_uart_protocol_v2 import (
    ProtocolError,
    parse_ack,
)


def test_matching_ack():
    result = parse_ack(
        "ACK,A73F91C2,1042,OK,STOP",
        session="A73F91C2",
        request_id=1042,
    )
    assert result == ("OK", "STOP")


@pytest.mark.parametrize(
    "response",
    [
        "ACK,A73F91C2,1041,OK,STOP",
        "ACK,OTHER123,1042,OK,STOP",
        "OK,STOP",
        "EVENT,ENC,1,2,3,4,100",
        "ACK,A73F91C2,1042",
        "ACK,A73F91C2,not-an-id,OK,STOP",
        "ACK,A73F91C2,1042,UNKNOWN,STOP",
    ],
)
def test_invalid_ack_rejected(response):
    with pytest.raises(ProtocolError):
        parse_ack(
            response,
            session="A73F91C2",
            request_id=1042,
        )


def test_matching_error_ack():
    result = parse_ack(
        "ACK,A73F91C2,1042,ERR,invalid direction",
        session="A73F91C2",
        request_id=1042,
    )
    assert result == ("ERR", "invalid direction")
