// Copyright (c) 2026. Licensed under MIT.
// ROS-independent interpolation for collision checking a complete 2D path.
#ifndef PATROL_NAVIGATION_CONTROLLER__PATH_SWEEP_HPP_
#define PATROL_NAVIGATION_CONTROLLER__PATH_SWEEP_HPP_

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <limits>
#include <vector>

#include "patrol_navigation_controller/path_time_estimator.hpp"

namespace patrol_navigation_controller
{

// Sampling limits are stricter than the 0.05m global costmap resolution.
// Turning in place is sampled just like a translation, so the rectangular
// footprint cannot rotate through an obstacle between path poses.
struct PathSweepLimits
{
  double max_translation_step_m = 0.025;
  double max_rotation_step_rad = 0.05;
  size_t max_samples = 60000;
};

template<typename CollisionFree>
bool validateSweptPath(
  const std::vector<MotionPose> & path,
  CollisionFree && collision_free,
  const PathSweepLimits & limits = {})
{
  if (
    path.size() < 2 || limits.max_samples < 2 ||
    !std::isfinite(limits.max_translation_step_m) ||
    !std::isfinite(limits.max_rotation_step_rad) ||
    limits.max_translation_step_m <= 0.0 ||
    limits.max_rotation_step_rad <= 0.0
  ) {
    return false;
  }

  size_t checked = 0;
  for (size_t edge = 0; edge + 1 < path.size(); ++edge) {
    const auto & start = path[edge];
    const auto & end = path[edge + 1];
    if (
      !std::isfinite(start.x) || !std::isfinite(start.y) ||
      !std::isfinite(start.yaw) || !std::isfinite(end.x) ||
      !std::isfinite(end.y) || !std::isfinite(end.yaw)
    ) {
      return false;
    }
    const double length = std::hypot(end.x - start.x, end.y - start.y);
    const double rotation = shortestTurn(start.yaw, end.yaw);
    if (!std::isfinite(length) || !std::isfinite(rotation)) {
      return false;
    }

    const double raw_intervals = std::max({
      1.0, std::ceil(length / limits.max_translation_step_m),
      std::ceil(std::abs(rotation) / limits.max_rotation_step_rad)
    });
    // Check size before casting a potentially unbounded number to size_t.
    const size_t remaining = limits.max_samples - checked;
    if (!std::isfinite(raw_intervals) ||
      raw_intervals > static_cast<double>(remaining))
    {
      return false;
    }
    const size_t intervals = static_cast<size_t>(raw_intervals);
    // Include starting endpoint once and every subsequent sample, including
    // the final endpoint. Check all poses, never only the returned waypoints.
    for (size_t step = (edge == 0 ? 0 : 1); step <= intervals; ++step) {
      if (++checked > limits.max_samples) {
        return false;
      }
      const double t = static_cast<double>(step) / intervals;
      const MotionPose sample{
        start.x + t * (end.x - start.x),
        start.y + t * (end.y - start.y),
        start.yaw + t * rotation
      };
      if (!collision_free(sample)) {
        return false;
      }
    }
  }
  return true;
}

}  // namespace patrol_navigation_controller

#endif  // PATROL_NAVIGATION_CONTROLLER__PATH_SWEEP_HPP_
