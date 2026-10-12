"""PR #80 review follow-up: host-only tests; never touch physical UART."""
from __future__ import annotations

import inspect
from pathlib import Path
import subprocess
import tempfile
import threading

import pytest

from raspberry.controllers.motor_controller import MotorController
from raspberry.controllers.pico_uart_v2_connection import V2ConnectionError, V2StopOnlyConnection
from raspberry.controllers.pico_uart_v2_router import V2AckRouter

PICO = Path(__file__).resolve().parents[1] / 'raspberry' / 'pico_w_sdk'


def test_both_serial_entrypoints_require_exclusive_opens():
    assert 'exclusive=True' in inspect.getsource(MotorController._ensure_connected_locked)
    assert 'exclusive=True' in inspect.getsource(V2StopOnlyConnection._open_locked)


def test_v2_rejects_exclusively_owned_port_without_any_exchange():
    observed = []
    def already_owned(**kwargs):
        observed.append(kwargs)
        raise OSError('exclusive lock busy (simulated V1 owner)')
    link = V2StopOnlyConnection(serial_factory=already_owned, timeout_sec=0.1)
    with pytest.raises(V2ConnectionError, match='open failed'):
        link.connect()
    assert observed and observed[0]['exclusive'] is True
    assert link.ready is False and link.session is None


class ClosableSerial:
    is_open = True
    def __init__(self):
        self.closes = 0
    def close(self):
        self.closes += 1
        self.is_open = False


def test_invalidation_closes_port_and_wakes_router_without_deadlock():
    link = V2StopOnlyConnection(serial_factory=lambda **_: None)
    device = ClosableSerial()
    stop = threading.Event()
    router = V2AckRouter()
    with link._state_lock:
        link._generation = 7
        link._serial = device
        link._reader_stop = stop
        link._router = router
        link._failure = None
    link._invalidate('reboot/error', 7)
    assert stop.is_set()
    assert device.closes == 1 and not device.is_open
    assert router.failed
    assert not link.ready and link.failure == 'reboot/error'
    link._invalidate('old fault', 6)
    assert device.closes == 1 and link.failure == 'reboot/error'
    link.close()
    assert device.closes >= 1 and not device.is_open
    assert link.failure == 'closed'


FIRMWARE_HARNESS = r'''
#include "uart_v2_stop_only.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static unsigned stops;
static void force_stop(void) { ++stops; }
static void expect(const char *req, const char *ack) {
    char reply[128];
    unsigned was = stops;
    assert(uart_v2_stop_only(req, reply, sizeof(reply), force_stop));
    assert(stops == was + 1);
    assert(strcmp(reply, ack) == 0);
}
int main(void) {
    uart_v2_stop_only_reset(0xAABBCCDDEEFF0011ULL);
    expect("REQ,S1,1,DISCOVER", "ACK,S1,1,OK,BOOT_AABBCCDDEEFF0011");
    expect("REQ,S1,2,HELLO,AABBCCDDEEFF0011", "ACK,S1,2,OK,HELLO");
    expect("REQ,S1,3,STOP", "ACK,S1,3,OK,STOP");
    expect("REQ,S1,4,MOVE,forward,1", "ACK,S1,4,ERR,MOTION_DISABLED");
    expect("REQ,S1,4,DRIVE,1,1", "ACK,S1,4,ERR,MOTION_DISABLED");
    expect("REQ,S1,4,LED,1", "ACK,S1,4,ERR,MOTION_DISABLED");
    expect("REQ,S1,4,PING", "ACK,S1,4,OK,PONG");
    expect("REQ,S1,4,STOP", "ACK,S1,4,ERR,STALE_ID");
    unsigned was = stops;
    char reply[128];
    assert(!uart_v2_stop_only("MOVE,forward,1", reply, sizeof(reply), force_stop));
    assert(stops == was + 1);
    uart_v2_stop_only_reset(0x1122334455667788ULL);
    expect("REQ,S1,5,STOP", "ACK,S1,5,ERR,NO_SESSION");
    return 0;
}
'''


def test_actual_experimental_firmware_parser_blocks_motion_and_forces_stop():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d)
        src = path / 'stop_only_contract.c'
        exe = path / 'stop_only_contract'
        src.write_text(FIRMWARE_HARNESS)
        subprocess.run([
            'gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pedantic',
            '-I', str(PICO), str(src), str(PICO / 'uart_v2_stop_only.c'),
            '-o', str(exe),
        ], check=True, capture_output=True, text=True, timeout=30)
        subprocess.run([str(exe)], check=True, capture_output=True, text=True, timeout=10)


def test_arm_capable_host_model_cannot_compile_as_firmware_without_opt_in():
    with tempfile.TemporaryDirectory() as d:
        result = subprocess.run([
            'gcc', '-std=c11', '-Wall', '-Wextra', '-Werror',
            '-c', str(PICO / 'uart_protocol_v2.c'),
            '-o', str(Path(d) / 'model.o'),
        ], capture_output=True, text=True, timeout=30)
        assert result.returncode != 0
        assert 'HOST-ONLY model' in result.stderr
    cmake = (PICO / 'CMakeLists.txt').read_text()
    assert 'uart_v2_stop_only.c' in cmake
    assert 'uart_protocol_v2.c' not in cmake
