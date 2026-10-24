---
name: validate-dashboard
description: Validate changed browser-visible dashboard behavior using Playwright via dashboard_reviewer.
---

# Validate Dashboard

Read:
- `references/dashboard_flows.md`
- `references/expected_states.md`

## Procedure

1. Primary/Main Agent delegates the changed browser flow to `dashboard_reviewer`.
2. Reuse an existing reviewer result when diff, URL, and scope match.
3. Provide only changed files, expected behavior, safe URL, and available test state.
4. Reviewer executes actual Playwright validation and checks:
   - affected login/session/CSRF/redirect behavior
   - changed UI state/assets
   - console errors
   - unexpected failed requests/4xx/5xx
5. Parent checks coverage and carries blocking failures to `review-change`.

## Context Budget

- Validate affected flows, not the full dashboard by default.
- Screenshot/trace only when needed as defect evidence.
- Do not paste raw console/network/DOM snapshots into the parent result.
- Reviewer result is limited by `.codex/agents/dashboard-review.toml`.

## Prohibited

No D-Pad/keyboard robot motion, Telegram, warning broadcast, ROS mutation,
map/user/production DB mutation, or source/config edits during review.
