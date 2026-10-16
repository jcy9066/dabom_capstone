// ROS-free C++17 test of the exact local motion horizon geometry.
#include <cassert>
#include <cmath>
#include <limits>

#include "patrol_navigation_controller/local_motion_sweep.hpp"
#include "patrol_navigation_controller/path_sweep.hpp"

using patrol_navigation_controller::localMotionHorizon;
using patrol_navigation_controller::validateSweptPath;
using patrol_navigation_controller::MotionPose;

int main()
{
  const MotionPose start{1.0, 2.0, 0.0};
  const auto ahead = localMotionHorizon(start, 0.20, 0.0);
  assert(ahead.size() >= 6);
  assert(std::abs(ahead.front().x - 1.0) < 1e-12);
  assert(std::abs(ahead.back().x - 1.10) < 1e-10);
  assert(std::abs(ahead.back().y - 2.0) < 1e-12);
  const auto reverse = localMotionHorizon(start, -0.20, 0.0);
  assert(reverse.size() >= 6);
  assert(std::abs(reverse.back().x - 0.90) < 1e-10);
  const auto spin = localMotionHorizon(start, 0.0, 1.0);
  assert(spin.size() > 10);
  assert(std::abs(spin.back().x - 1.0) < 1e-12);
  assert(std::abs(spin.back().yaw - 0.50) < 1e-12);
  const auto curve = localMotionHorizon(start, 0.20, 1.0);
  assert(curve.size() > 10);
  assert(std::abs(curve.back().x - (1.0 + 0.20 * std::sin(0.50))) < 1e-10);
  assert(std::abs(curve.back().y - (2.0 + 0.20 * (1 - std::cos(0.50)))) < 1e-10);
  // Dense collision sampling checks obstacles in between predicted poses.
  assert(!validateSweptPath(ahead, [](const MotionPose & p) {
    return std::abs(p.x - 1.05) > 0.002;
  }));
  // Default sweep sampling at <=0.05 rad checks the projected poses.
  // This obstacle's angular footprint includes yaw=0.1923.. which is one
  // of the emitted samples; the previous ±0.002 threshold missed them all.
  assert(!validateSweptPath(spin, [](const MotionPose & p) {
    return std::abs(p.yaw - 0.20) > 0.02;
  }));
  // For a narrower obstacle, explicitly request a denser angular step
  // so the swept check samples *between* the 0.03846-rad input poses.
  assert(!validateSweptPath(spin, [](const MotionPose & p) {
    return std::abs(p.yaw - 0.20) > 0.002;
  }, patrol_navigation_controller::PathSweepLimits{0.025, 0.01, 60000}));
  assert(validateSweptPath(curve, [](const MotionPose &) { return true; }));
  const double nan = std::numeric_limits<double>::quiet_NaN();
  const double inf = std::numeric_limits<double>::infinity();
  assert(localMotionHorizon(start, 0.0, 0.0).empty());
  assert(localMotionHorizon(start, nan, 0.0).empty());
  assert(localMotionHorizon(start, 0.1, inf).empty());
  assert(localMotionHorizon({nan, 0.0, 0.0}, 0.1, 0.0).empty());
  assert(localMotionHorizon(start, 0.2, 0.0, -1.0).empty());
  assert(localMotionHorizon(start, 0.2, 0.0, inf).empty());
  assert(localMotionHorizon(start, 1.0, 0.0).empty());
  assert(localMotionHorizon(start, 0.0, 4.0).empty());
  return 0;
}
