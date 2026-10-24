from __future__ import annotations

import threading
import time

import serial


class GpsReader:
    def __init__(
        self,
        port: str = "/dev/ttyAMA1",
        baudrate: int = 9600,
        timeout_sec: float = 1.0,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout_sec = timeout_sec

        self._running = False
        self._thread: threading.Thread | None = None
        self._serial = None
        self._lock = threading.Lock()

        self._state = {
            "lat": None,
            "lng": None,
            "alt": None,
            "fix": False,
            "satellites": 0,
            "hdop": None,
            "updated_at": None,
        }

    @staticmethod
    def _nmea_to_decimal(
        value: str,
        direction: str,
    ) -> float | None:
        if not value or not direction:
            return None

        try:
            raw = float(value)
        except ValueError:
            return None

        degrees = int(raw // 100)
        minutes = raw - (degrees * 100)

        decimal = degrees + (minutes / 60.0)

        if direction in {"S", "W"}:
            decimal = -decimal

        return decimal

    @staticmethod
    def _checksum_ok(line: str) -> bool:
        if not line.startswith("$") or "*" not in line:
            return False

        try:
            body, checksum_text = line[1:].split("*", 1)
            expected = int(checksum_text[:2], 16)
        except (ValueError, IndexError):
            return False

        calculated = 0

        for char in body:
            calculated ^= ord(char)

        return calculated == expected

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        self._running = True

        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="gps-reader",
        )
        self._thread.start()

    def close(self) -> None:
        self._running = False

        # serial timeout이 1초이므로 reader가 자연스럽게
        # readline()에서 빠져나올 시간을 먼저 준다.
        if self._thread is not None:
            self._thread.join(
                timeout=self.timeout_sec + 1.0
            )

        # 혹시 thread가 종료되지 않았을 때만 마지막으로 닫는다.
        if (
            self._thread is not None
            and self._thread.is_alive()
            and self._serial is not None
        ):
            try:
                self._serial.close()
            except Exception:
                pass

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set_no_fix(
        self,
        satellites: int,
        hdop: float | None,
    ) -> None:
        with self._lock:
            self._state.update(
                {
                    "lat": None,
                    "lng": None,
                    "alt": None,
                    "fix": False,
                    "satellites": satellites,
                    "hdop": hdop,
                    "updated_at": time.time(),
                }
            )

    def _handle_gga(self, line: str) -> None:
        if not self._checksum_ok(line):
            return

        fields = line.split(",")

        if len(fields) < 10:
            return

        try:
            fix_quality = int(fields[6] or "0")
        except ValueError:
            fix_quality = 0

        try:
            satellites = int(fields[7] or "0")
        except ValueError:
            satellites = 0

        try:
            hdop = float(fields[8]) if fields[8] else None
        except ValueError:
            hdop = None

        if fix_quality <= 0:
            self._set_no_fix(
                satellites=satellites,
                hdop=hdop,
            )
            return

        lat = self._nmea_to_decimal(
            fields[2],
            fields[3],
        )

        lng = self._nmea_to_decimal(
            fields[4],
            fields[5],
        )

        try:
            alt = float(fields[9]) if fields[9] else None
        except ValueError:
            alt = None

        if lat is None or lng is None:
            return

        with self._lock:
            self._state.update(
                {
                    "lat": lat,
                    "lng": lng,
                    "alt": alt,
                    "fix": True,
                    "satellites": satellites,
                    "hdop": hdop,
                    "updated_at": time.time(),
                }
            )

    def _run(self) -> None:
        while self._running:
            try:
                with serial.Serial(
                    self.port,
                    baudrate=self.baudrate,
                    timeout=self.timeout_sec,
                ) as gps:
                    self._serial = gps

                    print(
                        f"[gps] connected: "
                        f"{self.port} @ {self.baudrate}"
                    )

                    while self._running:
                        raw = gps.readline()

                        if not raw:
                            continue

                        line = raw.decode(
                            "ascii",
                            errors="ignore",
                        ).strip()

                        if line.startswith(
                            ("$GPGGA", "$GNGGA")
                        ):
                            self._handle_gga(line)

            except (
                serial.SerialException,
                OSError,
            ) as exc:
                if self._running:
                    print(
                        f"[gps] 연결 실패: {exc}"
                    )
                    time.sleep(2.0)

            finally:
                self._serial = None
