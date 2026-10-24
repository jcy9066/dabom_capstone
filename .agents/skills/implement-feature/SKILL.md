---
name: implement-feature
description: Implement or modify dabom_capstone code/config with scoped exploration, validation, review, and validated local commit.
---

# Implement Feature

## Workflow

1. Read root `AGENTS.md`.
2. Confirm requested scope and current `git status --short`.
3. Primary/Main Agent delegates targeted pre-change exploration to `code_explorer`.
4. Wait for that result, then modify only required files.
5. Preserve existing API/ROS/UART contracts unless the request requires a change.
6. Add/update tests for changed behavior.
7. Run relevant validation Skills:
   - `server/` → `validate-server`
   - `frontend/` → `validate-dashboard`
   - `server/app.py` → both
   - `navigation/` → `validate-navigation`
   - `raspberry/` → `validate-raspberry`
8. Run `review-change` last.
9. If required checks PASS, stage only this task's files and create one new commit.
10. For multiple independent tasks, repeat steps 3-9 per task.

## Context Budget

- Prefer RTK-supported commands when available.
- Prefer `status --short`, `diff --stat`, targeted diff/search, partial reads, and `pytest -q`.
- Do not print entire files/logs/diffs unless the defect requires it.
- Reuse existing reviewer results for the same current diff/scope.
- Sub-Agent returns must follow their line limits; do not ask for verbose reports.

## Restrictions

- No unrelated refactor/docs.
- No duplicate implementation.
- No real motor/GPIO/serial motion/ROS motion/firmware flash.
- No `push`, `pull`, `fetch`, `merge`, branch/history mutation.
- Do not commit when a required check FAILs.
