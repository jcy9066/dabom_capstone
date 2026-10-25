// Copyright (c) 2026. Licensed under MIT.
// Geometric overlap between a convex robot footprint and a costmap cell.
// Grid cells represent occupied AREAS, not dimensionless center points.
#ifndef PATROL_NAVIGATION_CONTROLLER__GRID_FOOTPRINT_GEOMETRY_HPP_
#define PATROL_NAVIGATION_CONTROLLER__GRID_FOOTPRINT_GEOMETRY_HPP_

#include <algorithm>
#include <cmath>
#include <limits>
#include <vector>

namespace patrol_navigation_controller
{

struct GridPoint
{
  double x;
  double y;
};

// The runtime footprint must be finite, non-degenerate and convex.
// A malformed polygon must never be treated as a valid collision envelope.
inline bool validConvexFootprint(const std::vector<GridPoint> & polygon)
{
  if (polygon.size() < 3) {
    return false;
  }
  bool positive_turn = false;
  bool negative_turn = false;
  for (size_t i = 0; i < polygon.size(); ++i) {
    const auto & a = polygon[i];
    const auto & b = polygon[(i + 1) % polygon.size()];
    const auto & c = polygon[(i + 2) % polygon.size()];
    if (!std::isfinite(a.x) || !std::isfinite(a.y) ||
      std::hypot(b.x - a.x, b.y - a.y) <= 1e-12)
    {
      return false;
    }
    const double cross =
      (b.x - a.x) * (c.y - b.y) - (b.y - a.y) * (c.x - b.x);
    if (!std::isfinite(cross)) {
      return false;
    }
    positive_turn |= cross > 1e-12;
    negative_turn |= cross < -1e-12;
    if (positive_turn && negative_turn) {
      return false;
    }
  }
  return positive_turn || negative_turn;
}

// Boundary contact counts as overlap. Invalid or non-convex robot footprints
// fail closed: a "true" result always causes an occupied cell to be rejected.
inline bool polygonOverlapsGridCell(
  const std::vector<GridPoint> & polygon,
  const double cell_center_x, const double cell_center_y,
  const double half_cell_width)
{
  if (!validConvexFootprint(polygon) ||
    !std::isfinite(cell_center_x) || !std::isfinite(cell_center_y) ||
    !std::isfinite(half_cell_width) || half_cell_width <= 0.0)
  {
    return true;
  }

  // Separating-axis theorem: exact overlap of a convex polygon and
  // the entire square cell, including its corners and polygon interior.
  const auto overlaps_axis = [&](const double ax, const double ay) {
      double min_projection = std::numeric_limits<double>::infinity();
      double max_projection = -std::numeric_limits<double>::infinity();
      for (const auto & point : polygon) {
        const double projection = point.x * ax + point.y * ay;
        min_projection = std::min(min_projection, projection);
        max_projection = std::max(max_projection, projection);
      }
      const double center = cell_center_x * ax + cell_center_y * ay;
      const double radius = half_cell_width * (std::abs(ax) + std::abs(ay));
      // Comparisons use strict separation: touching the boundary is blocked.
      return !(max_projection < center - radius - 1e-12 ||
        min_projection > center + radius + 1e-12);
    };

  if (!overlaps_axis(1.0, 0.0) || !overlaps_axis(0.0, 1.0)) {
    return false;
  }
  for (size_t i = 0; i < polygon.size(); ++i) {
    const auto & a = polygon[i];
    const auto & b = polygon[(i + 1) % polygon.size()];
    if (!overlaps_axis(-(b.y - a.y), b.x - a.x)) {
      return false;
    }
  }
  return true;
}

}  // namespace patrol_navigation_controller

#endif  // PATROL_NAVIGATION_CONTROLLER__GRID_FOOTPRINT_GEOMETRY_HPP_
