#include "uart_v2_stop_only.h"
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static struct {
    char boot_nonce[17];
    char session[33];
    uint32_t last_id;
    bool bound;
} state;

void uart_v2_stop_only_reset(uint64_t nonce) {
    memset(&state, 0, sizeof(state));
    (void)snprintf(state.boot_nonce, sizeof(state.boot_nonce), "%016llX", (unsigned long long)nonce);
}

static bool valid_session(const char *s, size_t n) {
    if (n < 1 || n > 32) return false;
    for (size_t i = 0; i < n; ++i) {
        const char c = s[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '_' || c == '-')) return false;
    }
    return true;
}

static bool valid_id(const char *p, const char *end, uint32_t *result) {
    if (p == end || (size_t)(end - p) > 10 || *p == '0') return false;
    uint64_t n = 0;
    for (const char *it = p; it < end; ++it) {
        if (*it < '0' || *it > '9') return false;
        n = n * 10 + (unsigned)(*it - '0');
        if (n > UINT32_MAX) return false;
    }
    *result = (uint32_t)n;
    return true;
}

static bool ack(char *out, size_t cap, const char *session, size_t sn,
                const char *request, size_t rn, bool ok, const char *payload) {
    int n = snprintf(out, cap, "ACK,%.*s,%.*s,%s,%s", (int)sn, session,
                     (int)rn, request, ok ? "OK" : "ERR", payload);
    return n > 0 && (size_t)n < cap;
}

bool uart_v2_stop_only(const char *line, char *out, size_t cap,
                       void (*force_stop)(void)) {
    if (!force_stop) return false;
    /* Fail closed even before parsing untrusted input. */
    force_stop();
    if (!line || !out || cap == 0) return false;
    out[0] = '\0';
    if (strlen(line) > 127 || strncmp(line, "REQ,", 4) != 0) return false;
    const char *session = line + 4;
    const char *comma = strchr(session, ',');
    if (!comma || !valid_session(session, (size_t)(comma - session))) return false;
    const size_t sn = (size_t)(comma - session);
    const char *id = comma + 1;
    comma = strchr(id, ',');
    if (!comma) return false;
    const size_t rn = (size_t)(comma - id);
    uint32_t request_id;
    if (!valid_id(id, comma, &request_id)) return false;
    const char *cmd = comma + 1;

    if (strcmp(cmd, "DISCOVER") == 0) {
        if (request_id != 1) {
            return ack(out, cap, session, sn, id, rn, false, "DISCOVER_ID");
        }
        char payload[24];
        (void)snprintf(payload, sizeof(payload), "BOOT_%s", state.boot_nonce);
        return ack(out, cap, session, sn, id, rn, true, payload);
    }

    if (strncmp(cmd, "HELLO,", 6) == 0) {
        const char *token = cmd + 6;
        if (request_id != 2 || strlen(token) != 16 ||
            strcmp(token, state.boot_nonce) != 0) {
            return ack(out, cap, session, sn, id, rn, false, "BOOT_MISMATCH");
        }
        /* Reject replay of the same session's HELLO; a new Pi may
         * rebind with a fresh random session using this boot challenge.
         * This is replay freshness, NOT cryptographic authentication.
         */
        if (state.bound && strlen(state.session) == sn &&
            memcmp(state.session, session, sn) == 0) {
            return ack(out, cap, session, sn, id, rn, false, "STALE_ID");
        }
        memcpy(state.session, session, sn);
        state.session[sn] = '\0';
        state.last_id = request_id;
        state.bound = true;
        return ack(out, cap, session, sn, id, rn, true, "HELLO");
    }

    const bool is_stop = strcmp(cmd, "STOP") == 0;
    const bool is_ping = strcmp(cmd, "PING") == 0;
    if (!is_stop && !is_ping) {
        return ack(out, cap, session, sn, id, rn, false, "MOTION_DISABLED");
    }
    if (!state.bound) {
        return ack(out, cap, session, sn, id, rn, false, "NO_SESSION");
    }
    if (strlen(state.session) != sn || memcmp(state.session, session, sn) != 0) {
        return ack(out, cap, session, sn, id, rn, false, "SESSION_MISMATCH");
    }
    if (request_id <= state.last_id) {
        return ack(out, cap, session, sn, id, rn, false, "STALE_ID");
    }
    state.last_id = request_id;
    return ack(out, cap, session, sn, id, rn, true, is_stop ? "STOP" : "PONG");
}
