---
name: review-change
description: Final review for scope, regression, tests, secrets, generated files, and readiness.
---

# Review Change

Run only after all required area validation is complete.

## Procedure

1. Inspect compact repository state:
   - prefer `rtk git status` or `git status --short`
   - prefer `rtk git diff`/`git diff --stat`, then targeted raw diff when needed
2. Confirm every changed/untracked file belongs to the request.
3. Run:
   `python .agents/skills/review-change/scripts/review_change.py`
4. Check secrets, `.env`, generated/build/log/backup files, duplicate/dead code,
   unintended API/protocol changes, missing tests, and validation claim drift.
5. Delegate the final diff/test-gap audit to `test_reviewer`; reuse only an exact matching result.
6. Wait for `test_reviewer` and decide READY / NOT READY.
7. If READY, return control to the Primary/Main Agent for scoped `git add` and new commit.

## Context Budget

- Do not dump the full diff or successful logs.
- Do not rerun already-successful identical checks without a concrete regression reason.
- Final review output should contain verdict, blocking issues, warnings, and skipped checks only.

## Restrictions

This Skill itself is read-only: no source/config edit, add/commit/push/merge/history mutation.
Do not declare READY if required validation or `test_reviewer` failed.
