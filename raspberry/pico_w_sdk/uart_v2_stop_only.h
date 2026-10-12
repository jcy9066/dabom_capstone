#ifndef UART_V2_STOP_ONLY_H
#define UART_V2_STOP_ONLY_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Experimental firmware, STOP/PING only. Never authorizes motion.
 * Only the Pico UART loop may call this single-instance state machine.
 * boot_nonce must be freshly randomized on every Pico firmware boot.
 */
void uart_v2_stop_only_reset(uint64_t boot_nonce);
bool uart_v2_stop_only(const char *line, char *reply, size_t capacity,
                       void (*force_stop)(void));
#endif
