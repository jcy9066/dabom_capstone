"""Opt-in, host-testable V2 STOP/PING UART adapter. Never controls motion.

NOT wired to MotorController. No implicit fallback to legacy ACKs.
"""
from __future__ import annotations

import threading
from raspberry.controllers.pico_uart_protocol_v2 import RequestSequencer, parse_ack, ProtocolError


class V2LinkError(RuntimeError):
    pass


class StopOnlyV2Link:
    def __init__(self, serial_port, *, session=None, max_lines=8):
        if max_lines < 1:
            raise ValueError("max_lines must be positive")
        self._serial = serial_port
        self._sequence = RequestSequencer(session=session)
        self._lock = threading.Lock()
        self._failed = False
        self._max_lines = max_lines

    @property
    def failed(self):
        return self._failed

    def _invalidate(self):
        self._failed = True
        try:
            self._serial.close()
        except Exception:
            pass

    def exchange(self, command):
        if command not in ("STOP", "PING"):
            raise V2LinkError("motion and other commands prohibited")
        with self._lock:
            if self._failed:
                raise V2LinkError("link invalidated; new verified session required")
            try:
                request_id, frame = self._sequence.build(command)
                self._serial.write((frame + "\n").encode("ascii"))
                for _ in range(self._max_lines):
                    raw = self._serial.readline()
                    if not raw:
                        raise V2LinkError("ACK timeout")
                    line = raw.decode("ascii").strip("\r\n")
                    if line.startswith("READY,"):
                        raise V2LinkError("Pico reboot during request")
                    if line.startswith("EVENT,"):
                        continue
                    status, payload = parse_ack(line, session=self._sequence.session, request_id=request_id)
                    expected = "STOP" if command == "STOP" else "PONG"
                    if status != "OK" or payload != expected:
                        raise V2LinkError("ACK status/payload mismatch")
                    return payload
                raise V2LinkError("ACK not received within line budget")
            except (OSError, UnicodeError, ProtocolError, V2LinkError) as exc:
                self._invalidate()
                raise V2LinkError(str(exc)) from exc
