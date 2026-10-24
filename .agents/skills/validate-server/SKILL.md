---
name: validate-server
description: Validate changed FastAPI/auth/WebSocket/DB/server API paths in dabom_capstone.
---

# Validate Server

## Scope

Validate only affected `server/` paths, callers, API contracts, auth/CSRF/session,
WebSocket state, DB access, environment names, and related tests.

## Procedure

1. Reuse the current `code_explorer` result if it covers this post-change path;
   otherwise delegate a targeted server-path check.
2. Check changed route method/path/request/response against frontend callers.
3. Check affected auth/session/CSRF/WebSocket/DB error and cleanup behavior.
4. Run:
   `python .agents/skills/validate-server/scripts/validate_server.py`
5. Run affected tests first with quiet output:
   `rtk pytest -q <tests>` when RTK is available, otherwise `python -m pytest -q <tests>`.
6. If browser-visible behavior or `server/app.py` changed, run `validate-dashboard`.
7. Record only PASS/WARN/FAIL/SKIP and the failing evidence.

## Context Budget

- Do not dump full route tables, logs, source files, or full successful pytest output.
- Use targeted search/partial reads.
- Do not repeat a current-scope `code_explorer` review.

## Restrictions

No production DB mutation, Telegram send, robot motion, Git remote/history mutation,
or `test_reviewer` delegation here.
