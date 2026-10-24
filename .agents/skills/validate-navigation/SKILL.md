---
name: validate-navigation
description: Validate changed ROS2 Humble/SLAM/localization/Nav2/TF/navigation paths.
---

# Validate Navigation

Read `references/ros_interfaces.md`.

## Procedure

1. Delegate current ROS diff/interface review to `ros_reviewer`; reuse matching result.
2. Verify changed launch ↔ executable registration, installed data, interface/frame names,
   mode separation, QoS/timestamps, and dry-run safety.
3. Run:
   `python .agents/skills/validate-navigation/scripts/validate_navigation.py`
4. When ROS dependencies exist, build only the affected package:
   `cd navigation/ros && colcon build --packages-select patrol_navigation`
5. Run only affected navigation tests first, in quiet/RTK mode when available.
6. Separate static/build/mock results from actual ROS runtime results.

## Context Budget

Use targeted search/partial reads. Do not return complete YAML, launch files, build logs,
or successful test logs. Return only failures, warnings, evidence, and skipped runtime checks.

## Restrictions

No `/cmd_vel`, real motor output, ROS process mutation, map write, Git remote/history mutation,
or `test_reviewer` delegation here.
