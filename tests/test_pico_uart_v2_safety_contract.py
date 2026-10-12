"""RED tests for UART V2 ID bounds and emergency-stop policy."""

import subprocess
import tempfile
from pathlib import Path

import pytest

from raspberry.controllers.pico_uart_protocol_v2 import (
    ProtocolError,
    build_request,
)

ROOT = Path(__file__).resolve().parents[1]
PICO = ROOT / "raspberry" / "pico_w_sdk"


def test_request_id_maximum_accepted():
    request = build_request(
        "STOP",
        "test",
        session="TESTSESSION",
        request_id=4294967295,
    )
    assert request == "REQ,TESTSESSION,4294967295,STOP,test"


def test_request_id_above_uint32_rejected():
    with pytest.raises(ProtocolError):
        build_request(
            "STOP",
            "test",
            session="TESTSESSION",
            request_id=4294967296,
        )


C_SAFETY_TEST = r'''
#include <assert.h>
#include <stdio.h>

#include "uart_protocol_v2.h"

int main(void) {
    uart_v2_context_t ctx;

    uart_v2_init(&ctx);

    assert(uart_v2_bind_verified(&ctx, "TESTSESSION"));
    assert(uart_v2_arm_verified(&ctx));

    /* Normal STOP disarms, but does not create a fault. */
    uart_v2_stop(&ctx);
    assert(ctx.state == UART_V2_BOUND_STOPPED);

    /* Emergency STOP must latch a separate safety state. */
    uart_v2_emergency_stop(&ctx);
    assert(ctx.state == UART_V2_EMERGENCY_STOPPED);

    /* Ordinary arming cannot bypass emergency STOP. */
    assert(!uart_v2_arm_verified(&ctx));

    /* Ordinary STOP cannot clear emergency STOP. */
    uart_v2_stop(&ctx);
    assert(ctx.state == UART_V2_EMERGENCY_STOPPED);

    /* An explicit verified reset is required. */
    assert(uart_v2_reset_emergency_verified(&ctx));
    assert(ctx.state == UART_V2_BOUND_STOPPED);

    /* Reset must not automatically arm motion. */
    assert(
        uart_v2_check_request(&ctx, "TESTSESSION", 1, true)
        == UART_V2_REJECT_DISARMED
    );

    puts("UART_V2_EMERGENCY_CONTRACT_PASS");
    return 0;
}
'''


def test_host_compiled_emergency_stop_contract():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        source = tmp / "emergency_contract.c"
        binary = tmp / "emergency_contract"

        source.write_text(C_SAFETY_TEST, encoding="utf-8")

        subprocess.run(
            [
                "gcc",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-pedantic",
                "-I",
                str(PICO),
                str(source),
                str(PICO / "uart_protocol_v2.c"),
                "-o",
                str(binary),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

        result = subprocess.run(
            [str(binary)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert "UART_V2_EMERGENCY_CONTRACT_PASS" in result.stdout
