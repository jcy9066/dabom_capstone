"""Host-only request identity tests."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from raspberry.controllers.pico_uart_protocol_v2 import (
    MAX_REQUEST_ID,
    ProtocolError,
    RequestSequencer,
)


def test_sequential_ids():
    seq = RequestSequencer(session="TESTSESSION")
    assert [seq.next_id() for _ in range(3)] == [1, 2, 3]


def test_build_assigns_matching_id():
    seq = RequestSequencer(session="TESTSESSION")
    request_id, wire = seq.build("STOP", "test")

    assert request_id == 1
    assert wire == "REQ,TESTSESSION,1,STOP,test"


def test_new_instances_have_distinct_sessions():
    first = RequestSequencer()
    second = RequestSequencer()

    assert first.session != second.session


def test_concurrent_ids_unique():
    seq = RequestSequencer(session="TESTSESSION")

    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(executor.map(lambda _: seq.next_id(), range(1000)))

    assert len(ids) == len(set(ids))
    assert sorted(ids) == list(range(1, 1001))


def test_request_id_exhaustion():
    seq = RequestSequencer(session="TESTSESSION")
    seq._next_id = MAX_REQUEST_ID

    assert seq.next_id() == MAX_REQUEST_ID

    with pytest.raises(ProtocolError):
        seq.next_id()


@pytest.mark.parametrize("session", ["", "BAD,SESSION", "A" * 33])
def test_invalid_session(session):
    with pytest.raises(ProtocolError):
        RequestSequencer(session=session)
