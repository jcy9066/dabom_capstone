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

// A pivot is entered only from a stop. During the same pivot, yaw motion
// must continue without a stop/start oscillation. Linear motion is always
// prohibited while the pivot is active.
inline bool safeToContinuePivot(
  bool pivot_started, double linear_x, double angular_z,
  double linear_stop_threshold, double angular_stop_threshold)
{
  if (!stationaryForSegmentTransition(
      0.0, 0.0, linear_stop_threshold, angular_stop_threshold) ||
    !std::isfinite(linear_x) || !std::isfinite(angular_z))
  {
    return false;
  }
  if (!pivot_started) {
    return stationaryForSegmentTransition(
      linear_x, angular_z, linear_stop_threshold, angular_stop_threshold);
  }
  return std::abs(linear_x) < linear_stop_threshold;
}

// This robot has a differential-drive base. Commands on the four other
// twist axes are unsupported even when finite. Reject them before treating
// (linear.x=0, angular.z=0) as a physical STOP.
inline bool validDifferentialDriveCommand(
  double linear_x, double linear_y, double linear_z,
  double angular_x, double angular_y, double angular_z)
{
  const double values[] = {
    linear_x, linear_y, linear_z, angular_x, angular_y, angular_z
  };
  for (const double value : values) {
    if (!std::isfinite(value)) {
      return false;
    }
  }
  constexpr double epsilon = 1e-9;
  return std::abs(linear_y) <= epsilon &&
    std::abs(linear_z) <= epsilon &&
    std::abs(angular_x) <= epsilon &&
    std::abs(angular_y) <= epsilon;
}

}  // namespace patrol_navigation_controller

#endif  // PATROL_NAVIGATION_CONTROLLER__SEGMENT_HANDOFF_GUARD_HPP_
