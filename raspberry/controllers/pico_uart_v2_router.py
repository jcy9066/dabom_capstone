"""Host-only UART V2 ACK router.

This module does not open serial ports or authorize motor motion.
"""

import threading

from raspberry.controllers.pico_uart_protocol_v2 import (
    ProtocolError,
    parse_ack,
)


class V2RouterError(RuntimeError):
    pass


class V2AckRouter:
    def __init__(self):
        self._condition = threading.Condition()
        self._pending = None
        self._response = None
        self._failed = False

    @property
    def failed(self):
        with self._condition:
            return self._failed

    def begin(self, session, request_id):
        with self._condition:
            if self._failed:
                raise V2RouterError("V2 link invalidated")
            if self._pending is not None:
                raise V2RouterError("V2 request already pending")
            self._pending = (session, request_id)
            self._response = None

    def feed(self, line):
        with self._condition:
            if line.startswith("READY,"):
                self._failed = True
                self._response = None
                self._condition.notify_all()
                return True

            if not line.startswith("ACK,"):
                return False

            if self._pending is None:
                self._failed = True
                self._condition.notify_all()
                return True

            session, request_id = self._pending

            try:
                self._response = parse_ack(
                    line,
                    session=session,
                    request_id=request_id,
                )
            except ProtocolError:
                self._failed = True

            self._condition.notify_all()
            return True

    def wait(self, timeout):
        with self._condition:
            if self._pending is None:
                raise V2RouterError("No pending V2 request")

            if not self._condition.wait_for(
                lambda: self._response is not None or self._failed,
                timeout=timeout,
            ):
                self._failed = True

            if self._failed:
                raise V2RouterError("V2 ACK rejected or timed out")

            response = self._response
            self._pending = None
            self._response = None
            return response

    def invalidate(self):
        with self._condition:
            self._failed = True
            self._response = None
            self._condition.notify_all()
