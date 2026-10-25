// Standard C++ test; does not require ROS2 or physical motor output.
#include <cassert>
#include <cmath>
#include <limits>
#include "patrol_navigation_controller/segment_handoff_guard.hpp"

using patrol_navigation_controller::stationaryForSegmentTransition;
using patrol_navigation_controller::validUnitQuaternion;
using patrol_navigation_controller::safeToContinuePivot;
using patrol_navigation_controller::boundedPivotAngularCommand;
using patrol_navigation_controller::validDifferentialDriveCommand;

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
  // Initial pivot must wait for a true stop. The subsequent control cycles
  // allow angular velocity, but never unwanted linear drift.
  assert(!safeToContinuePivot(false, 0.0, 0.10, 0.02, 0.04));
  assert(safeToContinuePivot(false, 0.0, 0.0, 0.02, 0.04));
  assert(safeToContinuePivot(true, 0.0, 0.10, 0.02, 0.04));
  assert(safeToContinuePivot(true, 0.0, -0.80, 0.02, 0.04));
  assert(!safeToContinuePivot(true, 0.03, 0.10, 0.02, 0.04));
  assert(!safeToContinuePivot(true, nan, 0.10, 0.02, 0.04));
  assert(!safeToContinuePivot(true, 0.0, inf, 0.02, 0.04));
  assert(!safeToContinuePivot(true, 0.0, 0.10, -0.02, 0.04));
  // A single spike in measured odometry must not enter the command ramp:
  // each 10Hz output increments from the previous accepted command by 0.1rad/s.
  assert(std::abs(boundedPivotAngularCommand(0.0, 1.05, 1.05, 1.0, 10.0) - 0.1) < 1e-12);
  assert(std::abs(boundedPivotAngularCommand(0.1, 1.05, 1.05, 1.0, 10.0) - 0.2) < 1e-12);
  assert(std::abs(boundedPivotAngularCommand(0.5, -1.05, 1.05, 1.0, 10.0) - 0.4) < 1e-12);
  assert(std::abs(boundedPivotAngularCommand(1.0, 10.0, 1.05, 1.0, 10.0) - 1.05) < 1e-12);
  assert(std::isnan(boundedPivotAngularCommand(nan, 1.0, 1.05, 1.0, 10.0)));
  assert(std::isnan(boundedPivotAngularCommand(0.0, 1.0, 1.05, 1.0, 0.0)));
  // Differential drive must reject an invalid 6D twist even when vx/wz
  // are zero; otherwise the STOP fast-path would ignore the other axes.
  assert(validDifferentialDriveCommand(0.0, 0.0, 0.0, 0.0, 0.0, 0.0));
  assert(validDifferentialDriveCommand(0.2, 0.0, 0.0, 0.0, 0.0, 1.0));
  assert(validDifferentialDriveCommand(0.0, 1e-10, 0.0, 0.0, 0.0, 0.0));
  assert(!validDifferentialDriveCommand(0.0, 0.1, 0.0, 0.0, 0.0, 0.0));
  assert(!validDifferentialDriveCommand(0.0, 0.0, 0.1, 0.0, 0.0, 0.0));
  assert(!validDifferentialDriveCommand(0.0, 0.0, 0.0, 0.1, 0.0, 0.0));
  assert(!validDifferentialDriveCommand(0.0, 0.0, 0.0, 0.0, 0.1, 0.0));
  assert(!validDifferentialDriveCommand(0.0, nan, 0.0, 0.0, 0.0, 0.0));
  assert(!validDifferentialDriveCommand(0.0, 0.0, nan, 0.0, 0.0, 0.0));
  assert(!validDifferentialDriveCommand(0.0, 0.0, 0.0, inf, 0.0, 0.0));
  assert(!validDifferentialDriveCommand(0.0, 0.0, 0.0, 0.0, 0.0, inf));
  assert(validUnitQuaternion(0, 0, 0, 1));
  assert(validUnitQuaternion(0, 0, std::sqrt(0.5), std::sqrt(0.5)));
  assert(!validUnitQuaternion(0, 0, 0, 0));
  assert(!validUnitQuaternion(0, 0, 0, 1.2));
  assert(!validUnitQuaternion(0, nan, 0, 1));
  assert(!validUnitQuaternion(0, 0, 0, 1, nan));
  return 0;
}
