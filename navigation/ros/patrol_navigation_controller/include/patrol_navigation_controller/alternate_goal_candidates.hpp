// Copyright (c) 2026. Licensed under MIT.
// Pure geometry: nearby Goal poses sorted by absolute distance to original.
#ifndef PATROL_NAVIGATION_CONTROLLER__ALTERNATE_GOAL_CANDIDATES_HPP_
#define PATROL_NAVIGATION_CONTROLLER__ALTERNATE_GOAL_CANDIDATES_HPP_

#include <algorithm>
#include <cmath>
#include <vector>
#include "patrol_navigation_controller/path_time_estimator.hpp"

namespace patrol_navigation_controller
{

// The path's *actual* terminal pose (not the requested Smac target) must
// remain within the Nav2 goal checker's approved XY and final-yaw limits.
// Nav2 may end a tolerance-based search short of its requested endpoint.
inline bool goalEndpointWithinTolerance(
  const MotionPose & endpoint, const MotionPose & original_goal,
  const double max_xy_m = 0.50, const double max_yaw_rad = 0.20)
{
  const double values[] = {
    endpoint.x, endpoint.y, endpoint.yaw,
    original_goal.x, original_goal.y, original_goal.yaw,
    max_xy_m, max_yaw_rad
  };
  for (const double value : values) {
    if (!std::isfinite(value)) {
      return false;
    }
  }
  if (max_xy_m <= 0.0 || max_yaw_rad <= 0.0 ||
    max_yaw_rad > 3.14159265358979323846)
  {
    return false;
  }
  return std::hypot(
    endpoint.x - original_goal.x, endpoint.y - original_goal.y) <= max_xy_m &&
    std::abs(shortestTurn(endpoint.yaw, original_goal.yaw)) <= max_yaw_rad;
}

// Keep a margin inside Nav2's 0.50m XY Goal tolerance for localization noise.
// This function proposes locations only; the planner MUST subsequently verify
// footprint collision clearance and a feasible, freshly collision-checked path.
inline std::vector<MotionPose> nearbyGoalCandidates(
  const MotionPose & goal, const double radius_m = 0.20,
  const double grid_step_m = 0.05)
{
  std::vector<MotionPose> proposed;
  if (!std::isfinite(goal.x) || !std::isfinite(goal.y) ||
    !std::isfinite(goal.yaw) || !std::isfinite(radius_m) ||
    !std::isfinite(grid_step_m) || radius_m <= 0.0 ||
    radius_m > 0.50 || grid_step_m <= 0.0 || grid_step_m > radius_m)
  {
    return proposed;
  }
  const int ticks = static_cast<int>(std::ceil(radius_m / grid_step_m));
  for (int x = -ticks; x <= ticks; ++x) {
    for (int y = -ticks; y <= ticks; ++y) {
      if (x == 0 && y == 0) {
        continue;
      }
      const double dx = static_cast<double>(x) * grid_step_m;
      const double dy = static_cast<double>(y) * grid_step_m;
      if (std::hypot(dx, dy) > radius_m + 1e-12) {
        continue;
      }
      proposed.push_back({goal.x + dx, goal.y + dy, goal.yaw});
    }
  }
  // The nearest reachable goal takes precedence over travel time. Equal
  // distances use deterministic XY ordering for reproducible navigation.
  std::sort(
    proposed.begin(), proposed.end(), [&goal](const MotionPose & a,
    const MotionPose & b) {
      const double da = std::hypot(a.x - goal.x, a.y - goal.y);
      const double db = std::hypot(b.x - goal.x, b.y - goal.y);
      if (std::abs(da - db) > 1e-10) {
        return da < db;
      }
      if (a.x != b.x) {
        return a.x < b.x;
      }
      return a.y < b.y;
    });
  return proposed;
}

}  // namespace patrol_navigation_controller

#endif  // PATROL_NAVIGATION_CONTROLLER__ALTERNATE_GOAL_CANDIDATES_HPP_
