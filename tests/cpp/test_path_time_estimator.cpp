// Pure standard C++ regression test: compile with -std=c++17, no ROS needed.
#include <cassert>
#include <cmath>
#include <limits>
#include <vector>

#include "patrol_navigation_controller/path_time_estimator.hpp"
#include "patrol_navigation_controller/path_sweep.hpp"
#include "patrol_navigation_controller/alternate_goal_candidates.hpp"

using patrol_navigation_controller::MotionPose;
using patrol_navigation_controller::MotionTimeLimits;
using patrol_navigation_controller::estimatePathTime;
using patrol_navigation_controller::PathSweepLimits;
using patrol_navigation_controller::validateSweptPath;

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
  // Angular jerk is derived from the wheel-level jerk limit and the
  // actual track width. A stricter motor jerk cannot improve pivot time.
  MotionTimeLimits high_jerk = limits;
  high_jerk.linear_jerk_mps3 = 4.0;
  MotionTimeLimits low_jerk = limits;
  low_jerk.linear_jerk_mps3 = 0.5;
  const std::vector<MotionPose> pure_pivot{
    {0, 0, 0}, {0, 0, 1.5707963267948966}
  };
  const auto fast_turn = estimatePathTime(pure_pivot, high_jerk);
  const auto slow_turn = estimatePathTime(pure_pivot, low_jerk);
  assert(fast_turn.valid && slow_turn.valid);
  assert(slow_turn.seconds > fast_turn.seconds);

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

  // Wider wheel separation increases the required wheel travel in curves;
  // this cannot produce a faster feasible traverse at the same wheel cap.
  const std::vector<MotionPose> curved{
    {0, 0, 0}, {0.20, 0.04, 0.50}
  };
  MotionTimeLimits narrow = limits;
  narrow.wheel_track_m = 0.12;
  MotionTimeLimits wide = limits;
  wide.wheel_track_m = 0.36;
  assert(estimatePathTime(curved, wide).seconds >
    estimatePathTime(curved, narrow).seconds);
  MotionTimeLimits invalid_track = limits;
  invalid_track.wheel_track_m = 0.0;
  assert(!estimatePathTime(curved, invalid_track).valid);

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
  // 0.10m edge with a blocked midpoint but clear endpoints cannot pass.
  const std::vector<MotionPose> corridor{{0.0, 0.0, 0.0}, {0.10, 0.0, 0.0}};
  size_t samples = 0;
  assert(validateSweptPath(corridor, [&](const MotionPose &) {
    ++samples;
    return true;
  }));
  assert(samples == 5);
  assert(!validateSweptPath(corridor, [](const MotionPose & pose) {
    return std::abs(pose.x - 0.05) > 0.001;
  }));

  // Rotate in-place: orientation sweeps through intermediate footprint poses.
  const std::vector<MotionPose> rotation{{0, 0, 0}, {0, 0, 0.4}};
  assert(!validateSweptPath(rotation, [](const MotionPose & pose) {
    return std::abs(pose.yaw - 0.2) > 0.001;
  }));
  // A path with unsampled length, non-finite pose, or too many samples
  // must fail closed rather than silently permit unknown terrain.
  assert(!validateSweptPath(
    corridor, [](const MotionPose &) {return true;},
    PathSweepLimits{0.025, 0.05, 3}));
  assert(!validateSweptPath(
    {{0, 0, 0}, {nan, 0, 0}},
    [](const MotionPose &) {return true;}));
  assert(!validateSweptPath(
    {{0, 0, 0}},
    [](const MotionPose &) {return true;}));
  // Candidate endpoints are absolute <= 0.50m from the original Goal,
  // deterministic, and keep the requested final Yaw unchanged.
  const patrol_navigation_controller::MotionPose goal{1.0, 2.0, 1.2};
  const auto nearby = patrol_navigation_controller::nearbyGoalCandidates(goal);
  assert(nearby.size() > 25);
  double previous_distance = 0.0;
  for (const auto & candidate : nearby) {
    const double distance = std::hypot(
      candidate.x - goal.x, candidate.y - goal.y);
    assert(distance <= 0.48 + 1e-8);
    assert(distance > 0.0);
    assert(distance + 1e-10 >= previous_distance);
    assert(candidate.yaw == goal.yaw);
    previous_distance = distance;
  }
  assert(std::abs(std::hypot(nearby.front().x - 1.0,
    nearby.front().y - 2.0) - 0.05) < 1e-8);
  const auto repeated = patrol_navigation_controller::nearbyGoalCandidates(goal);
  assert(repeated.size() == nearby.size());
  for (size_t i = 0; i < nearby.size(); ++i) {
    assert(nearby[i].x == repeated[i].x &&
      nearby[i].y == repeated[i].y);
  }
  assert(patrol_navigation_controller::nearbyGoalCandidates(
    goal, 0.51).empty());
  assert(patrol_navigation_controller::nearbyGoalCandidates(
    {1.0, 2.0, nan}).empty());
  return 0;
}
