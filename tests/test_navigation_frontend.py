import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NavigationFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.control = (ROOT / "frontend/services/static/navigation_control.js").read_text(encoding="utf-8")
        cls.map_control = (ROOT / "frontend/components/navigation/saved_map_modal.js").read_text(encoding="utf-8")
        cls.map_compatibility = (ROOT / "frontend/services/static/navigation_map_control.js").read_text(encoding="utf-8")
        cls.map_control_css = (ROOT / "frontend/services/static/navigation_map_control.css").read_text(encoding="utf-8")
        cls.drive_component = (ROOT / "frontend/components/controls/drive_mode_control.js").read_text(encoding="utf-8")
        cls.navigation_component = (ROOT / "frontend/components/controls/navigation_mode_control.js").read_text(encoding="utf-8")
        cls.controls_css = (ROOT / "frontend/components/controls/controls.css").read_text(encoding="utf-8")

    def test_consumes_frozen_dashboard_navigation_contract(self):
        for contract in (
            "payload?.dashboard_config",
            "config.estop_cooldown_sec",
            "config.goal_reached_tolerance_m",
            "NAVIGATION_STOP_CONFIRMATION_REQUIRED",
            "confirm_stop: true",
            "'/api/navigation/control/emergency-stop'",
            "'/api/navigation/control/resume'",
        ):
            self.assertIn(contract, self.control)

    def test_mapping_goal_and_manual_auto_transition_rules_are_wired(self):
        self.assertIn(
            "Mapping 모드에서는 주행 목표를 설정할 수 없습니다. Driving 모드로 전환해주세요.",
            self.control,
        )
        auto_request = self.control.index("await requestDriveMode('AUTO', { announce: false })")
        goal_request = self.control.index("mutate('/api/navigation/control/goal', { goal })")
        self.assertLess(auto_request, goal_request)
        self.assertIn("state.draftGoal = null", self.control)
        viewer = (
            ROOT / "frontend/services/static/lidar_3d_viewer.js"
        ).read_text(encoding="utf-8")
        self.assertNotIn("drawGoalFlag", self.control)
        self.assertIn("createGoalMarker", viewer)
        self.assertIn("screenToGround", viewer)
        self.assertIn("GLTFLoader", viewer)
        self.assertIn("robot_upper_chassis.glb", viewer)

    def test_3d_viewer_reuses_existing_minimap_shell_and_local_assets(self):
        template = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
        viewer = (
            ROOT / "frontend/services/static/lidar_3d_viewer.js"
        ).read_text(encoding="utf-8")
        dashboard = (
            ROOT / "frontend/services/static/script.js"
        ).read_text(encoding="utf-8")

        self.assertIn('id="camera-stream"', template)
        self.assertIn('class="minimap-overlay lidar-viewer-overlay"', template)
        self.assertNotIn('class="minimap-overlay lidar-viewer-overlay expanded"', template)
        self.assertIn('id="minimapExpandBtn"', template)
        self.assertIn("minimap.classList.toggle('expanded', minimapExpanded)", dashboard)
        self.assertIn("expanded: minimapExpanded", dashboard)
        self.assertIn("setExpandedState?.(minimapExpanded)", dashboard)
        self.assertNotIn("minimap.style.width", dashboard)
        self.assertNotIn("minimap.style.height", dashboard)
        self.assertIn("applyCollapsedTopView", viewer)
        self.assertIn("controls.enabled = viewerExpanded", viewer)
        self.assertIn('id="lidar-control-drawer"', template)
        self.assertIn('id="lidarControlDrawerToggle"', template)
        self.assertIn("setControlDrawerOpen", viewer)
        self.assertIn('id="lidarViewerFullscreenBtn"', template)
        self.assertIn('id="lidarControlDrawerToggle"', template)
        self.assertIn('data-control-button="mode"', template)
        self.assertIn('data-control-button="view"', template)
        self.assertNotIn("CONTROLS ▴", template)
        self.assertIn("toggleLidarViewerFullscreen", dashboard)
        self.assertIn('id="lidarControlOverflow"', template)
        self.assertIn('id="lidarControlOverflowToggle"', template)
        self.assertIn('data-control-overflow-rank="100"', template)
        self.assertIn("layoutControlOverflow", viewer)
        self.assertIn("scheduleControlOverflowLayout", viewer)
        self.assertNotIn('id="lidar-live-badge"', template)
        self.assertNotIn('id="lidar-map-status"', template)
        self.assertNotIn("getElementById('lidar-live-badge')", dashboard)
        self.assertNotIn("getElementById('lidar-map-status')", dashboard)
        self.assertNotIn("lidar-map-canvas", template)
        self.assertNotIn("cdn.jsdelivr.net", template)
        self.assertIn("/static/vendor/three/three.module.min.js", template)
        self.assertIn("GLTFLoader", viewer)
        self.assertIn("robot_upper_chassis.glb", viewer)
        self.assertIn("robot_lower_chassis.glb", viewer)
        self.assertNotIn("ThreeMFLoader", viewer)

    def test_dashboard_and_expanded_map_share_navigation_state(self):
        self.assertIn("controls.syncNavigationMode", self.navigation_component)
        self.assertIn("controls?.syncNavigationMode?.(payload.navigation_mode", self.control)
        self.assertIn("dashboard-navigation-mode-controls-mount", self.drive_component)
        self.assertIn("global.navigationControl?.applyState?.(payload)", self.map_control)

    def test_saved_map_requires_explicit_or_server_active_selection(self):
        self.assertNotIn("maps[0]", self.map_control)
        self.assertNotIn("map === maps[0]", self.map_control)
        self.assertIn("const selectedName = snapshot?.selectedName || activeName", self.map_control)
        self.assertIn("radio.checked = map.map_name === selectedName", self.map_control)
        self.assertIn("load.disabled = !selectedMapName(container)", self.map_control)
        self.assertIn("labels.selectRequired", self.map_control)

    def test_saved_map_modal_uses_shared_manager_without_direct_modal_dom_ownership(self):
        template = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
        dashboard = (ROOT / "frontend/services/static/script.js").read_text(encoding="utf-8")
        self.assertIn("components.navigationMaps", self.map_control)
        self.assertIn("manager.register(VIEW_NAME", self.map_control)
        self.assertIn("manager.open(VIEW_NAME", self.map_control)
        self.assertIn("manager.close()", self.map_control)
        self.assertNotIn("getElementById('commonModal')", self.map_control)
        self.assertNotIn("getElementById('modalTitle')", self.map_control)
        self.assertNotIn("getElementById('modalBody')", self.map_control)
        self.assertNotIn("getElementById('commonModal')", self.map_compatibility)
        self.assertNotIn('onclick="openSavedMapModal()"', template)
        self.assertIn("/components/navigation/saved_map_modal.js", template)
        self.assertIn("components.navigationMaps?.mount({", dashboard)

    def test_saved_map_contracts_and_pending_cursor_remain_scoped(self):
        for contract in (
            "'/api/navigation/maps'",
            "'/api/navigation/maps/active'",
            "'/api/navigation/maps/rename'",
            "'/api/navigation/control/mode'",
            "mode: 'DRIVING'",
            "initial_pose: { x, y, yaw_degrees: yawDegrees }",
            "global.navigationControl?.applyState?.(payload)",
            "INITIAL_POSE_OUT_OF_BOUNDS",
            "MAP_OPERATION_IN_PROGRESS",
        ):
            self.assertIn(contract, self.map_control)
        self.assertIn(".saved-map-actions button:disabled { cursor: not-allowed;", self.map_control_css)
        self.assertIn(".saved-map-modal.is-pending .saved-map-actions button:disabled { cursor: progress;", self.map_control_css)
        self.assertNotIn("button:disabled { cursor: wait;", self.map_control_css)

    def test_estop_is_independent_from_directional_dpad_disable(self):
        self.assertIn(".d-pad-container.disabled .d-btn", self.controls_css)
        self.assertIn("#dpad-center-action-mount", self.controls_css)
        self.assertIn("payload.connected === true", self.control)
        self.assertIn("state.estopPending", self.control)
        self.assertIn("state.estopCooldownUntil", self.control)

    def test_only_hazard_navigation_events_are_emitted(self):
        for code in (
            "PI_CONNECTION_LOSS",
            "LIDAR_DATA_LOSS",
            "ODOMETRY_LOSS",
            "LOCALIZATION_LOST",
            "REQUIRED_TF_FAILURE",
            "EMERGENCY_STOP_ACTIVE",
            "EMERGENCY_STOP_CLEARED",
        ):
            self.assertIn(code, self.control)
        self.assertIn("dabom:navigation-hazard", self.control)
        self.assertIn("document.addEventListener('dabom:navigation-hazard', renderNavigationHazard)", self.control)
        self.assertIn("const target = $('alertBox')", self.control)
        self.assertIn("row.className = `alert-entry", self.control)
        self.assertIn("const MAX_HAZARD_ENTRIES = 50", self.control)
        self.assertIn("new MutationObserver(scheduleHazardReconcile)", self.control)
        self.assertIn("reconcileNavigationHazards(true)", self.control)
        self.assertIn("알림 내역이 삭제되었습니다", self.control)
        self.assertNotIn("dabom:navigation-normal-operation", self.control)

    def test_manual_mode_pauses_navigation_and_retains_goal_before_pi_mode_command(self):
        start = self.control.index("async function requestDriveMode")
        end = self.control.index("function navigationIsMoving", start)
        request = self.control[start:end]
        state_read = request.index("await refreshState()")
        pause = request.index("mutate('/api/navigation/control/pause-for-manual')")
        mode_command = request.index("mutate('/api/robots/pi-01/command'")
        self.assertLess(state_read, pause)
        self.assertLess(pause, mode_command)
        self.assertNotIn("mutate('/api/navigation/control/cancel')", request)
        self.assertIn("paused.goal_retained !== true", request)
        self.assertIn("JSON.stringify(paused.active_goal) !== retainedGoal", request)
        self.assertIn("JSON.stringify(paused.planned_path) !== retainedPath", request)
        stop_delivery = request.index("paused.stop_delivered !== true")
        self.assertLess(stop_delivery, mode_command)
        self.assertIn("MANUAL 전환을 중단했습니다", request)
        self.assertIn("state.drivePending", request)


if __name__ == "__main__":
    unittest.main()
