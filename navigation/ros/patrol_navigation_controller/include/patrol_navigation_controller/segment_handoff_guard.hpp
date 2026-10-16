// Copyright (c) 2026. Licensed under MIT.
// Pure C++ predicates shared by segmented Nav2 motion and regression tests.
#ifndef PATROL_NAVIGATION_CONTROLLER__SEGMENT_HANDOFF_GUARD_HPP_
#define PATROL_NAVIGATION_CONTROLLER__SEGMENT_HANDOFF_GUARD_HPP_

#include <cmath>

namespace patrol_navigation_controller
{

inline bool validUnitQuaternion(
  double x, double y, double z, double w, double norm_tolerance = 0.05)
{
  if (!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(z) ||
    !std::isfinite(w) || !std::isfinite(norm_tolerance) ||
    norm_tolerance < 0.0 || norm_tolerance >= 1.0)
  {
    return false;
  }
  const double norm_squared = x * x + y * y + z * z + w * w;
  return std::isfinite(norm_squared) &&
    std::abs(norm_squared - 1.0) <= norm_tolerance;
}

inline bool stationaryForSegmentTransition(
  double linear_x, double angular_z,
  double linear_stop_threshold, double angular_stop_threshold)
{
  if (!std::isfinite(linear_x) || !std::isfinite(angular_z) ||
    !std::isfinite(linear_stop_threshold) ||
    !std::isfinite(angular_stop_threshold) ||
    linear_stop_threshold <= 0.0 || angular_stop_threshold <= 0.0)
  {
    return false;
  }
  return std::abs(linear_x) < linear_stop_threshold &&
    std::abs(angular_z) < angular_stop_threshold;
}

}  // namespace patrol_navigation_controller

#endif  // PATROL_NAVIGATION_CONTROLLER__SEGMENT_HANDOFF_GUARD_HPP_
