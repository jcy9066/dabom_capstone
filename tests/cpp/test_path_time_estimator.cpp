// Pure standard C++ regression test: compile with -std=c++17, no ROS needed.
#include <cassert>
#include <cmath>
#include <limits>
#include <vector>

#include "patrol_navigation_controller/path_time_estimator.hpp"

using patrol_navigation_controller::MotionPose;
using patrol_navigation_controller::MotionTimeLimits;
using patrol_navigation_controller::estimatePathTime;

int main()
{
  MotionTimeLimits limits;
  const std::vector<MotionPose> short_forward{{0, 0, 0}, {0.20, 0, 0}};
  const std::vector<MotionPose> long_forward{{0, 0, 0}, {0.55, 0, 0}};
  auto short_score = estimatePathTime(short_forward, limits);
  auto long_score = estimatePathTime(long_forward, limits);
  assert(short_score.valid && long_score.valid);
  assert(short_score.seconds < long_score.seconds);
  assert(short_score.switches == 0);

  // A shorter XY distance is not necessarily the fastest trajectory.
  const std::vector<MotionPose> short_with_pivot{
    {0, 0, 0}, {0, 0, 3.141592653589793}, {-0.2, 0, 3.141592653589793}
  };
  const auto pivot_score = estimatePathTime(short_with_pivot, limits);
  assert(pivot_score.valid);
  assert(pivot_score.switches == 1);
  assert(pivot_score.seconds > long_score.seconds);

  // Reversing may be slow in an unknown rear sector; cost must include it.
  const std::vector<MotionPose> reverse{{0, 0, 0}, {-0.2, 0, 0}};
  const auto reverse_score = estimatePathTime(reverse, limits);
  assert(reverse_score.valid);
  assert(reverse_score.seconds > short_score.seconds);

  // An encoder-verified direction switch costs more than a continuous run.
  const std::vector<MotionPose> cusp{
    {0, 0, 0}, {0.15, 0, 0}, {0.05, 0, 0}
  };
  auto switched = estimatePathTime(cusp, limits);
  assert(switched.valid && switched.switches == 1);
  MotionTimeLimits no_stop = limits;
  no_stop.direction_switch_sec = 0.0;
  const auto instantaneous = estimatePathTime(cusp, no_stop);
  assert(switched.seconds > instantaneous.seconds + 0.49);

  // More restrictive motion/jerk limits must not improve the predicted time.
  MotionTimeLimits slower = limits;
  slower.forward_mps *= 0.50;
  slower.linear_jerk_mps3 *= 0.50;
  assert(estimatePathTime(long_forward, slower).seconds >
    long_score.seconds);

  // Rotation wraparound must use the short turn rather than a full revolution.
  const auto wrapped = estimatePathTime(
    {{0, 0, 3.13}, {0, 0, -3.13}}, limits);
  assert(wrapped.valid && wrapped.seconds < 1.0);

  // Never choose a corrupted/empty/unbounded candidate path.
  const double nan = std::numeric_limits<double>::quiet_NaN();
  assert(!estimatePathTime({{0, 0, 0}}, limits).valid);
  assert(!estimatePathTime({{0, 0, 0}, {nan, 0, 0}}, limits).valid);
  MotionTimeLimits broken = limits;
  broken.forward_mps = 0.0;
  assert(!estimatePathTime(short_forward, broken).valid);
  assert(!estimatePathTime({{0, 0, 0}, {0, 0, 0}}, limits).valid);
  return 0;
}
