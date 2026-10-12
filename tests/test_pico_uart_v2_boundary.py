"""Host-only boundary tests for Pico UART V2 safety state."""

import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PICO = ROOT / "raspberry" / "pico_w_sdk"

C_TEST = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "uart_protocol_v2.h"

#define SESSION "TESTSESSION"

static void test_emergency_latch(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    assert(uart_v2_bind_verified(&ctx, SESSION));
    assert(uart_v2_arm_verified(&ctx));

    uart_v2_emergency_stop(&ctx);

    assert(ctx.state == UART_V2_EMERGENCY_STOPPED);
    assert(!uart_v2_arm_verified(&ctx));
    assert(!uart_v2_bind_verified(&ctx, "NEWSESSION"));

    assert(
        uart_v2_check_request(&ctx, SESSION, 1, true)
        == UART_V2_REJECT_DISARMED
    );

    uart_v2_stop(&ctx);
    assert(ctx.state == UART_V2_EMERGENCY_STOPPED);

    assert(uart_v2_reset_emergency_verified(&ctx));
    assert(ctx.state == UART_V2_BOUND_STOPPED);

    assert(
        uart_v2_check_request(&ctx, SESSION, 1, true)
        == UART_V2_REJECT_DISARMED
    );
}

static void test_uint32_max_boundary(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    assert(uart_v2_bind_verified(&ctx, SESSION));
    assert(uart_v2_arm_verified(&ctx));

    assert(
        uart_v2_check_request(
            &ctx, SESSION, UINT32_MAX, true
        ) == UART_V2_ACCEPT
    );

    assert(ctx.last_request_id == UINT32_MAX);

    assert(
        uart_v2_check_request(&ctx, SESSION, 1, true)
        == UART_V2_REJECT_SEQUENCE
    );

    assert(
        uart_v2_check_request(
            &ctx, SESSION, UINT32_MAX, true
        ) == UART_V2_REJECT_SEQUENCE
    );
}

static void test_wrong_session_does_not_consume_id(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    assert(uart_v2_bind_verified(&ctx, SESSION));
    assert(uart_v2_arm_verified(&ctx));

    assert(
        uart_v2_check_request(
            &ctx, "WRONGSESSION", 1, true
        ) == UART_V2_REJECT_SESSION
    );

    assert(ctx.last_request_id == 0);

    assert(
        uart_v2_check_request(&ctx, SESSION, 1, true)
        == UART_V2_ACCEPT
    );
}

static void test_fault_cannot_be_reset_as_emergency(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    assert(uart_v2_bind_verified(&ctx, SESSION));

    uart_v2_fault(&ctx);

    assert(ctx.state == UART_V2_FAULT);
    assert(!uart_v2_reset_emergency_verified(&ctx));

    uart_v2_emergency_stop(&ctx);
    assert(ctx.state == UART_V2_FAULT);
}

int main(void) {
    test_emergency_latch();
    test_uint32_max_boundary();
    test_wrong_session_does_not_consume_id();
    test_fault_cannot_be_reset_as_emergency();

    puts("UART_V2_BOUNDARY_PASS");
    return 0;
}
'''


def test_host_compiled_v2_boundaries():
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)

        source = directory / "boundary_test.c"
        binary = directory / "boundary_test"

        source.write_text(C_TEST, encoding="utf-8")

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

        assert "UART_V2_BOUNDARY_PASS" in result.stdout
