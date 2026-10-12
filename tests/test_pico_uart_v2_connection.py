"""No physical Pi/Pico opens: serial_factory always supplies a fake."""
from __future__ import annotations
import queue
import threading
import time
from collections import Counter

import pytest

from raspberry.controllers.pico_uart_protocol_v2 import RequestSequencer
from raspberry.controllers.pico_uart_v2_connection import V2ConnectionError, V2StopOnlyConnection


class FakeSerial:
    def __init__(self, pico, **_kwargs):
        self.pico = pico
        self.is_open = True
        self.writes = []
        self.incoming = queue.Queue()
        self._lock = threading.Lock()

    def reset_input_buffer(self):
        while not self.incoming.empty():
            self.incoming.get_nowait()

    def write(self, data):
        with self._lock:
            if not self.is_open:
                raise OSError("port closed")
            self.writes.append(data)
        lines = self.pico(data.decode("ascii").rstrip("\r\n"))
        if lines is not None:
            if isinstance(lines, (str, bytes)):
                lines = [lines]
            for line in lines:
                if isinstance(line, str):
                    line = (line + "\n").encode("ascii")
                self.incoming.put(line)
        return len(data)

    def flush(self):
        pass

    def readline(self):
        try:
            return self.incoming.get(timeout=0.015)
        except queue.Empty:
            return b""

    def close(self):
        self.is_open = False

    def inject(self, line):
        self.incoming.put((line + "\n").encode("ascii"))


class FakePico:
    def __init__(self, boot="AABBCCDDEEFF0011"):
        self.boot = boot
        self.session = None
        self.last_id = 0
        self.intercept = None
        self.commands = []

    def __call__(self, frame):
        if self.intercept:
            override = self.intercept(frame)
            if override is not NotImplemented:
                return override
        parts = frame.split(",")
        assert parts[0] == "REQ", frame
        _, session, id_str, cmd, *args = parts
        request_id = int(id_str)
        self.commands.append((session, request_id, cmd, args))
        if cmd == "DISCOVER" and request_id == 1:
            status, payload = "OK", "BOOT_" + self.boot
        elif cmd == "HELLO" and request_id == 2 and args == [self.boot]:
            if self.session == session:
                status, payload = "ERR", "STALE_ID"
            else:
                self.session = session
                self.last_id = 2
                status, payload = "OK", "HELLO"
        elif cmd in ("STOP", "PING"):
            if self.session != session:
                status, payload = "ERR", "SESSION_MISMATCH"
            elif request_id <= self.last_id:
                status, payload = "ERR", "STALE_ID"
            else:
                self.last_id = request_id
                status, payload = "OK", "STOP" if cmd == "STOP" else "PONG"
        else:
            status, payload = "ERR", "MOTION_DISABLED"
        return f"ACK,{session},{id_str},{status},{payload}"


def make_link(pico=None, *, fakes=None, sessions=("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB")):
    pico = pico or FakePico()
    issued = iter(sessions)
    captured = [] if fakes is None else fakes
    def factory(**kwargs):
        dev = FakeSerial(pico, **kwargs)
        captured.append(dev)
        return dev
    conn = V2StopOnlyConnection(
        serial_factory=factory,
        timeout_sec=0.12,
        sequencer_factory=lambda: RequestSequencer(session=next(issued)),
    )
    return conn, captured, pico


def test_default_denies_real_uart_and_does_not_auto_open():
    link = V2StopOnlyConnection()
    with pytest.raises(V2ConnectionError, match="disabled"):
        link.connect()
    assert not link.ready


def test_handshake_uses_only_v2_frames_and_sends_initial_stop():
    link, devs, pico = make_link()
    try:
        sess = link.connect()
        assert sess == "A" * 32
        assert link.ready and link.session == sess
        frames = [w.decode().strip() for w in devs[0].writes]
        assert frames == [
            f"REQ,{sess},1,DISCOVER",
            f"REQ,{sess},2,HELLO,AABBCCDDEEFF0011",
            f"REQ,{sess},3,STOP",
        ]
        assert link.ping() == "PONG"
        assert link.stop() == "STOP"
        assert [x[2] for x in pico.commands] == ["DISCOVER", "HELLO", "STOP", "PING", "STOP"]
    finally:
        link.close()
    assert not devs[0].is_open


def test_move_and_drive_never_write():
    link, devs, _ = make_link()
    with pytest.raises(V2ConnectionError, match="handshake"):
        link.ping()
    link.connect()
    before = len(devs[0].writes)
    with pytest.raises(V2ConnectionError, match="prohibits"):
        link.move("forward")
    with pytest.raises(V2ConnectionError, match="prohibits"):
        link.drive(1, 1)
    assert len(devs[0].writes) == before
    link.close()


@pytest.mark.parametrize("mode", ["timeout_discover", "bad_discover", "bad_hello", "wrong_id", "legacy_ack", "bad_stop_ack"])
def test_handshake_errors_close_serial_and_leave_not_ready(mode):
    pico = FakePico()
    def interceptor(frame):
        if mode == "timeout_discover" and frame.endswith(",DISCOVER"):
            return None
        if mode == "bad_discover" and frame.endswith(",DISCOVER"):
            return "ACK," + frame.split(",")[1] + ",1,OK,BOOT_BAD"
        if mode == "bad_hello" and ",HELLO," in frame:
            return "ACK," + frame.split(",")[1] + ",2,ERR,BOOT_MISMATCH"
        if mode == "wrong_id" and frame.endswith(",DISCOVER"):
            return "ACK," + frame.split(",")[1] + ",2,OK,BOOT_AABBCCDDEEFF0011"
        if mode == "legacy_ack":
            return "OK,PONG"
        if mode == "bad_stop_ack" and frame.endswith(",STOP"):
            return "ACK," + frame.split(",")[1] + ",3,OK,PONG"
        return NotImplemented
    pico.intercept = interceptor
    link, devs, _ = make_link(pico)
    with pytest.raises(V2ConnectionError):
        link.connect()
    assert devs and not devs[0].is_open
    assert not link.ready


def test_ready_during_hello_aborts_connection():
    pico = FakePico()
    pico.intercept = lambda frame: "READY,PICO" if ",HELLO," in frame else NotImplemented
    link, devs, _ = make_link(pico)
    with pytest.raises(V2ConnectionError):
        link.connect()
    assert not devs[0].is_open
    assert not link.ready


def test_ready_after_handshake_invalidates_and_requires_explicit_reconnect():
    link, devs, _ = make_link()
    link.connect()
    devs[0].inject("READY,PICO")
    deadline = time.monotonic() + 0.5
    while link.ready and time.monotonic() < deadline:
        time.sleep(0.002)
    assert not link.ready
    before = len(devs[0].writes)
    with pytest.raises(V2ConnectionError):
        link.ping()
    assert len(devs[0].writes) == before
    sess2 = link.reconnect()
    assert sess2 == "B" * 32
    assert not devs[0].is_open
    assert link.ready
    link.close()


def test_reconnect_drops_old_session_even_when_request_id_restarts():
    link, devs, pico = make_link()
    old = link.connect()
    new = link.reconnect()
    assert old != new
    assert pico.session == new
    # Old frame cannot impersonate a matching ID 4 for the new session.
    devs[-1].inject(f"ACK,{old},4,OK,PONG")
    deadline = time.monotonic() + 0.5
    while link.ready and time.monotonic() < deadline:
        time.sleep(0.002)
    assert not link.ready
    with pytest.raises(V2ConnectionError):
        link.ping()
    link.close()


def test_timeout_after_handshake_closes_uart_and_never_falls_back_to_v1():
    pico = FakePico()
    pico.intercept = lambda frame: None if frame.endswith(",PING") else NotImplemented
    link, devs, _ = make_link(pico)
    link.connect()
    with pytest.raises(V2ConnectionError):
        link.ping()
    assert not devs[0].is_open
    assert not link.ready
    assert not any(b"PING\n" == frame for frame in devs[0].writes)


def test_wrong_ack_during_stop_invalidates_without_fallback():
    pico = FakePico()
    pico.intercept = lambda frame: f"ACK,{frame.split(',')[1]},4,OK,PONG" if frame.endswith(",STOP") and ",3," not in frame else NotImplemented
    link, devs, _ = make_link(pico)
    link.connect()
    with pytest.raises(V2ConnectionError):
        link.stop()
    assert not link.ready and not devs[0].is_open


def test_unsolicited_ack_poisoning_marks_link_failed():
    link, devs, _ = make_link()
    link.connect()
    devs[0].inject("ACK,UNEXPECTED,100,OK,STOP")
    deadline = time.monotonic() + 0.5
    while link.ready and time.monotonic() < deadline:
        time.sleep(0.002)
    assert not link.ready
    with pytest.raises(V2ConnectionError):
        link.ping()
    link.close()


def test_reader_error_invalidates():
    link, devs, _ = make_link()
    link.connect()
    port = devs[0]
    port.close()
    deadline = time.monotonic() + 0.5
    while link.ready and time.monotonic() < deadline:
        time.sleep(0.002)
    assert not link.ready
    with pytest.raises(V2ConnectionError):
        link.stop()
    link.close()


def test_duplicate_connect_does_not_open_new_serial():
    link, devs, _ = make_link()
    link.connect()
    with pytest.raises(V2ConnectionError, match="already connected"):
        link.connect()
    assert len(devs) == 1 and link.ready
    link.close()


def test_repeated_ping_is_serialized_and_has_unique_id():
    link, devs, pico = make_link()
    link.connect()
    errs = []
    def ping():
        try:
            assert link.ping() == "PONG"
        except Exception as e:
            errs.append(e)
    threads = [threading.Thread(target=ping) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=1)
    assert not errs
    ids = [i for _, i, cmd, _ in pico.commands if cmd == "PING"]
    assert ids == list(range(4, 12))
    link.close()
