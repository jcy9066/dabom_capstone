# PR #80: UART V2 host-only review follow-up (2026-10-13)

**No real UART opened, no firmware flashed, no motor power enabled, no E-STOP reset.**

## UART ownership

- Both legacy V1 MotorController and opt-in V2StopOnlyConnection now request Linux pyserial `exclusive=True`.
- Linux advisory locks only exclude *cooperating* exclusive owners. An old service that did not set exclusive mode can still interfere. Before an approved hardware test, shut down V1 service and verify it does not own the UART. Never run V1 and V2 readers together.
- This change does not enable V2 in production, nor authorize motion.

## Fault cleanup

- V2 errors mark the link invalid, cancel pending ACK, signal the reader to stop and close the UART descriptor. Reconnecting is always explicit with a fresh session.

## Prototype vs actual firmware

- `uart_protocol_v2.c/.h` is an **unused host-only prototype** containing a simulated arming/E-STOP state machine. It is **not linked into Pico firmware**, and compilation is now explicitly guarded by `DABOM_V2_HOST_MODEL_ONLY`.
- Actual experimental firmware compiles `main.c` and `uart_v2_stop_only.c`. The latter invokes a hard-stop callback for received requests and rejects MOVE/DRIVE; additional host C tests exercise this real compiled parser.
- Host tests do **not** prove physical STOP/E-STOP effectiveness or latency.

## Merge/deployment gates

Keep PR #80 Draft, based on Draft PR #79. No firmware upload, real Pi↔Pico link or motor tests until separate safety approval, service ownership handoff, and physical STOP/E-STOP validation.
