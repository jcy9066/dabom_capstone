// Standard C++ test; does not require ROS2 or physical motor output.
#include <cassert>
#include <cmath>
#include <limits>
#include "patrol_navigation_controller/segment_handoff_guard.hpp"

using patrol_navigation_controller::stationaryForSegmentTransition;
using patrol_navigation_controller::validUnitQuaternion;

int main()
{
  const double nan = std::numeric_limits<double>::quiet_NaN();
  const double inf = std::numeric_limits<double>::infinity();
  assert(stationaryForSegmentTransition(0.0, 0.0, 0.02, 0.04));
  assert(stationaryForSegmentTransition(0.019, -0.039, 0.02, 0.04));
  assert(!stationaryForSegmentTransition(0.02, 0.0, 0.02, 0.04));
  assert(!stationaryForSegmentTransition(0.0, -0.04, 0.02, 0.04));
  assert(!stationaryForSegmentTransition(0.0, 0.11, 0.02, 0.04));
  assert(!stationaryForSegmentTransition(nan, 0.0, 0.02, 0.04));
  assert(!stationaryForSegmentTransition(0.0, inf, 0.02, 0.04));
  assert(!stationaryForSegmentTransition(0.0, 0.0, nan, 0.04));
  assert(!stationaryForSegmentTransition(0.0, 0.0, 0.02, 0.0));
  assert(validUnitQuaternion(0, 0, 0, 1));
  assert(validUnitQuaternion(0, 0, std::sqrt(0.5), std::sqrt(0.5)));
  assert(!validUnitQuaternion(0, 0, 0, 0));
  assert(!validUnitQuaternion(0, 0, 0, 1.2));
  assert(!validUnitQuaternion(0, nan, 0, 1));
  assert(!validUnitQuaternion(0, 0, 0, 1, nan));
  return 0;
}
