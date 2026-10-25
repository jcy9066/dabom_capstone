// Copyright (c) 2026. Licensed under MIT.
// Shared full-chassis collision contract for both global planning and local control.
// Call while holding the costmap mutex; never certify a stale or unknown map.
#ifndef PATROL_NAVIGATION_CONTROLLER__COSTMAP_FOOTPRINT_VALIDATOR_HPP_
#define PATROL_NAVIGATION_CONTROLLER__COSTMAP_FOOTPRINT_VALIDATOR_HPP_

#include "patrol_navigation_controller/grid_footprint_geometry.hpp"
#include "patrol_navigation_controller/path_time_estimator.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <vector>

#include "nav2_costmap_2d/cost_values.hpp"
#include "nav2_costmap_2d/costmap_2d.hpp"
#include "nav2_costmap_2d/footprint.hpp"

namespace patrol_navigation_controller
{

inline bool polygonFootprintClear(
  const nav2_costmap_2d::Costmap2D * costmap,
  const MotionPose & pose, const double footprint_cost,
  const nav2_costmap_2d::Footprint & footprint,
  int * center_cost_out = nullptr, int * occupied_cell_cost_out = nullptr)
{
  unsigned int mx = 0;
  unsigned int my = 0;
  if (occupied_cell_cost_out) {
    *occupied_cell_cost_out = -1;
  }
  if (!costmap || !std::isfinite(pose.x) || !std::isfinite(pose.y) ||
    !std::isfinite(pose.yaw) ||
    !costmap->worldToMap(pose.x, pose.y, mx, my)) {
    if (center_cost_out) {
      *center_cost_out = -1;
    }
    return false;
  }
  const int center_cost = static_cast<int>(costmap->getCost(mx, my));
  if (center_cost_out) {
    *center_cost_out = center_cost;
  }
  // Preserve the original Nav2 Humble Smac center + boundary checks.
  if (center_cost >= static_cast<int>(nav2_costmap_2d::INSCRIBED_INFLATED_OBSTACLE) ||
    !std::isfinite(footprint_cost) || footprint_cost < 0.0 ||
    footprint_cost >= static_cast<double>(nav2_costmap_2d::LETHAL_OBSTACLE) ||
    footprint.size() < 3)
  {
    return false;
  }

  // The stock FootprintCollisionChecker rasterizes the polygon PERIMETER.
  // A lethal / unknown map cell can lie entirely INSIDE a rectangular chassis
  // without intersecting any perimeter or its center. Check the whole area.
  // The footprint passed here already includes configured footprint_padding.
  const double cos_yaw = std::cos(pose.yaw);
  const double sin_yaw = std::sin(pose.yaw);
  std::vector<GridPoint> polygon;
  polygon.reserve(footprint.size());
  double min_x = std::numeric_limits<double>::infinity();
  double max_x = -std::numeric_limits<double>::infinity();
  double min_y = std::numeric_limits<double>::infinity();
  double max_y = -std::numeric_limits<double>::infinity();
  for (const auto & vertex : footprint) {
    const double x = pose.x + vertex.x * cos_yaw - vertex.y * sin_yaw;
    const double y = pose.y + vertex.x * sin_yaw + vertex.y * cos_yaw;
    unsigned int vertex_mx = 0;
    unsigned int vertex_my = 0;
    if (!std::isfinite(x) || !std::isfinite(y) ||
      !costmap->worldToMap(x, y, vertex_mx, vertex_my))
    {
      return false;  // No clearance proof if the chassis leaves the map.
    }
    polygon.push_back({x, y});
    min_x = std::min(min_x, x);
    max_x = std::max(max_x, x);
    min_y = std::min(min_y, y);
    max_y = std::max(max_y, y);
  }

  if (!validConvexFootprint(polygon)) {
    return false;
  }

  unsigned int min_mx = 0;
  unsigned int min_my = 0;
  unsigned int max_mx = 0;
  unsigned int max_my = 0;
  if (!costmap->worldToMap(min_x, min_y, min_mx, min_my) ||
    !costmap->worldToMap(max_x, max_y, max_mx, max_my))
  {
    return false;
  }
  const double resolution = costmap->getResolution();
  if (!std::isfinite(resolution) || resolution <= 0.0) {
    return false;
  }
  for (size_t cy = min_my; cy <= max_my; ++cy) {
    for (size_t cx = min_mx; cx <= max_mx; ++cx) {
      const auto cell_cost = costmap->getCost(
        static_cast<unsigned int>(cx), static_cast<unsigned int>(cy));
      if (cell_cost < nav2_costmap_2d::LETHAL_OBSTACLE) {
        continue;  // Cost 253 is inflation, not physical occupation.
      }
      double wx = 0.0;
      double wy = 0.0;
      costmap->mapToWorld(
        static_cast<unsigned int>(cx), static_cast<unsigned int>(cy), wx, wy);
      if (polygonOverlapsGridCell(polygon, wx, wy, resolution / 2.0)) {
        if (occupied_cell_cost_out) {
          *occupied_cell_cost_out = static_cast<int>(cell_cost);
        }
        return false;
      }
    }
  }
  return true;
}


}  // namespace patrol_navigation_controller

#endif  // PATROL_NAVIGATION_CONTROLLER__COSTMAP_FOOTPRINT_VALIDATOR_HPP_
