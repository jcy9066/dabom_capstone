"""Host-compiled C tests for the isolated Pico UART V2 state machine."""

import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PICO = ROOT / "raspberry" / "pico_w_sdk"

C_TEST = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "uart_protocol_v2.h"

#define SESSION "A73F91C2"

static void test_initial_state(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    assert(ctx.state == UART_V2_UNBOUND);
    assert(ctx.last_request_id == 0);
    assert(ctx.session[0] == '\0');

    assert(
        uart_v2_check_request(&ctx, SESSION, 1, true)
        == UART_V2_REJECT_SESSION
    );

    assert(!uart_v2_arm_verified(&ctx));
}

static void test_bind_does_not_arm(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    assert(uart_v2_bind_verified(&ctx, SESSION));
    assert(ctx.state == UART_V2_BOUND_STOPPED);

    assert(
        uart_v2_check_request(&ctx, SESSION, 1, true)
        == UART_V2_REJECT_DISARMED
    );

    assert(ctx.last_request_id == 0);
}

static void test_arm_and_motion(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    assert(uart_v2_bind_verified(&ctx, SESSION));
    assert(uart_v2_arm_verified(&ctx));

    assert(ctx.state == UART_V2_BOUND_ARMED);

    assert(
        uart_v2_check_request(&ctx, SESSION, 1, true)
        == UART_V2_ACCEPT
    );

    assert(ctx.last_request_id == 1);
}

static void test_duplicate_and_stale_requests(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    assert(uart_v2_bind_verified(&ctx, SESSION));
    assert(uart_v2_arm_verified(&ctx));

    assert(
        uart_v2_check_request(&ctx, SESSION, 10, true)
        == UART_V2_ACCEPT
    );

    assert(
        uart_v2_check_request(&ctx, SESSION, 10, true)
        == UART_V2_REJECT_SEQUENCE
    );

    assert(
        uart_v2_check_request(&ctx, SESSION, 9, true)
        == UART_V2_REJECT_SEQUENCE
    );

    assert(ctx.last_request_id == 10);
}

static void test_wrong_session(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    assert(uart_v2_bind_verified(&ctx, SESSION));
    assert(uart_v2_arm_verified(&ctx));

    assert(
        uart_v2_check_request(&ctx, "OLD_SESSION", 1, true)
        == UART_V2_REJECT_SESSION
    );

    assert(ctx.last_request_id == 0);
}

static void test_stop_disarms(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    assert(uart_v2_bind_verified(&ctx, SESSION));
    assert(uart_v2_arm_verified(&ctx));

    uart_v2_stop(&ctx);

    assert(ctx.state == UART_V2_BOUND_STOPPED);

    assert(
        uart_v2_check_request(&ctx, SESSION, 1, true)
        == UART_V2_REJECT_DISARMED
    );
}

static void test_fault_is_terminal_without_reset(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    assert(uart_v2_bind_verified(&ctx, SESSION));
    assert(uart_v2_arm_verified(&ctx));

    uart_v2_fault(&ctx);

    assert(ctx.state == UART_V2_FAULT);

    assert(
        uart_v2_check_request(&ctx, SESSION, 1, true)
        == UART_V2_REJECT_FAULT
    );

    assert(
        uart_v2_check_request(&ctx, SESSION, 2, false)
        == UART_V2_REJECT_FAULT
    );

    assert(!uart_v2_arm_verified(&ctx));
    assert(!uart_v2_bind_verified(&ctx, "NEW_SESSION"));

    uart_v2_stop(&ctx);
    assert(ctx.state == UART_V2_FAULT);
}

static void test_invalid_sessions(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    assert(!uart_v2_bind_verified(&ctx, ""));
    assert(!uart_v2_bind_verified(&ctx, "BAD,SESSION"));
    assert(!uart_v2_bind_verified(&ctx, NULL));

    assert(ctx.state == UART_V2_UNBOUND);
}

static void test_rebind_requires_reset(void) {
    uart_v2_context_t ctx;
    uart_v2_init(&ctx);

    assert(uart_v2_bind_verified(&ctx, SESSION));

    assert(!uart_v2_bind_verified(&ctx, "NEW_SESSION"));
    assert(strcmp(ctx.session, SESSION) == 0);
}

int main(void) {
    test_initial_state();
    test_bind_does_not_arm();
    test_arm_and_motion();
    test_duplicate_and_stale_requests();
    test_wrong_session();
    test_stop_disarms();
    test_fault_is_terminal_without_reset();
    test_invalid_sessions();
    test_rebind_requires_reset();

    puts("UART_V2_C_STATE_MACHINE_PASS");
    return 0;
}
'''


def test_host_compiled_state_machine():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        source = tmp / "state_machine_test.c"
        binary = tmp / "state_machine_test"

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

        assert "UART_V2_C_STATE_MACHINE_PASS" in result.stdout
