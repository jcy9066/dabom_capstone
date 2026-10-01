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

    def test_3d_viewer_rotates_lidar_visuals_180_degrees_in_place(self):
        template = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
        viewer = (
            ROOT / "frontend/services/static/lidar_3d_viewer.js"
        ).read_text(encoding="utf-8")

        self.assertIn("const LIDAR_VISUAL_YAW_OFFSET_RAD = Math.PI;", viewer)
        self.assertIn("for (const group of [scanRoot, pointsRoot])", viewer)
        self.assertIn(
            "group.rotation.z = pose.yaw + LIDAR_VISUAL_YAW_OFFSET_RAD;",
            viewer,
        )
        self.assertIn(
            'static/lidar_3d_viewer.js?v=20261001-live-map-walls-v26',
            template,
        )

    def test_3d_camera_view_is_mirrored_horizontally_only(self):
        template = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
        viewer = (
            ROOT / "frontend/services/static/lidar_3d_viewer.js"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "const uvs = new Float32Array([\n            1, 0,\n            0, 0,\n            0, 1,\n            1, 1,",
            viewer,
        )
        self.assertIn("cameraTextureContext.drawImage(", viewer)
        self.assertIn("const cameraImage = document.getElementById('camera-stream')", viewer)
        self.assertIn(
            'static/lidar_3d_viewer.js?v=20261001-live-map-walls-v26',
            template,
        )

    def test_3d_viewer_uses_differential_drive_motion_and_wheel_roll(self):
        template = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
        viewer = (
            ROOT / "frontend/services/static/lidar_3d_viewer.js"
        ).read_text(encoding="utf-8")

        self.assertIn("POSE_FORWARD_RESPONSE_PER_SEC = 7.0", viewer)
        self.assertIn("POSE_LATERAL_RESPONSE_PER_SEC = 2.5", viewer)
        self.assertIn("POSE_YAW_RESPONSE_PER_SEC = 9.0", viewer)
        self.assertIn("function advanceWheelVisuals(previousPose, nextPose)", viewer)
        self.assertIn("const centerDistance = (", viewer)
        self.assertIn("centerDistance\n            - yawDelta * wheelVisualTrackM * 0.5", viewer)
        self.assertIn("centerDistance\n            + yawDelta * wheelVisualTrackM * 0.5", viewer)
        self.assertIn(
            "wheelRollRadians.left += leftDistance / wheelVisualRadiusM",
            viewer,
        )
        self.assertIn(
            "wheelRollRadians.right += rightDistance / wheelVisualRadiusM",
            viewer,
        )
        self.assertNotIn(
            "wheelRollRadians.left -= leftDistance / wheelVisualRadiusM",
            viewer,
        )
        self.assertNotIn(
            "wheelRollRadians.right -= rightDistance / wheelVisualRadiusM",
            viewer,
        )
        self.assertIn("wheel.rotation.y = wheelRollRadians.left", viewer)
        self.assertIn("wheel.rotation.y = wheelRollRadians.right", viewer)
        self.assertIn("const forwardError = errorX * forwardX + errorY * forwardY", viewer)
        self.assertIn("const lateralError = errorX * lateralX + errorY * lateralY", viewer)
        self.assertIn(
            'static/lidar_3d_viewer.js?v=20261001-live-map-walls-v26',
            template,
        )

    def test_mapping_viewer_stays_visible_before_first_slam_map_and_pose(self):
        template = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
        viewer = (
            ROOT / "frontend/services/static/lidar_3d_viewer.js"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "const MAPPING_PREVIEW_POSE = Object.freeze({ x: 0, y: 0, yaw: 0 });",
            viewer,
        )
        self.assertIn("function payloadMatchesMappingSession(payload)", viewer)
        self.assertIn("payload.navigation_mode || ''", viewer)
        self.assertIn(
            "mappingMode ? MAPPING_PREVIEW_POSE : null",
            viewer,
        )
        self.assertIn("const liveScan = state.scan || null;", viewer)
        self.assertIn("function applyTfPose(pose)", viewer)
        self.assertIn("applyTfPose(livePose);", viewer)
        self.assertIn("const liveMap = state.map || null;", viewer)
        self.assertIn("const livePose = state.pose || null;", viewer)
        self.assertIn("liveMapRoot.name = 'live-map-walls'", viewer)
        self.assertIn("function rebuildLiveMapPreview(scan, pose, enabled)", viewer)
        self.assertIn(
            "rebuildLiveMapPreview(liveScan, visualizationPose, mappingMode);",
            viewer,
        )
        self.assertNotIn(
            "payloadMatchesMappingSession(state.map)",
            viewer,
        )
        self.assertNotIn(
            "robotPoseGroup,\n            tfPoseGroup,",
            viewer,
        )
        dashboard = (
            ROOT / "frontend/services/static/script.js"
        ).read_text(encoding="utf-8")
        self.assertIn("const NAVIGATION_SNAPSHOT_VISIBLE_MS = 1000;", dashboard)
        self.assertIn("/ws/navigation/visualization", dashboard)
        self.assertIn("new WebSocket(navigationVisualizationWsUrl())", dashboard)
        self.assertIn("applyNavigationStreamMessage", dashboard)
        self.assertIn("if (navigationVisualizationSocketOpen) return;", dashboard)
        self.assertNotIn(
            "payloadMatchesMappingSession(state.scan)",
            viewer,
        )
        self.assertIn(
            "applyVisualizationState(currentVisualizationState);",
            viewer,
        )
        self.assertIn(
            "updateRobotPose(MAPPING_PREVIEW_POSE);",
            viewer,
        )
        self.assertIn(
            "applyRenderedPose(renderedPose || targetPose);",
            viewer,
        )
        self.assertIn(
            'static/lidar_3d_viewer.js?v=20261001-live-map-walls-v26',
            template,
        )

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
        self.assertIn("syncControlDrawerState", viewer)
        self.assertIn("dabom:drawer-toggle", viewer)
        self.assertIn('/components/controls/drawer_toggle.js?v=20260930-lidar3d-controls-v16', template)
        self.assertIn('/components/controls/viewer_status.js?v=20260930-lidar3d-controls-v16', template)
        self.assertIn('/components/controls/led_toggle.js?v=20260930-lidar3d-controls-v16', template)
        self.assertIn('data-drawer-target="lidar-control-drawer"', template)
        self.assertIn('data-drawer-target="lidar-layer-drawer"', template)
        self.assertIn('dashboard-drawer-toggle--top', template)
        self.assertIn('dashboard-drawer-toggle--left', template)
        self.assertIn('.dashboard-drawer-toggle[aria-expanded="true"]', self.controls_css)
        self.assertIn('opacity: 0;', self.controls_css)
        self.assertIn('.dashboard-drawer-toggle[aria-expanded="false"]', self.controls_css)
        self.assertIn('id="lidarViewerFullscreenBtn"', template)
        self.assertIn('id="lidarControlDrawerToggle"', template)
        self.assertIn('id="navigation-viewer-mode-controls-mount"', template)
        self.assertIn("navigation-mode-toggle", self.navigation_component)
        self.assertIn('data-control-button="view"', template)
        self.assertNotIn("CONTROLS ▴", template)
        self.assertIn("toggleLidarViewerFullscreen", dashboard)
        self.assertIn("requestElementFullscreen", dashboard)
        self.assertIn("webkitRequestFullscreen", dashboard)
        self.assertIn("webkitfullscreenchange", dashboard)
        self.assertIn("async function toggleMinimapExpand()", dashboard)
        self.assertIn("currentFullscreenElement() === minimap", dashboard)
        self.assertIn("cameraView = document.getElementById('video-wrapper')", dashboard)
        self.assertIn("await exitDocumentFullscreen()", dashboard)
        self.assertIn("await requestElementFullscreen(cameraView)", dashboard)
        self.assertIn('id="lidarControlOverflow"', template)
        self.assertIn('id="lidarControlOverflowToggle"', template)
        self.assertIn('id="dashboard-led-control-mount"', template)
        self.assertNotIn('id="lidar-led-control-mount"', template)
        self.assertIn("dashboard-led-control-mount", self.control)
        dashboard_css = (
            ROOT / "frontend/services/static/style.css"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "grid-template-columns: minmax(0, 38fr) minmax(88px, 20fr) minmax(190px, 42fr);",
            dashboard_css,
        )
        self.assertIn("grid-template-rows: repeat(3, minmax(0, 1fr));", dashboard_css)
        self.assertIn(".dashboard-mode-controls > *", dashboard_css)
        self.assertIn("overflow: hidden;", dashboard_css)
        self.assertIn(".dashboard-led-toggle-state {\n    min-width: 0;", self.controls_css)
        self.assertIn('id="dashboardBeepBtn"', template)
        self.assertIn('class="card action-btn action-beep"', template)
        self.assertIn("'/api/navigation/control/beep'", self.control)
        self.assertNotIn('id="navigation-led-test"', template)
        self.assertNotIn('id="navigation-estop"', template)
        self.assertNotIn('id="navigation-resume"', template)
        self.assertIn("layoutControlOverflow", viewer)
        self.assertIn("scheduleControlOverflowLayout", viewer)
        self.assertNotIn("new MutationObserver", viewer)
        self.assertIn("goalInteractionAllowed", viewer)
        self.assertIn("syncGoalInteractionAvailability", viewer)
        self.assertIn("button.disabled = !allowed", viewer)
        self.assertNotIn('data-lidar-view="follow" data-control-button="view" data-control-overflow-rank', template)
        self.assertIn("controlStrip?.scrollTo?.", viewer)
        self.assertIn("controlStrip.scrollLeft += event.deltaY", viewer)
        self.assertIn('id="lidar-layer-drawer"', template)
        self.assertIn('id="lidarLayerDrawerToggle"', template)
        self.assertIn('data-open="false"', template)
        self.assertNotIn("<summary>DISPLAY</summary>", template)
        self.assertNotIn('id="lidarMapSaveBtn"', template)
        self.assertIn("saved-map-save-current", self.map_control)
        self.assertIn("components.controls?.controlButton?.enhance?.(save)", self.map_control)
        self.assertIn("saved-map-start-mapping", self.map_control)
        self.assertIn("saved-map-clear-current", self.map_control)
        self.assertIn("request('MAPPING', { restart: true })", self.map_control)
        self.assertIn("global.navigationMapView?.clearMapDisplay?.()", self.map_control)
        self.assertIn("components.controls?.controlButton?.enhance?.(startMapping)", self.map_control)
        self.assertIn("components.controls?.controlButton?.enhance?.(clearCurrentMap)", self.map_control)
        self.assertIn("scanVisualHeightM = topZ + ORIGINAL_CAMERA_HEIGHT_OFFSET_M", viewer)
        self.assertIn("cameraMountLocal.set(", viewer)
        self.assertIn("LIDAR_HEIGHT_M,", viewer)
        self.assertIn("cameraViewPlane.frustumCulled = false", viewer)
        self.assertIn("cameraViewPlane.renderOrder = 50", viewer)
        self.assertIn("cameraViewBorder.renderOrder = 51", viewer)
        self.assertIn("depthTest: false", viewer)
        self.assertIn("flex: 0 0 48px;", self.controls_css)
        self.assertIn("width: 48px;", self.controls_css)
        self.assertIn("height: 26px;", self.controls_css)
        self.assertIn("width: 18px;", self.controls_css)
        self.assertIn("height: 20px;", self.controls_css)
        self.assertIn("grid-template-columns: minmax(0, 1fr) 48px minmax(0, 1fr);", dashboard_css)
        self.assertIn("background: #60a5fa;", dashboard_css)
        self.assertIn("background: #22d3ee;", self.controls_css)
        self.assertIn("background: #22c55e;", self.controls_css)
        self.assertIn("displayMetric(data.cpu_usage)", dashboard)
        self.assertIn("displayMetric(data.cpu_temp)", dashboard)
        self.assertIn("displayMetric(data.ram_usage)", dashboard)
        self.assertIn("Number(data.ping)", dashboard)
        self.assertIn("ping.toFixed(1)", dashboard)
        self.assertIn("data.internet === 'offline' ? 'OFFLINE' : '--'", dashboard)
        self.assertIn("viewerOverlay?.classList.toggle('controls-collapsed', !isOpen)", viewer)
        self.assertIn("controlOverflowExpanded", viewer)
        self.assertIn("controlOverflowToggle.hidden = isOpen", viewer)
        self.assertNotIn('id="lidar-live-badge"', template)
        self.assertNotIn('id="lidar-map-status"', template)
        self.assertNotIn("getElementById('lidar-live-badge')", dashboard)
        self.assertNotIn("getElementById('lidar-map-status')", dashboard)
        self.assertNotIn("lidar-map-canvas", template)
        self.assertNotIn("lidar-map-canvas", self.control)
        self.assertNotIn("cdn.jsdelivr.net", template)
        self.assertIn("/static/vendor/three/three.module.min.js", template)
        self.assertIn("GLTFLoader", viewer)
        self.assertIn("robot_upper_chassis.glb", viewer)
        self.assertIn("robot_lower_chassis.glb", viewer)
        self.assertNotIn("ThreeMFLoader", viewer)

    def test_navigation_mode_labels_request_explicit_targets(self):
        self.assertIn("mappingLabel.dataset.navigationModeTarget = 'MAPPING'", self.navigation_component)
        self.assertIn("drivingLabel.dataset.navigationModeTarget = 'DRIVING'", self.navigation_component)
        self.assertIn("const explicitTarget = event.target?.closest?.(", self.navigation_component)
        self.assertIn("if (next === current) return;", self.navigation_component)
        self.assertIn("async function setMappingMode({ restart = false } = {})", self.control)
        self.assertIn("setMappingMode(options)", self.control)

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
        modal_source = (ROOT / "frontend/components/modal/modal_manager.js").read_text(encoding="utf-8")
        self.assertIn("syncFullscreenHost", modal_source)
        self.assertIn("currentFullscreenElement", modal_source)
        self.assertIn("fullscreenElement.appendChild(this.root)", modal_source)

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
            "labels.startMapping",
            "labels.clearCurrentMap",
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
