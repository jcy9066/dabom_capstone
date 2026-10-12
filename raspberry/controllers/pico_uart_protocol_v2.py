"""Host-testable Pico UART V2 response parser.

This module is not yet connected to the live MotorController.
"""

import re
import secrets
import threading
from typing import Tuple


class ProtocolError(ValueError):
    """Invalid, stale or mismatched UART V2 response."""


_SESSION_RE = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
_REQUEST_RE = re.compile(r"(0|[1-9][0-9]*)\Z")


def parse_ack(
    response: str,
    *,
    session: str,
    request_id: int,
) -> Tuple[str, str]:
    """Validate a V2 acknowledgement against an outstanding request.

    Returns (status, payload). A matching ERR is returned as ERR;
    the caller must treat it as a failed command.
    """
    if not isinstance(response, str):
        raise ProtocolError("ACK must be text")

    if not isinstance(session, str) or not _SESSION_RE.fullmatch(session):
        raise ProtocolError("Invalid expected session")

    if (
        isinstance(request_id, bool)
        or not isinstance(request_id, int)
        or request_id < 1
        or request_id > MAX_REQUEST_ID
    ):
        raise ProtocolError("Invalid expected request ID")

    if "\r" in response or "\n" in response:
        raise ProtocolError("ACK contains a line break")

    parts = response.split(",", 4)

    if len(parts) != 5 or parts[0] != "ACK":
        raise ProtocolError("Invalid V2 ACK format")

    _, actual_session, actual_request, status, payload = parts

    if not _SESSION_RE.fullmatch(actual_session):
        raise ProtocolError("Invalid ACK session")

    if actual_session != session:
        raise ProtocolError("ACK session mismatch")

    if not _REQUEST_RE.fullmatch(actual_request):
        raise ProtocolError("Invalid ACK request ID")

    if int(actual_request) != request_id:
        raise ProtocolError("ACK request ID mismatch")

    if status not in ("OK", "ERR"):
        raise ProtocolError("Invalid ACK status")

    if not payload:
        raise ProtocolError("Missing ACK payload")

    if any(ord(char) < 32 or ord(char) > 126 for char in payload):
        raise ProtocolError("Invalid ACK payload characters")

    return status, payload


def build_request(
    command: str,
    *args: str,
    session: str,
    request_id: int,
) -> str:
    """Construct a bounded, ASCII-only Pico UART V2 request."""

    if not isinstance(session, str) or not _SESSION_RE.fullmatch(session):
        raise ProtocolError("Invalid request session")

    if (
        isinstance(request_id, bool)
        or not isinstance(request_id, int)
        or request_id < 1
        or request_id > MAX_REQUEST_ID
    ):
        raise ProtocolError("Invalid request ID")

    if not isinstance(command, str) or not re.fullmatch(
        r"[A-Z][A-Z0-9_]*", command
    ):
        raise ProtocolError("Invalid command")

    if not all(isinstance(arg, str) for arg in args):
        raise ProtocolError("Arguments must be strings")

    for arg in args:
        if not arg:
            raise ProtocolError("Empty argument")

        if any(
            ord(char) < 32
            or ord(char) > 126
            or char == ","
            for char in arg
        ):
            raise ProtocolError("Invalid argument characters")

    request = ",".join(
        ("REQ", session, str(request_id), command, *args)
    )

    try:
        encoded = request.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ProtocolError("Request must be ASCII") from exc

    if len(encoded) > 127:
        raise ProtocolError("Request exceeds Pico UART buffer")

    return request


MAX_REQUEST_ID = 4294967295


class RequestSequencer:
    """Thread-safe request identity generator for one UART session."""

    def __init__(self, *, session: str = None):
        if session is None:
            session = secrets.token_hex(16).upper()

        if not isinstance(session, str) or not _SESSION_RE.fullmatch(session):
            raise ProtocolError("Invalid session")

        self.session = session
        self._next_id = 1
        self._lock = threading.Lock()

    def next_id(self) -> int:
        with self._lock:
            if self._next_id > MAX_REQUEST_ID:
                raise ProtocolError("Request ID exhausted")

            request_id = self._next_id
            self._next_id += 1
            return request_id

    def build(self, command: str, *args: str) -> tuple[int, str]:
        with self._lock:
            if self._next_id > MAX_REQUEST_ID:
                raise ProtocolError("Request ID exhausted")

            request_id = self._next_id

            request = build_request(
                command,
                *args,
                session=self.session,
                request_id=request_id,
            )

            self._next_id += 1
            return request_id, request
