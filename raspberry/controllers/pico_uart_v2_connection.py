"""Dedicated, opt-in UART V2 STOP/PING connection for Raspberry Pi.

Unlike the legacy MotorController, this connection never sends ENC_STREAM,
legacy PING/STOP/LED, or MOVE/DRIVE.  It has exactly one UART reader.

No serial device can be opened without explicitly opting in.  Production
motion remains unsupported even when a device is opened.
"""
from __future__ import annotations

import re
import threading
from typing import Callable, Optional

from raspberry.controllers.pico_uart_protocol_v2 import RequestSequencer
from raspberry.controllers.pico_uart_v2_router import V2AckRouter


class V2ConnectionError(RuntimeError):
    """V2 handshake, response, or transport failed; no motion is permitted."""


class V2StopOnlyConnection:
    """Isolated Pi↔Pico V2 link (not a MotorController replacement).

    `serial_factory` is intended for host-only FakeSerial tests.
    Physical serial access is denied unless `allow_real_uart=True` is
    deliberately supplied.  `connect` and `reconnect` perform DISCOVER,
    HELLO and a correlated STOP; `ping`/`stop` require that handshake.
    """

    def __init__(
        self,
        *,
        serial_port: str = "/dev/serial0",
        baudrate: int = 115200,
        timeout_sec: float = 0.35,
        serial_factory: Optional[Callable[..., object]] = None,
        allow_real_uart: bool = False,
        sequencer_factory: Callable[..., RequestSequencer] = RequestSequencer,
    ) -> None:
        if timeout_sec <= 0 or timeout_sec > 5:
            raise ValueError("timeout_sec must be in (0, 5]")
        self.serial_port = serial_port
        self.baudrate = int(baudrate)
        self.timeout_sec = float(timeout_sec)
        self._serial_factory = serial_factory
        self._allow_real_uart = bool(allow_real_uart)
        self._sequencer_factory = sequencer_factory
        self._command_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._serial = None
        self._router: Optional[V2AckRouter] = None
        self._sequence: Optional[RequestSequencer] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_stop: Optional[threading.Event] = None
        self._generation = 0
        self._ready = False
        self._failure: Optional[str] = "not connected"
        self._boot_token: Optional[str] = None

    @property
    def ready(self) -> bool:
        with self._state_lock:
            return (
                self._ready
                and self._failure is None
                and self._serial is not None
                and bool(getattr(self._serial, "is_open", False))
            )

    @property
    def session(self) -> Optional[str]:
        with self._state_lock:
            return self._sequence.session if self._ready and self._sequence else None

    @property
    def failure(self) -> Optional[str]:
        with self._state_lock:
            return self._failure

    def _invalidate(self, reason: str, generation: int) -> None:
        with self._state_lock:
            if generation != self._generation:
                return
            if self._failure is not None:
                return
            self._ready = False
            self._failure = reason
            router = self._router
        if router is not None:
            router.invalidate()

    def _reader_loop(self, device, router: V2AckRouter, stop: threading.Event, generation: int) -> None:
        try:
            while not stop.is_set():
                try:
                    raw = device.readline()
                except Exception as exc:
                    self._invalidate(f"UART read error: {exc}", generation)
                    break
                if not raw:
                    # PySerial's finite read timeout permits checking the stop flag.
                    if not bool(getattr(device, "is_open", False)):
                        self._invalidate("UART disconnected", generation)
                        break
                    continue
                if len(raw) > 256:
                    self._invalidate("oversized UART response", generation)
                    break
                try:
                    line = raw.decode("ascii", errors="strict").rstrip("\r\n")
                except (UnicodeError, AttributeError):
                    self._invalidate("non-ASCII UART response", generation)
                    break
                if not line:
                    continue
                if line.startswith("READY,"):
                    router.feed(line)
                    self._invalidate("Pico READY/reboot detected", generation)
                    break
                if line.startswith("ACK,"):
                    router.feed(line)
                    if router.failed:
                        self._invalidate("unsolicited/mismatched ACK", generation)
                        break
                    continue
                # Only status telemetry is explicitly ignored. Legacy OK/ERR
                # cannot satisfy a V2 request or trigger a silent V1 fallback.
                if line in ("EVENT,FAILSAFE_STOP", "EVENT,LED_FAILSAFE_OFF"):
                    continue
                self._invalidate(f"unexpected UART V2 line: {line[:48]}", generation)
                break
        finally:
            if not stop.is_set():
                self._invalidate("UART reader terminated", generation)

    def _close_locked(self) -> None:
        with self._state_lock:
            device = self._serial
            router = self._router
            stop = self._reader_stop
            worker = self._reader_thread
            self._generation += 1  # Old readers cannot poison a new session.
            self._serial = None
            self._router = None
            self._sequence = None
            self._reader_stop = None
            self._reader_thread = None
            self._ready = False
            self._boot_token = None
            self._failure = "closed"
        if stop:
            stop.set()
        if router:
            router.invalidate()
        if device is not None:
            try:
                device.close()
            except Exception:
                pass
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=0.7)

    def close(self) -> None:
        """Disconnect without sending ANY motion or legacy UART commands."""
        with self._command_lock:
            self._close_locked()

    def _open_locked(self) -> None:
        factory = self._serial_factory
        if factory is None:
            if not self._allow_real_uart:
                raise V2ConnectionError("real UART is disabled; use FakeSerial for host tests")
            try:
                import serial  # pylint: disable=import-outside-toplevel
            except ImportError as exc:
                raise V2ConnectionError("pyserial is not installed") from exc
            factory = serial.Serial
        router = V2AckRouter()
        sequence = self._sequencer_factory()
        stop = threading.Event()
        try:
            device = factory(
                port=self.serial_port,
                baudrate=self.baudrate,
                timeout=0.05,
                write_timeout=self.timeout_sec,
            )
            if not getattr(device, "is_open", False):
                raise V2ConnectionError("opened UART is closed")
            # Discard stale bytes BEFORE the new session's reader starts.
            # A subsequent READY remains fatal and demands fresh handshake.
            if hasattr(device, "reset_input_buffer"):
                device.reset_input_buffer()
        except Exception as exc:
            if "device" in locals():
                try:
                    device.close()
                except Exception:
                    pass
            raise V2ConnectionError(f"UART open failed: {exc}") from exc
        with self._state_lock:
            self._generation += 1
            generation = self._generation
            self._serial = device
            self._router = router
            self._sequence = sequence
            self._reader_stop = stop
            self._failure = None
            self._ready = False
            worker = threading.Thread(
                target=self._reader_loop,
                args=(device, router, stop, generation),
                daemon=True,
                name="pico-v2-stop-only-reader",
            )
            self._reader_thread = worker
        worker.start()

    def _request_locked(self, command: str, expected: str, *args: str) -> str:
        with self._state_lock:
            device = self._serial
            router = self._router
            sequence = self._sequence
            generation = self._generation
            failure = self._failure
        if not device or not router or not sequence or failure is not None:
            raise V2ConnectionError(f"V2 link not usable: {failure}")
        try:
            request_id, frame = sequence.build(command, *args)
            router.begin(sequence.session, request_id)
            with self._write_lock:
                data = (frame + "\n").encode("ascii")
                if device.write(data) != len(data):
                    raise V2ConnectionError("short UART write")
                device.flush()
            status, payload = router.wait(self.timeout_sec)
            with self._state_lock:
                if self._generation != generation or self._failure is not None:
                    raise V2ConnectionError(f"V2 link invalidated: {self._failure}")
            if status != "OK" or payload != expected:
                raise V2ConnectionError(f"V2 {command} rejected/unexpected ACK: {status},{payload}")
            return payload
        except Exception as exc:
            self._invalidate(f"V2 {command} failed: {exc}", generation)
            raise V2ConnectionError(f"V2 {command} failed: {exc}") from exc

    def connect(self) -> str:
        """Explicit fresh serial open + DISCOVER, HELLO, correlated STOP.

        No reconnect attempts happen automatically or behind MOVE/DRIVE.
        """
        with self._command_lock:
            if self.ready:
                raise V2ConnectionError("already connected")
            self._close_locked()
            try:
                self._open_locked()
                with self._state_lock:
                    generation = self._generation
                    seq = self._sequence
                # DISCOVER's random boot token is checked below; it cannot
                # be predicted as a constant payload.
                token = self._request_boot_locked()
                if re.fullmatch(r"BOOT_[0-9A-F]{16}", token) is None:
                    self._invalidate("malformed boot token", generation)
                    raise V2ConnectionError("malformed Pico boot token")
                self._request_locked("HELLO", "HELLO", token[5:])
                self._request_locked("STOP", "STOP")
                with self._state_lock:
                    if generation != self._generation or self._failure is not None:
                        raise V2ConnectionError("Pico reboot during handshake")
                    self._boot_token = token
                    self._ready = True
                return seq.session
            except Exception as exc:
                self._close_locked()
                raise V2ConnectionError(f"V2 negotiation failed: {exc}") from exc

    def _request_boot_locked(self) -> str:
        """DISCOVER ACK has an unpredictable but strictly bounded payload."""
        with self._state_lock:
            device, router, seq, generation, failure = (
                self._serial, self._router, self._sequence, self._generation, self._failure
            )
        if not device or not router or not seq or failure is not None:
            raise V2ConnectionError("V2 not connected")
        try:
            rid, frame = seq.build("DISCOVER")
            router.begin(seq.session, rid)
            with self._write_lock:
                data = (frame + "\n").encode("ascii")
                if device.write(data) != len(data):
                    raise V2ConnectionError("short UART write")
                device.flush()
            status, payload = router.wait(self.timeout_sec)
            with self._state_lock:
                if generation != self._generation or self._failure is not None:
                    raise V2ConnectionError("Pico reboot during discovery")
            if status != "OK":
                raise V2ConnectionError(f"DISCOVER refused: {payload}")
            return payload
        except Exception as exc:
            self._invalidate(f"V2 DISCOVER failed: {exc}", generation)
            raise V2ConnectionError(f"V2 DISCOVER failed: {exc}") from exc

    def reconnect(self) -> str:
        """Explicitly discard previous link/session; never resend old IDs."""
        with self._command_lock:
            self._close_locked()
            return self.connect()

    def _command(self, command: str, expected: str) -> str:
        with self._command_lock:
            if not self.ready:
                raise V2ConnectionError("V2 handshake/STOP not complete")
            try:
                return self._request_locked(command, expected)
            except Exception:
                self._close_locked()
                raise

    def stop(self) -> str:
        return self._command("STOP", "STOP")

    def ping(self) -> str:
        return self._command("PING", "PONG")

    def move(self, *_args, **_kwargs):
        raise V2ConnectionError("V2 STOP-only mode prohibits motion")

    def drive(self, *_args, **_kwargs):
        raise V2ConnectionError("V2 STOP-only mode prohibits motion")
