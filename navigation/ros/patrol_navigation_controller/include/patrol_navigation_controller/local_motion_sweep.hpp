// Copyright (c) 2026. Licensed under MIT.
// ROS-free short-horizon differential-drive projection for runtime collision
// checking before emitting nonzero Nav2 control commands.
#ifndef PATROL_NAVIGATION_CONTROLLER__LOCAL_MOTION_SWEEP_HPP_
#define PATROL_NAVIGATION_CONTROLLER__LOCAL_MOTION_SWEEP_HPP_

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <vector>

#include "patrol_navigation_controller/path_time_estimator.hpp"

namespace patrol_navigation_controller
{

// An invalid/overspeed input returns an empty path (never "clear").
// The 0.50s horizon is a safety check, not a substitute for the global
// planner's full swept path check or Pi's rear/blind-zone motion interlock.
inline std::vector<MotionPose> localMotionHorizon(
  const MotionPose & initial, double linear_mps, double angular_rps,
  double horizon_sec = 0.50)
{
  if (!std::isfinite(initial.x) || !std::isfinite(initial.y) ||
    !std::isfinite(initial.yaw) || !std::isfinite(linear_mps) ||
    !std::isfinite(angular_rps) || !std::isfinite(horizon_sec) ||
    horizon_sec <= 0.0 || horizon_sec > 1.0)
  {
    return {};
  }
  const double travel = std::abs(linear_mps) * horizon_sec;
  const double rotation = std::abs(angular_rps) * horizon_sec;
  // Reject implausibly large commanded velocities and excessive sampling.
  // The configured robot's maximum v = 0.212m/s and yaw = 1.05rad/s.
  if (travel > 0.25 || rotation > 1.20 ||
    (travel < 1e-10 && rotation < 1e-10))
  {
    return {};
  }
  const double intervals = std::max({
    1.0, std::ceil(travel / 0.02), std::ceil(rotation / 0.04)});
  if (!std::isfinite(intervals) || intervals > 128.0) {
    return {};
  }
  const auto count = static_cast<size_t>(intervals);
  std::vector<MotionPose> projected;
  projected.reserve(count + 1);
  for (size_t i = 0; i <= count; ++i) {
    const double time = horizon_sec * static_cast<double>(i) / count;
    const double new_yaw = initial.yaw + angular_rps * time;
    double dx = 0.0;
    double dy = 0.0;
    if (std::abs(angular_rps) < 1e-8) {
      dx = linear_mps * time * std::cos(initial.yaw);
      dy = linear_mps * time * std::sin(initial.yaw);
    } else {
      const double radius = linear_mps / angular_rps;
      dx = radius * (std::sin(new_yaw) - std::sin(initial.yaw));
      dy = -radius * (std::cos(new_yaw) - std::cos(initial.yaw));
    }
    MotionPose next{initial.x + dx, initial.y + dy, new_yaw};
    if (!std::isfinite(next.x) || !std::isfinite(next.yaw) ||
      !std::isfinite(next.y))
    {
      return {};
    }
    projected.push_back(next);
  }
  return projected;
}

}  // namespace patrol_navigation_controller

#endif  // PATROL_NAVIGATION_CONTROLLER__LOCAL_MOTION_SWEEP_HPP_
