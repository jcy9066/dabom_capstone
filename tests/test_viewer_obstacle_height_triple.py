"""Exact 3x visual obstacle height regression, never collision/XY changes."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / "frontend/services/static/lidar_3d_viewer.js"


class ViewerObstacleHeightTriple(unittest.TestCase):
    def test_static_and_dynamic_heights_triple_dev_baseline(self):
        js = VIEWER.read_text(encoding="utf-8")
        def constant(name):
            match = re.search(r"const\\s+" + name + r"\\s*=\\s*([0-9.]+);", js)
            self.assertIsNotNone(match, name)
            return float(match.group(1))
        # Current dev had 0.08m occupied and 0.06m dynamic obstacle walls.
        self.assertAlmostEqual(0.08 * 3, constant("DEFAULT_OCCUPIED_HEIGHT_M"))
        self.assertAlmostEqual(0.06 * 3, constant("DYNAMIC_OBSTACLE_HEIGHT_M"))
        self.assertIn("obstacleHeightM = clamp(wheelRadius * 6.75, 0.21, 0.36)", js)
        self.assertAlmostEqual(2.25 * 3, 6.75)
        self.assertAlmostEqual(0.07 * 3, 0.21)
        self.assertAlmostEqual(0.12 * 3, 0.36)

    def test_footprint_and_costmap_coordinates_are_unchanged(self):
        js = VIEWER.read_text(encoding="utf-8")
        # Changing wall height cannot change placement or cell extraction.
        for invariant in (
            "Number(costmap.width)", "Number(costmap.height)",
            "Number(costmap.resolution)", "currentCostmap?.dynamic_obstacles",
            "currentCostmap?.inflation", "origin.x", "origin.y",
            "origin.yaw", "mapRoot.rotation.z = transform.yaw",
            "function displayedGlobalPath(", "function createGoalMarker(",
        ):
            self.assertIn(invariant, js)


if __name__ == "__main__":
    unittest.main()
