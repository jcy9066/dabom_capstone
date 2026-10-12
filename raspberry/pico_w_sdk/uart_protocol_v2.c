#include "uart_protocol_v2.h"

/* Host-only state-machine prototype. Real experimental firmware uses
 * uart_v2_stop_only.c and cannot arm motion. Fail accidental inclusion. */
#ifndef DABOM_V2_HOST_MODEL_ONLY
#error "uart_protocol_v2.c is a HOST-ONLY model, not Pico firmware"
#endif

#include <ctype.h>
#include <stddef.h>
#include <string.h>

static bool valid_session(const char *session) {
    if (session == NULL) {
        return false;
    }

    size_t length = 0;

    while (length < UART_V2_SESSION_SIZE && session[length] != '\0') {
        const unsigned char c = (unsigned char)session[length];

        if (!(
            (c >= 'A' && c <= 'Z') ||
            (c >= 'a' && c <= 'z') ||
            (c >= '0' && c <= '9') ||
            c == '_' ||
            c == '-'
        )) {
            return false;
        }

        length++;
    }

    return length > 0 && length < UART_V2_SESSION_SIZE;
}

void uart_v2_init(uart_v2_context_t *ctx) {
    if (ctx == NULL) {
        return;
    }

    memset(ctx, 0, sizeof(*ctx));
    ctx->state = UART_V2_UNBOUND;
}

void uart_v2_stop(uart_v2_context_t *ctx) {
    if (ctx == NULL) {
        return;
    }

    if (ctx->state == UART_V2_BOUND_ARMED) {
        ctx->state = UART_V2_BOUND_STOPPED;
    }
}

void uart_v2_emergency_stop(uart_v2_context_t *ctx) {
    if (ctx == NULL || ctx->state == UART_V2_FAULT) {
        return;
    }

    ctx->state = UART_V2_EMERGENCY_STOPPED;
}

bool uart_v2_reset_emergency_verified(uart_v2_context_t *ctx) {
    if (
        ctx == NULL ||
        ctx->state != UART_V2_EMERGENCY_STOPPED
    ) {
        return false;
    }

    ctx->state = UART_V2_BOUND_STOPPED;
    return true;
}

void uart_v2_fault(uart_v2_context_t *ctx) {
    if (ctx == NULL) {
        return;
    }

    ctx->state = UART_V2_FAULT;
}

bool uart_v2_bind_verified(
    uart_v2_context_t *ctx,
    const char *session
) {
    if (
        ctx == NULL ||
        ctx->state != UART_V2_UNBOUND ||
        !valid_session(session)
    ) {
        return false;
    }

    strcpy(ctx->session, session);
    ctx->last_request_id = 0;
    ctx->state = UART_V2_BOUND_STOPPED;
    return true;
}

bool uart_v2_arm_verified(uart_v2_context_t *ctx) {
    if (
        ctx == NULL ||
        ctx->state != UART_V2_BOUND_STOPPED
    ) {
        return false;
    }

    ctx->state = UART_V2_BOUND_ARMED;
    return true;
}

uart_v2_result_t uart_v2_check_request(
    uart_v2_context_t *ctx,
    const char *session,
    uint32_t request_id,
    bool motion_command
) {
    if (ctx == NULL || ctx->state == UART_V2_FAULT) {
        return UART_V2_REJECT_FAULT;
    }

    if (ctx->state == UART_V2_EMERGENCY_STOPPED) {
        return UART_V2_REJECT_DISARMED;
    }

    if (
        ctx->state == UART_V2_UNBOUND ||
        !valid_session(session) ||
        strcmp(ctx->session, session) != 0
    ) {
        return UART_V2_REJECT_SESSION;
    }

    if (
        request_id == 0 ||
        request_id <= ctx->last_request_id
    ) {
        return UART_V2_REJECT_SEQUENCE;
    }

    if (
        motion_command &&
        ctx->state != UART_V2_BOUND_ARMED
    ) {
        return UART_V2_REJECT_DISARMED;
    }

    ctx->last_request_id = request_id;
    return UART_V2_ACCEPT;
}
