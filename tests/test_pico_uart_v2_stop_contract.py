"""Host-only STOP and fault-state contract tests."""

import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PICO = ROOT / "raspberry" / "pico_w_sdk"

C_TEST = r'''
#include <assert.h>
#include <stdio.h>

#include "uart_protocol_v2.h"

#define SESSION "TESTSESSION"

static void test_stop_is_idempotent(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    uart_v2_stop(&ctx);
    assert(ctx.state == UART_V2_UNBOUND);

    assert(uart_v2_bind_verified(&ctx, SESSION));
    uart_v2_stop(&ctx);
    assert(ctx.state == UART_V2_BOUND_STOPPED);

    assert(uart_v2_arm_verified(&ctx));
    uart_v2_stop(&ctx);
    uart_v2_stop(&ctx);
    assert(ctx.state == UART_V2_BOUND_STOPPED);
}

static void test_stop_preserves_request_sequence(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    assert(uart_v2_bind_verified(&ctx, SESSION));
    assert(uart_v2_arm_verified(&ctx));

    assert(
        uart_v2_check_request(&ctx, SESSION, 50, true)
        == UART_V2_ACCEPT
    );

    uart_v2_stop(&ctx);

    assert(ctx.last_request_id == 50);

    assert(
        uart_v2_check_request(&ctx, SESSION, 49, false)
        == UART_V2_REJECT_SEQUENCE
    );
}

static void test_emergency_stop_is_idempotent(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    assert(uart_v2_bind_verified(&ctx, SESSION));

    uart_v2_emergency_stop(&ctx);
    uart_v2_emergency_stop(&ctx);

    assert(ctx.state == UART_V2_EMERGENCY_STOPPED);
    assert(!uart_v2_arm_verified(&ctx));

    uart_v2_stop(&ctx);
    assert(ctx.state == UART_V2_EMERGENCY_STOPPED);
}

static void test_fault_remains_terminal(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    assert(uart_v2_bind_verified(&ctx, SESSION));
    uart_v2_fault(&ctx);

    uart_v2_stop(&ctx);
    uart_v2_emergency_stop(&ctx);

    assert(ctx.state == UART_V2_FAULT);
    assert(!uart_v2_arm_verified(&ctx));
    assert(!uart_v2_reset_emergency_verified(&ctx));
}

static void test_emergency_stop_does_not_advance_sequence(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    assert(uart_v2_bind_verified(&ctx, SESSION));
    assert(uart_v2_arm_verified(&ctx));

    assert(
        uart_v2_check_request(&ctx, SESSION, 20, true)
        == UART_V2_ACCEPT
    );

    uart_v2_emergency_stop(&ctx);

    assert(ctx.last_request_id == 20);
}

int main(void) {
    test_stop_is_idempotent();
    test_stop_preserves_request_sequence();
    test_emergency_stop_is_idempotent();
    test_fault_remains_terminal();
    test_emergency_stop_does_not_advance_sequence();

    puts("UART_V2_STOP_CONTRACT_PASS");
    return 0;
}
'''


def test_host_compiled_stop_contract():
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)

        source = directory / "stop_contract.c"
        binary = directory / "stop_contract"

        source.write_text(C_TEST, encoding="utf-8")

        subprocess.run(
            [
                "gcc",
                "-std=c11",
                "-DDABOM_V2_HOST_MODEL_ONLY=1",
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

        assert "UART_V2_STOP_CONTRACT_PASS" in result.stdout
