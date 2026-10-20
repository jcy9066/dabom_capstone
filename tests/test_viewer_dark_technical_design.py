"""View-only dark technical contracts, ROS and browser independent."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "frontend/services/static/lidar_3d_viewer.js"
CSS = ROOT / "frontend/services/static/lidar_3d_viewer.css"
HTML = ROOT / "frontend/templates/index.html"


class DarkTechnicalDesignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = JS.read_text(encoding="utf-8")
        cls.css = CSS.read_text(encoding="utf-8")
        cls.html = HTML.read_text(encoding="utf-8")

    def test_palette_and_all_thirteen_independent_layers(self):
        for expected in (
            "0x0b1420", "[56, 83, 103, 255]", "[29, 42, 56, 255]",
            "0x8297a9", "0x52687b", "0x577184", "0x8b5cf6",
            "0xf59e0b", "0x7dd3fc", "0x2dd4bf",
            "0xf97316", "0xfb7185", "0xfbbf24",
        ):
            self.assertIn(expected, self.js)
        for layer in (
            "map", "robot", "grid", "tf", "scan", "points", "obstacles",
            "inflation", "path", "trajectory", "goal", "camera-frustum",
            "camera-view",
        ):
            self.assertIn("'" + layer + "'", self.js)

    def test_map_outline_is_map_local_visual_only(self):
        map_body = self.js.split("function rebuildMap(map, revision)", 1)[1].split(
            "function costmapKey(", 1,
        )[0]
        for snippet in (
            "new THREE.InstancedMesh(", "topMaterial", "sideMaterial",
            "new THREE.LineLoop(", "widthM, heightM, 0.012",
            "outline.raycast = () => {};", "mapRoot.rotation.z = transform.yaw;",
            "MAP_OCCUPIED_THRESHOLD",
        ):
            self.assertIn(snippet, map_body)

    def test_robot_mesh_and_low_cost_lighting(self):
        for snippet in (
            "robot_upper_chassis.glb", "robot_lower_chassis.glb",
            "HemisphereLight", "DirectionalLight", "robot-contact-shadow",
            "color: COLORS.wheel", "color: COLORS.robotFront", "antialias: true",
        ):
            self.assertIn(snippet, self.js)
        self.assertNotIn("renderer.shadowMap.enabled = true", self.js)

    def test_path_and_trajectory_respect_chassis_depth(self):
        path = self.js.split("function rebuildPath(path)", 1)[1].split(
            "function createGoalMarker(", 1,
        )[0]
        traj = self.js.split("function ensureTrajectoryBuffer()", 1)[1].split(
            "function writeTrajectoryPoint(", 1,
        )[0]
        self.assertIn("depthTest: true", path)
        self.assertIn("depthTest: true", traj)
        self.assertIn("line.renderOrder = 20;", path)
        self.assertIn("line.renderOrder = 12;", traj)
        self.assertIn("new THREE.Line(", path)
        self.assertIn("new THREE.Points(", traj)

    def test_goals_have_different_shapes_not_semantics(self):
        goal = self.js.split("function createGoalMarker(", 1)[1].split(
            "function goalKey(", 1,
        )[0]
        for expected in (
            "isDraft = false", "new THREE.LineDashedMaterial(",
            "ring.computeLineDistances()", "[[0.072, 0.099], [0.040, 0.052]]",
            "marker.rotation.z = yaw;", "new THREE.ArrowHelper(",
        ):
            self.assertIn(expected, goal)

    def test_preserves_camera_lidar_and_user_control_contracts(self):
        for expected in (
            "LIDAR_VISUAL_YAW_OFFSET_RAD = Math.PI",
            "for (const group of [scanRoot, pointsRoot])",
            "function rebuildScan(scan)", "function updateCameraTexture(now)",
            "function screenToGround(event)", "function setExpandedState(expanded)",
            "function applyViewMode(mode)", "function applyTechnicalDensity()",
            "function sampleTrajectory(pose)", "const layerGroups = {}",
            "opacity: 0.68",
        ):
            self.assertIn(expected, self.js)
        self.assertIn("#0b1420", self.css)
        self.assertIn(
            "static/lidar_3d_viewer.js?v=20261020-dark-technical-v1", self.html
        )


if __name__ == "__main__":
    unittest.main()
