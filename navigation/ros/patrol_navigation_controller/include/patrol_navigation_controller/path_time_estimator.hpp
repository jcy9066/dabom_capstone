// Copyright (c) 2026. Licensed under MIT.
// ROS-independent scoring of State Lattice path candidates.
#ifndef PATROL_NAVIGATION_CONTROLLER__PATH_TIME_ESTIMATOR_HPP_
#define PATROL_NAVIGATION_CONTROLLER__PATH_TIME_ESTIMATOR_HPP_

#include <algorithm>
#include <cstddef>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <vector>

namespace patrol_navigation_controller
{

struct MotionPose
{
  double x;
  double y;
  double yaw;
};

struct MotionTimeLimits
{
  double forward_mps = 0.212;
  // Conservative reverse traversal assumption: the rear may be unobserved.
  // A real observation may permit faster reverse, but cannot weaken safety.
  double reverse_mps = 0.212 * 0.30;
  double angular_rps = 1.05;
  double linear_accel_mps2 = 0.50;
  double angular_accel_rps2 = 1.0;
  double linear_jerk_mps3 = 2.0;
  double direction_switch_sec = 0.50;
};

struct MotionTimeScore
{
  double seconds = std::numeric_limits<double>::infinity();
  unsigned int switches = 0;
  bool valid = false;
};

inline double shortestTurn(double a, double b)
{
  return std::atan2(std::sin(b - a), std::cos(b - a));
}

inline double trapezoidalTime(double length, double speed, double accel)
{
  if (length <= 0.0) {
    return 0.0;
  }
  const double critical = speed * speed / accel;
  if (length <= critical) {
    return 2.0 * std::sqrt(length / accel);
  }
  return length / speed + speed / accel;
}

inline MotionTimeScore estimatePathTime(
  const std::vector<MotionPose> & poses, const MotionTimeLimits & limits)
{
  MotionTimeScore result;
  const double values[] = {
    limits.forward_mps, limits.reverse_mps, limits.angular_rps,
    limits.linear_accel_mps2, limits.angular_accel_rps2, limits.linear_jerk_mps3
  };
  for (const auto value : values) {
    if (!std::isfinite(value) || value <= 0.0) {
      return result;
    }
  }
  if (!std::isfinite(limits.direction_switch_sec) ||
    limits.direction_switch_sec < 0.0 || poses.size() < 2)
  {
    return result;
  }
  for (const auto & pose : poses) {
    if (!std::isfinite(pose.x) || !std::isfinite(pose.y) ||
      !std::isfinite(pose.yaw))
    {
      return result;
    }
  }

  // Three discrete actuation classes and two pivot directions.
  // Each transition is a physically stopped handoff; Pi verifies a fresh
  // encoder 0.5s stable-stop window before allowing the next mode.
  constexpr int kForward = 1;
  constexpr int kReverse = 2;
  constexpr int kPivotLeft = 3;
  constexpr int kPivotRight = 4;
  int current = 0;
  double run_distance = 0.0;
  double run_rotation = 0.0;
  double seconds = 0.0;
  unsigned int switches = 0;

  const auto flush = [&]() {
      if (current == 0) {
        return;
      }
      const bool driving = current == kForward || current == kReverse;
      if (driving) {
        const double speed = current == kReverse ?
          limits.reverse_mps : limits.forward_mps;
        const double translational = trapezoidalTime(
          run_distance, speed, limits.linear_accel_mps2);
        const double rotational = trapezoidalTime(
          run_rotation, limits.angular_rps, limits.angular_accel_rps2);
        // Translation and yaw on a curved path can occur together.
        const double core = std::max(translational, rotational);
        seconds += core;
        if (run_distance > 0.0) {
          seconds += std::min(
            limits.linear_accel_mps2 / limits.linear_jerk_mps3, core / 4.0);
        }
      } else {
        seconds += trapezoidalTime(
          run_rotation, limits.angular_rps, limits.angular_accel_rps2);
      }
    };

  for (size_t i = 1; i < poses.size(); ++i) {
    const auto & prev = poses[i - 1];
    const auto & next = poses[i];
    const double dx = next.x - prev.x;
    const double dy = next.y - prev.y;
    const double length = std::hypot(dx, dy);
    const double turn = shortestTurn(prev.yaw, next.yaw);
    if (!std::isfinite(length) || !std::isfinite(turn)) {
      return result;
    }
    int mode = 0;
    if (length < 0.003) {
      if (turn > 0.02) {
        mode = kPivotLeft;
      } else if (turn < -0.02) {
        mode = kPivotRight;
      }
    } else {
      const double projection = dx * std::cos(prev.yaw) +
        dy * std::sin(prev.yaw);
      mode = projection < -0.003 ? kReverse : kForward;
    }
    if (mode == 0) {
      continue;  // duplicate pose
    }
    if (current != mode) {
      if (current != 0) {
        flush();
        seconds += limits.direction_switch_sec;
        ++switches;
      }
      current = mode;
      run_distance = 0.0;
      run_rotation = 0.0;
    }
    run_distance += length;
    run_rotation += std::abs(turn);
  }
  flush();
  if (current == 0 || !std::isfinite(seconds)) {
    return result;
  }
  result.seconds = seconds;
  result.switches = switches;
  result.valid = true;
  return result;
}

}  // namespace patrol_navigation_controller

#endif  // PATROL_NAVIGATION_CONTROLLER__PATH_TIME_ESTIMATOR_HPP_
