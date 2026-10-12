"""Compile real Pico stop-only C protocol as host C, no motor hardware."""
from pathlib import Path
import subprocess
import tempfile

SDK = Path(__file__).resolve().parents[1] / "raspberry/pico_w_sdk"

HARNESS = r'''
#include "uart_v2_stop_only.h"
#include <assert.h>
#include <string.h>
static int stops;
static void stop(void) { ++stops; }
static char out[128];
static void check(const char *line, const char *expected) {
    assert(uart_v2_stop_only(line, out, sizeof(out), stop));
    assert(strcmp(out, expected) == 0);
}
int main(void) {
    uart_v2_stop_only_reset(0xAA11223344556677ULL);
    check("REQ,SESSION_A,1,STOP", "ACK,SESSION_A,1,ERR,NO_SESSION");
    check("REQ,SESSION_A,2,DISCOVER", "ACK,SESSION_A,2,ERR,DISCOVER_ID");
    check("REQ,SESSION_A,1,DISCOVER", "ACK,SESSION_A,1,OK,BOOT_AA11223344556677");
    check("REQ,SESSION_A,2,HELLO,0000000000000000", "ACK,SESSION_A,2,ERR,BOOT_MISMATCH");
    check("REQ,SESSION_A,2,HELLO,AA11223344556677", "ACK,SESSION_A,2,OK,HELLO");
    check("REQ,SESSION_A,3,PING", "ACK,SESSION_A,3,OK,PONG");
    check("REQ,SESSION_A,4,STOP", "ACK,SESSION_A,4,OK,STOP");
    check("REQ,SESSION_A,4,STOP", "ACK,SESSION_A,4,ERR,STALE_ID");
    check("REQ,SESSION_A,3,STOP", "ACK,SESSION_A,3,ERR,STALE_ID");
    check("REQ,OTHER,5,STOP", "ACK,OTHER,5,ERR,SESSION_MISMATCH");
    check("REQ,SESSION_A,5,DRIVE,1,1", "ACK,SESSION_A,5,ERR,MOTION_DISABLED");
    check("REQ,SESSION_B,1,DISCOVER", "ACK,SESSION_B,1,OK,BOOT_AA11223344556677");
    check("REQ,SESSION_B,2,HELLO,AA11223344556677", "ACK,SESSION_B,2,OK,HELLO");
    check("REQ,SESSION_A,6,PING", "ACK,SESSION_A,6,ERR,SESSION_MISMATCH");
    check("REQ,SESSION_B,3,STOP", "ACK,SESSION_B,3,OK,STOP");
    uart_v2_stop_only_reset(0xBB11223344556677ULL);
    check("REQ,SESSION_B,4,STOP", "ACK,SESSION_B,4,ERR,NO_SESSION");
    check("REQ,SESSION_B,2,HELLO,AA11223344556677", "ACK,SESSION_B,2,ERR,BOOT_MISMATCH");
    check("REQ,SESSION_C,1,DISCOVER", "ACK,SESSION_C,1,OK,BOOT_BB11223344556677");
    check("REQ,SESSION_C,2,HELLO,BB11223344556677", "ACK,SESSION_C,2,OK,HELLO");
    check("REQ,SESSION_C,3,PING", "ACK,SESSION_C,3,OK,PONG");
    assert(!uart_v2_stop_only("REQ,SESSION_C,01,STOP", out, sizeof(out), stop));
    assert(!uart_v2_stop_only("REQ,SESSION_C,4294967296,STOP", out, sizeof(out), stop));
    assert(!uart_v2_stop_only("REQ,BAD!,6,STOP", out, sizeof(out), stop));
    assert(!uart_v2_stop_only("REQ,SESSION_C,7", out, sizeof(out), stop));
    assert(stops == 24);
    return 0;
}
'''

def test_state_machine_c_contract():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d)
        (path / "harness.c").write_text(HARNESS)
        binary = path / "harness"
        subprocess.run(["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pedantic",
                        "-I", str(SDK), str(path / "harness.c"),
                        str(SDK / "uart_v2_stop_only.c"), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


def test_experimental_mode_blocks_all_legacy_frames():
    main = (SDK / "main.c").read_text()
    assert 'uart_v2_stop_only(line, response, sizeof(response), stop_motors)' in main
    assert 'ERR,V2_LEGACY_DISABLED' in main
    assert 'uart_v2_stop_only_reset(get_rand_64())' in main
    cm = (SDK / "CMakeLists.txt").read_text()
    assert "pico_rand" in cm
