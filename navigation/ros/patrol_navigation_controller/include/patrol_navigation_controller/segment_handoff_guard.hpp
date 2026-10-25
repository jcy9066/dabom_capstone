// Copyright (c) 2026. Licensed under MIT.
// Pure C++ predicates shared by segmented Nav2 motion and regression tests.
#ifndef PATROL_NAVIGATION_CONTROLLER__SEGMENT_HANDOFF_GUARD_HPP_
#define PATROL_NAVIGATION_CONTROLLER__SEGMENT_HANDOFF_GUARD_HPP_

#include <algorithm>
#include <cmath>
#include <limits>

namespace patrol_navigation_controller
{

// Slew by the last ACCEPTED command, not raw wheel-derived angular odometry.
// Encoder derivative spikes otherwise propagate directly into the new Twist;
// the local 0.5s safety horizon correctly rejects those out-of-range speeds
// and Nav2 aborts a healthy in-place turn.
inline double boundedPivotAngularCommand(
  double previous_command, double desired,
  double angular_limit, double acceleration_limit, double controller_frequency)
{
  if (!std::isfinite(previous_command) || !std::isfinite(desired) ||
    !std::isfinite(angular_limit) || !std::isfinite(acceleration_limit) ||
    !std::isfinite(controller_frequency) || angular_limit <= 0.0 ||
    acceleration_limit <= 0.0 || controller_frequency <= 0.0 ||
    std::abs(previous_command) > angular_limit)
  {
    return std::numeric_limits<double>::quiet_NaN();
  }
  const double increment = acceleration_limit / controller_frequency;
  if (!std::isfinite(increment) || increment <= 0.0) {
    return std::numeric_limits<double>::quiet_NaN();
  }
  const double requested = std::clamp(desired, -angular_limit, angular_limit);
  return std::clamp(requested,
    std::max(-angular_limit, previous_command - increment),
    std::min(angular_limit, previous_command + increment));
}

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
