from __future__ import annotations

import math
import os
import threading
import time
from typing import Any

from smbus2 import SMBus


class Bno055Reader:
    """Minimal BNO055 I2C reader for Raspberry Pi 4 I2C-1.

    Wiring:
      BNO055 VIN/3V3 -> Pi 3.3V (physical pin 1)
      BNO055 SDA     -> GPIO2/SDA1 (physical pin 3)
      BNO055 SCL     -> GPIO3/SCL1 (physical pin 5)
      BNO055 GND     -> Pi GND (physical pin 14)

    The reader intentionally exposes raw sensor orientation/health telemetry.
    It does not feed Nav2/odometry directly because chassis mounting yaw/axis
    calibration is a separate physical calibration step.
    """

    CHIP_ID_REG = 0x00
    CHIP_ID = 0xA0
    PAGE_ID_REG = 0x07
    EULER_H_LSB_REG = 0x1A
    QUATERNION_W_LSB_REG = 0x20
    TEMP_REG = 0x34
    CALIB_STAT_REG = 0x35
    OPR_MODE_REG = 0x3D
    PWR_MODE_REG = 0x3E
    SYS_TRIGGER_REG = 0x3F

    CONFIG_MODE = 0x00
    NDOF_MODE = 0x0C
    POWER_MODE_NORMAL = 0x00

    def __init__(
        self,
        bus: int | None = None,
        address: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.enabled = (
            enabled
            if enabled is not None
            else os.getenv("BNO055_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.bus_number = (
            int(bus)
            if bus is not None
            else int(os.getenv("BNO055_I2C_BUS", "1"))
        )
        if address is None:
            raw_address = os.getenv("BNO055_I2C_ADDRESS", "0x28").strip()
            self.address = int(raw_address, 0)
        else:
            self.address = int(address)

        if not 0 <= self.bus_number <= 255:
            raise ValueError("BNO055 I2C bus must be between 0 and 255")
        if not 0x08 <= self.address <= 0x77:
            raise ValueError("BNO055 I2C address must be a valid 7-bit address")

        self._lock = threading.Lock()
        self._bus: SMBus | None = None
        self._connected = False
        self._last_error: str | None = None
        self._last_success_at: float | None = None

    @staticmethod
    def _int16(lsb: int, msb: int) -> int:
        value = (int(msb) << 8) | int(lsb)
        return value - 65536 if value & 0x8000 else value

    def _open_locked(self) -> None:
        if not self.enabled or self._bus is not None:
            return

        bus = SMBus(self.bus_number)
        try:
            chip_id = bus.read_byte_data(self.address, self.CHIP_ID_REG)
            if chip_id != self.CHIP_ID:
                raise RuntimeError(
                    f"BNO055 chip id mismatch: 0x{chip_id:02x}"
                )

            # Datasheet-safe transition into NDOF fusion mode.
            bus.write_byte_data(self.address, self.OPR_MODE_REG, self.CONFIG_MODE)
            time.sleep(0.03)
            bus.write_byte_data(self.address, self.PWR_MODE_REG, self.POWER_MODE_NORMAL)
            time.sleep(0.01)
            bus.write_byte_data(self.address, self.PAGE_ID_REG, 0x00)
            bus.write_byte_data(self.address, self.SYS_TRIGGER_REG, 0x00)
            bus.write_byte_data(self.address, self.OPR_MODE_REG, self.NDOF_MODE)
            time.sleep(0.03)
        except Exception:
            bus.close()
            raise

        self._bus = bus
        self._connected = True
        self._last_error = None

    def _close_locked(self) -> None:
        if self._bus is not None:
            try:
                self._bus.close()
            except OSError:
                pass
        self._bus = None
        self._connected = False

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _read_snapshot_locked(self) -> dict[str, Any]:
        self._open_locked()
        if self._bus is None:
            raise RuntimeError("BNO055 is disabled")

        euler = self._bus.read_i2c_block_data(
            self.address,
            self.EULER_H_LSB_REG,
            6,
        )
        quaternion = self._bus.read_i2c_block_data(
            self.address,
            self.QUATERNION_W_LSB_REG,
            8,
        )
        calib = self._bus.read_byte_data(self.address, self.CALIB_STAT_REG)
        temp_raw = self._bus.read_byte_data(self.address, self.TEMP_REG)

        heading = self._int16(euler[0], euler[1]) / 16.0
        roll = self._int16(euler[2], euler[3]) / 16.0
        pitch = self._int16(euler[4], euler[5]) / 16.0

        qw = self._int16(quaternion[0], quaternion[1]) / 16384.0
        qx = self._int16(quaternion[2], quaternion[3]) / 16384.0
        qy = self._int16(quaternion[4], quaternion[5]) / 16384.0
        qz = self._int16(quaternion[6], quaternion[7]) / 16384.0

        if not all(
            math.isfinite(value)
            for value in (heading, roll, pitch, qw, qx, qy, qz)
        ):
            raise RuntimeError("BNO055 returned non-finite orientation data")

        # BNO055 temperature register is signed 8-bit Celsius in default units.
        temp_c = temp_raw - 256 if temp_raw & 0x80 else temp_raw

        now = time.time()
        self._connected = True
        self._last_error = None
        self._last_success_at = now

        return {
            "bno_connected": True,
            "bno_heading_deg": round(heading % 360.0, 2),
            "bno_roll_deg": round(roll, 2),
            "bno_pitch_deg": round(pitch, 2),
            "bno_quaternion_w": round(qw, 6),
            "bno_quaternion_x": round(qx, 6),
            "bno_quaternion_y": round(qy, 6),
            "bno_quaternion_z": round(qz, 6),
            "bno_calib_sys": (calib >> 6) & 0x03,
            "bno_calib_gyro": (calib >> 4) & 0x03,
            "bno_calib_accel": (calib >> 2) & 0x03,
            "bno_calib_mag": calib & 0x03,
            "bno_temp_c": int(temp_c),
            "bno_updated_at": now,
            "bno_error": None,
        }

    def snapshot(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                "bno_connected": False,
                "bno_heading_deg": None,
                "bno_roll_deg": None,
                "bno_pitch_deg": None,
                "bno_quaternion_w": None,
                "bno_quaternion_x": None,
                "bno_quaternion_y": None,
                "bno_quaternion_z": None,
                "bno_calib_sys": None,
                "bno_calib_gyro": None,
                "bno_calib_accel": None,
                "bno_calib_mag": None,
                "bno_temp_c": None,
                "bno_updated_at": None,
                "bno_error": "disabled",
            }

        with self._lock:
            try:
                return self._read_snapshot_locked()
            except (OSError, RuntimeError, ValueError) as exc:
                self._last_error = str(exc)
                self._close_locked()
                return {
                    "bno_connected": False,
                    "bno_heading_deg": None,
                    "bno_roll_deg": None,
                    "bno_pitch_deg": None,
                    "bno_quaternion_w": None,
                    "bno_quaternion_x": None,
                    "bno_quaternion_y": None,
                    "bno_quaternion_z": None,
                    "bno_calib_sys": None,
                    "bno_calib_gyro": None,
                    "bno_calib_accel": None,
                    "bno_calib_mag": None,
                    "bno_temp_c": None,
                    "bno_updated_at": self._last_success_at,
                    "bno_error": self._last_error,
                }
