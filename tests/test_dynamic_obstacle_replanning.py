import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class DynamicObstacleReplanningContractTests(unittest.TestCase):
    def test_nav2_tuning(self):
        params = (ROOT / "navigation/ros/patrol_navigation/config/nav2_params.yaml").read_text(encoding="utf-8")
        self.assertIn('plugin: "nav2_costmap_2d::ObstacleLayer"', params)
        self.assertIn("update_frequency: 10.0", params)
        self.assertIn("publish_frequency: 2.0", params)
        self.assertIn("expected_planner_frequency: 0.25", params)
        self.assertIn("observation_persistence: 0.5", params)
        self.assertIn("allow_unknown: true", params)
        self.assertEqual(params.count("inflation_radius: 0.35"), 2)
        self.assertEqual(params.count("clearing: true"), 2)

    def test_five_second_replanning_bt(self):
        bt = (ROOT / "navigation/ros/patrol_navigation/behavior_trees/navigate_to_pose_dynamic_replanning.xml").read_text(encoding="utf-8")
        self.assertIn('<RetryUntilSuccessful num_attempts="5">', bt)
        self.assertIn('<RateController hz="10.0">', bt)
        self.assertIn('<Delay delay_msec="300">', bt)
        self.assertIn('<Timeout msec="5000">', bt)
        self.assertIn('<IsPathValid path="{path}"', bt)
        self.assertNotIn("<Wait", bt)
        self.assertNotIn("<Spin", bt)
        self.assertNotIn("<BackUp", bt)

    def test_stream_and_viewer_contract(self):
        ros = (ROOT / "server/navigation_ros_control.py").read_text(encoding="utf-8")
        app = (ROOT / "server/app.py").read_text(encoding="utf-8")
        template = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
        dashboard = (ROOT / "frontend/services/static/script.js").read_text(encoding="utf-8")
        viewer = (ROOT / "frontend/services/static/lidar_3d_viewer.js").read_text(encoding="utf-8")
        self.assertIn('GLOBAL_PATH_TOPIC = "/plan"', ros)
        self.assertIn('GLOBAL_COSTMAP_TOPIC = "/global_costmap/costmap"', ros)
        self.assertIn('"dynamic_obstacles": dynamic_obstacles', ros)
        self.assertIn("self._cancel_lock = threading.Lock()", ros)
        self.assertIn("with self._cancel_lock:", ros)
        self.assertIn("if self._navigate_goal_handle is goal_handle:", ros)
        self.assertIn('"terminal": "SUCCEEDED"', ros)
        self.assertIn('terminal_state in {"SUCCEEDED", "FAILED", "CANCELED"}', ros)
        self.assertIn('if event_type == "global_path":', app)
        self.assertIn("navigation_control_api.note_nav2_replan_signal()", app)
        self.assertNotIn("navigation_control_api.note_replanned_path(normalized)", app)
        navigation = (ROOT / "server/navigation_control_api.py").read_text(encoding="utf-8")
        self.assertIn("def _refresh_approved_replan(", navigation)
        self.assertIn("self._goal_motion_generation == generation", navigation)
        self.assertIn("self._pending_goal_cancel is None", navigation)

        self.assertIn('"global_path_updated_at": global_path_updated_at', app)
        self.assertIn('data-lidar-display="obstacles" checked>Dynamic Obstacle', template)
        self.assertIn('data-lidar-display="inflation">Inflation', template)
        self.assertIn("message.type === 'global_path'", dashboard)
        self.assertIn("message.type === 'costmap'", dashboard)
        self.assertIn("GLOBAL_PATH_STALE_MS = 2200", dashboard)
        self.assertIn("globalPathStatus: lidarState.globalPathStatus", dashboard)
        self.assertIn("beginNavigationGlobalPathPending()", dashboard)
        self.assertIn("scheduleNavigationGlobalPathStale", dashboard)
        self.assertIn("rebuildCostmap(liveCostmap)", viewer)
        self.assertIn("function displayedGlobalPath", viewer)
        # Only Goal-scoped, server-authorized paths may be rendered.
        # Unowned late /plan packets must not resurrect a canceled Goal.
        self.assertIn("control?.active_goal", viewer)
        self.assertIn("['PATH_READY', 'READY', 'NAVIGATING', 'RESUMING']", viewer)
        self.assertIn("const authorizedPath = control.planned_path;", viewer)
        self.assertIn("Array.isArray(authorizedPath)", viewer)
        self.assertIn(
            'static/script.js?v=20261022-pr90-owned-goal-v1',
            template,
        )

if __name__ == "__main__":
    unittest.main()
