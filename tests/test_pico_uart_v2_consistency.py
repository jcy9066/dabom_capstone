"""UART V2 request identity consistency tests."""

import pytest

from raspberry.controllers.pico_uart_protocol_v2 import (
    ProtocolError,
    RequestSequencer,
    parse_ack,
)


@pytest.mark.parametrize("request_id", [0, -1, 4294967296, True])
def test_ack_invalid_expected_request_id(request_id):
    with pytest.raises(ProtocolError):
        parse_ack(
            "ACK,TESTSESSION,1,OK,STOP",
            session="TESTSESSION",
            request_id=request_id,
        )


def test_failed_build_does_not_consume_request_id():
    sequencer = RequestSequencer(session="TESTSESSION")

    with pytest.raises(ProtocolError):
        sequencer.build("INVALID,COMMAND")

    request_id, request = sequencer.build("STOP", "test")

    assert request_id == 1
    assert request == "REQ,TESTSESSION,1,STOP,test"


def test_oversized_request_does_not_consume_id():
    sequencer = RequestSequencer(session="TESTSESSION")

    with pytest.raises(ProtocolError):
        sequencer.build("STOP", "x" * 200)

    request_id, _ = sequencer.build("STOP", "test")
    assert request_id == 1


def test_sequencer_build_remains_monotonic():
    sequencer = RequestSequencer(session="TESTSESSION")

    ids = [
        sequencer.build("PING")[0]
        for _ in range(100)
    ]

    assert ids == list(range(1, 101))
