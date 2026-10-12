#ifndef UART_PROTOCOL_V2_H
#define UART_PROTOCOL_V2_H

#include <stdbool.h>
#include <stdint.h>

#define UART_V2_SESSION_SIZE 33

typedef enum {
    UART_V2_UNBOUND = 0,
    UART_V2_BOUND_STOPPED,
    UART_V2_BOUND_ARMED,
    UART_V2_EMERGENCY_STOPPED,
    UART_V2_FAULT
} uart_v2_state_t;

typedef struct {
    uart_v2_state_t state;
    char session[UART_V2_SESSION_SIZE];
    uint32_t last_request_id;
} uart_v2_context_t;

typedef enum {
    UART_V2_ACCEPT = 0,
    UART_V2_REJECT_SESSION,
    UART_V2_REJECT_SEQUENCE,
    UART_V2_REJECT_DISARMED,
    UART_V2_REJECT_FAULT
} uart_v2_result_t;

void uart_v2_init(uart_v2_context_t *ctx);
void uart_v2_stop(uart_v2_context_t *ctx);
void uart_v2_emergency_stop(uart_v2_context_t *ctx);
bool uart_v2_reset_emergency_verified(uart_v2_context_t *ctx);
void uart_v2_fault(uart_v2_context_t *ctx);

bool uart_v2_bind_verified(
    uart_v2_context_t *ctx,
    const char *session
);

bool uart_v2_arm_verified(uart_v2_context_t *ctx);

uart_v2_result_t uart_v2_check_request(
    uart_v2_context_t *ctx,
    const char *session,
    uint32_t request_id,
    bool motion_command
);

#endif
