"""Static UI-only guardrails for dashboard polish in October 2026.

Passing these checks does not establish browser- or hardware-level equivalence.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

def source(path):
    return (ROOT / path).read_text(encoding="utf-8")

DASHBOARD = source("frontend/templates/index.html")
LOGIN = source("frontend/templates/login.html")
CSS = source("frontend/services/static/selected_ui.css")
VISUAL = source("frontend/services/static/selected_ui.js")
RECORD_TABLE = source("frontend/components/records/record_table.js")
RECORDS = source("frontend/components/records/record_modal.js")
NAV_MODE = source("frontend/components/controls/navigation_mode_control.js")
MAP = source("frontend/components/navigation/saved_map_modal.js")
SITUATION = source("frontend/components/current_situation/current_situation.js")
GALLERY = source("frontend/components/gallery/image_detail.js")
SYSTEM = source("frontend/services/static/system_control.js")
FILTERS = source("frontend/components/records/record_filters.js")


class DashboardUiPolishContract(unittest.TestCase):
    def test_goal_cancel_is_same_control_with_new_label(self):
        self.assertIn('id="navigation-cancel"', DASHBOARD)
        self.assertIn('>goal 취소</button>', DASHBOARD)
        self.assertIn('id="navigation-start"', DASHBOARD)

    def test_login_input_contract(self):
        self.assertIn('id="loginId" type="email"', LOGIN)
        self.assertIn('id="loginPassword" type="password"', LOGIN)
        self.assertIn('placeholder="이메일을 입력하세요"', LOGIN)
        self.assertIn('placeholder="비밀번호를 입력하세요"', LOGIN)
        self.assertIn('id="sendEmailBtn"', LOGIN)
        self.assertIn('id="verifyEmailBtn"', LOGIN)
        self.assertIn('fieldset class="auth-protected-fields" id="protectedFields">', LOGIN)
        self.assertNotIn('id="protectedFields" disabled', LOGIN)
        self.assertIn('id="signupSubmit" disabled', LOGIN.replace('type="submit" ', '').replace('class="auth-btn primary signup-submit" ', ''))

    def test_no_duplicate_toggle_and_collapsible_server(self):
        self.assertEqual(DASHBOARD.count('id="darkToggle"'), 1)
        self.assertIn('id="serverStateDrawerToggle"', DASHBOARD)
        self.assertIn('id="serverStateGrid"', DASHBOARD)
        self.assertIn('aria-expanded="true"', DASHBOARD)
        self.assertIn('function installServerStatusDrawer()', VISUAL)
        self.assertIn('card.classList.toggle(\'is-collapsed\')', VISUAL)

    def test_status_values_are_css_only(self):
        self.assertIn('#pi-connection-status::first-letter', CSS)
        self.assertIn('#navigation-state-status::first-letter', CSS)
        self.assertIn("mappingLabel.dataset.navigationModeTarget = 'MAPPING'", NAV_MODE)
        self.assertIn("drivingLabel.dataset.navigationModeTarget = 'DRIVING'", NAV_MODE)
        self.assertIn("mappingLabel.textContent = 'Mapping'", NAV_MODE)
        self.assertIn("drivingLabel.textContent = 'Driving'", NAV_MODE)

    def test_saved_map_actions_and_location_callbacks_preserved(self):
        self.assertIn('saved-map-two-column', MAP)
        self.assertIn('locationColumn.append(renderLocationPanel(locationStatus))', MAP)
        self.assertIn("loadSelectedMap(container, progress)", MAP)
        self.assertIn("startMappingFromModal(container, progress)", MAP)
        self.assertIn("saveCurrentNavigationMap?.(save)", MAP)
        self.assertIn("renameSavedMap(map, container, progress)", MAP)

    def test_device_history_period_filters_unchanged(self):
        self.assertIn('data-filter="startAt"', RECORDS)
        self.assertIn('data-filter="endAt"', RECORDS)
        self.assertIn('readDateTimeRange(root)', RECORDS)
        self.assertIn("statusModal: Object.freeze({ startAt: '', endAt: '' })", FILTERS)

    def test_sort_direction_and_accessibility(self):
        self.assertIn("' ▼' : ' ▲'", RECORD_TABLE)
        self.assertIn('aria-sort', RECORD_TABLE)
        self.assertIn('data-sort-by', RECORD_TABLE)
        self.assertIn("records.bindSortHeaders", RECORD_TABLE)

    def test_current_situation_submit_preserved_without_cancel(self):
        self.assertIn('current-situation-layout', SITUATION)
        self.assertIn('id="currentSituationSubmit"', SITUATION)
        self.assertIn('id="currentSituationPreview"', SITUATION)
        self.assertIn('id="currentSituationIncludeImage"', SITUATION)
        self.assertNotIn('data-current-situation-close>취소</button>', SITUATION)
        self.assertIn("title: '상황 기록'", SITUATION)
        self.assertIn("return loadPreview(generation, elements);", SITUATION)

    def test_gallery_actions_and_zoom(self):
        self.assertIn('data-image-zoom-reset aria-label="기본 크기로 복원">Raw', GALLERY)
        self.assertIn('data-detail-change="-1"', GALLERY)
        self.assertIn('data-detail-change="1"', GALLERY)
        self.assertIn('data-detail-delete', GALLERY)
        self.assertIn("viewport.addEventListener('wheel'", GALLERY)
        self.assertIn('.gallery-detail .gallery-detail-actions button{', CSS)

    def test_gpu_control_semantics_unchanged(self):
        self.assertIn("component.control_available === false || component.state === 'unreachable'", SYSTEM)
        self.assertIn("button.disabled = pending || unreachable;", SYSTEM)
        self.assertIn("normalize.disabled = pending || unreachable;", SYSTEM)
        self.assertIn("control(group, component.id, action)", SYSTEM)
        self.assertIn("gpu: 'SERVER'", SYSTEM)
        self.assertIn("pi: 'PI'", SYSTEM)

    def test_supervisor_owned_cards_hidden_in_settings_only(self):
        self.assertIn("SETTINGS_HIDDEN_COMPONENT_IDS = new Set(['wheel_odometry', 'map_bridge', 'lidar_ros'])", SYSTEM)
        self.assertIn("for (const component of visibleComponents)", SYSTEM)
        self.assertIn("components.filter(component => !SETTINGS_HIDDEN_COMPONENT_IDS.has(component.id))", SYSTEM)
        self.assertIn("const components = [...piComponents, ...gpuComponents].filter(", SYSTEM)
        self.assertNotIn("const mapBridge = components.find(", SYSTEM)
        self.assertIn("renderGroup('gpuSystemControls', status.gpu, 'gpu')", SYSTEM)
        self.assertNotIn("piSystemControls", SYSTEM)
        self.assertIn("wrapper.append(gpu);", SYSTEM)
        backend = source("frontend/system_control.py")
        self.assertIn('supervisor_managed_components = frozenset({"wheel_odometry", "map_bridge"})', backend)
        self.assertIn('"gpu": [gpu_status(component_id) for component_id in component_ids]', backend)

    def test_sidebar_account_visible_and_logout_30_percent(self):
        self.assertIn('id="sidebarAccountName"', DASHBOARD)
        self.assertIn('id="sidebarAccountLoginId"', DASHBOARD)
        self.assertIn('id="logoutBtn"', DASHBOARD)
        self.assertIn('.sidebar-body > .sidebar-account {', CSS)
        self.assertIn('grid-template-columns: minmax(0, 1fr) 30%;', CSS)
        self.assertIn('position: static;', CSS)
        self.assertIn('.sidebar-body > .sidebar-account > .sidebar-logout-btn {', CSS)
        self.assertIn('grid-row: 1 / 3;', CSS)
        self.assertIn('overflow-wrap: anywhere;', CSS)

    def test_server_pi_switch_has_no_extra_outer_box(self):
        self.assertIn('.system-control-panel .system-control-switch {', CSS)
        self.assertIn('  border: 0;', CSS)
        self.assertIn('  background: transparent;', CSS)
        self.assertIn('.system-control-panel .system-control-switch::after {', CSS)
        self.assertIn('  content: none;', CSS)
        self.assertIn('.system-control-switch .ui-process-track', CSS)
        self.assertIn('  min-width:62px;padding:5px 7px;', CSS)
        self.assertIn("button.disabled = pending || unreachable;", SYSTEM)

    def test_model_toggle_not_augmented_by_global_loader(self):
        self.assertIn("if (element.matches('.dashboard-model-toggle')) return;", VISUAL)
        self.assertIn("element.prepend(dots);", VISUAL)
        model = source("frontend/components/controls/model_pipeline_toggle.js")
        self.assertIn("button.append(label, offLabel, track, onLabel, stateLabel);", model)
        self.assertIn("button.disabled = this.connected !== true || this.pending;", model)
        self.assertIn("return this.handlers.request?.(next);", model)

    def test_led_control_is_centered_without_changing_its_hit_area(self):
        self.assertIn('grid-template-columns: minmax(0, 1fr) 30px 48px 30px minmax(0, 1fr);', CSS)
        self.assertIn('.dashboard-led-toggle,.dashboard-led-control-mount{justify-content:center;text-align:center;}', CSS)
        # Equal-length side tracks place LED, Auto and Mapping at the same center X.
        self.assertIn('column-gap: 4px;', CSS)
        self.assertIn('grid-template-columns: minmax(0, 1fr) 48px minmax(0, 1fr);',
                      source("frontend/services/static/style.css"))
        self.assertIn('button.append(label, offLabel, track, onLabel, stateLabel);', source("frontend/components/controls/led_toggle.js"))
        self.assertIn('return this.handlers.request?.(next);', source("frontend/components/controls/led_toggle.js"))

    def test_server_status_drawer_is_centered_and_hover_only(self):
        self.assertIn('<h2 class="card-title">Server status</h2>', DASHBOARD)
        self.assertIn('.server-state-card .card-title { text-transform: none; }', CSS)
        self.assertIn('  left: 50%;', CSS)
        self.assertIn('  transform: translateX(-50%);', CSS)
        self.assertIn('  border: 0;', CSS)
        self.assertIn('  background: transparent;', CSS)
        self.assertIn('  opacity: 0;', CSS)
        self.assertIn('.server-state-card:hover .server-state-drawer-toggle', CSS)
        self.assertIn('.server-state-card:focus-within .server-state-drawer-toggle', CSS)
        self.assertIn('@media (hover: none) { .server-state-drawer-toggle { opacity: 1; } }', CSS)
        self.assertIn('.server-state-card.is-collapsed .server-state-header .card-title,', CSS)
        self.assertIn('.server-state-card.is-collapsed .server-state-updated', CSS)
        self.assertIn('aria-expanded="true"', DASHBOARD)
        self.assertNotIn('title="Server status 접기"', DASHBOARD)
        self.assertNotIn('toggle.title=', VISUAL)
        self.assertIn("card.classList.toggle('is-collapsed')", VISUAL)
        self.assertIn("grid.setAttribute('aria-hidden', String(collapsed));", VISUAL)

    def test_record_toolbar_click_focus_has_no_indicator(self):
        # Keep click/hover animation, remove the post-click ring and underline.
        self.assertIn('.map-bookmark-panel .mb-btn:focus,', CSS)
        self.assertIn('.map-bookmark-panel .mb-btn:focus-visible {', CSS)
        self.assertIn('  outline: none;', CSS)
        self.assertIn('  text-decoration: none;', CSS)
        self.assertIn(':not(:disabled):hover {', CSS)
        self.assertIn(':not(:disabled):active {', CSS)
        self.assertIn('data-record-view="patrolModal"', DASHBOARD)
        self.assertIn('data-record-view="statusModal"', DASHBOARD)

    def test_layout_followup_guards(self):
        self.assertIn('saved-map-primary-actions', MAP)
        self.assertIn("primaryActions.append(mappingActions, actions);", MAP)
        self.assertIn("#topSystemStatusBar[data-open=\"false\"] .system-status-items{", CSS)
        self.assertIn("position:absolute!important;width:0;height:0;", CSS)
        self.assertIn(".ui-loading-visual.is-offline .ui-dots > b{display:none!important;}", CSS)
        self.assertIn(".records-pagination .records-page-number.is-current,", CSS)
        self.assertIn(".records-view .record-data-table thead th .record-sort-button{", CSS)

    def test_latest_ui_feedback(self):
        self.assertNotIn("observeStatus(lidar,", VISUAL)
        self.assertIn("#no-camera-msg .ui-dots { color:#fffff0!important; }", CSS)
        self.assertIn('data-action-description-row=', RECORDS)
        self.assertIn("records.openActionDescriptionDetail = function", RECORDS)
        self.assertIn("record-description-overlay", RECORDS)
        self.assertIn("document.addEventListener('keydown', onKeydown, true);", RECORDS)
        self.assertIn("records.activeActionDetail?.close();", RECORDS)
        self.assertIn('for="currentSituationDescription">상황 기록</label>', SITUATION)
        self.assertIn("mapColumn.append(pose);\n        mapColumn.append(list);", MAP)
        self.assertIn("grid-template-columns:repeat(5,25px)", CSS)

    def test_final_spacing_and_unsorted_alignment(self):
        self.assertIn('span[aria-hidden="true"]:empty', CSS)
        self.assertIn("position:absolute;right:0;top:50%", CSS)
        self.assertIn('data-modal-view="recordImageDetail"] .modal-body', CSS)
        self.assertIn("padding-bottom:8px;", CSS)
        self.assertIn(".records-page-number[aria-current=\"page\"]", CSS)
        self.assertIn("#topSystemStatusBar {top:16px;}", CSS)
        self.assertIn("font-size:11px!important;", CSS)

    def test_latest_map_and_viewer_refinements(self):
        self.assertIn("${labels.description}", MAP)
        self.assertNotIn("${labels.description} ${labels.selectRequired} ${labels.renameHint}", MAP)
        self.assertIn("const choice = create('div', 'saved-map-location-choice');", MAP)
        self.assertIn("selector.hidden = true;", MAP)
        self.assertIn("saved-map-location-option", CSS)
        self.assertIn("grid-template-columns:repeat(3,minmax(0,1fr))", CSS)
        self.assertIn("max-height:min(40vh,360px)!important", CSS)
        self.assertIn("transform:rotate(90deg);", CSS)
        self.assertIn("transform:rotate(270deg);", CSS)
        self.assertIn("border:0!important;border-radius:0!important;", CSS)
        led_source = source("frontend/components/controls/led_toggle.js")
        self.assertIn("enabled: false,", led_source)
        self.assertIn("this.enabled = typeof options.enabled === 'boolean'", led_source)
        self.assertIn("selector.hidden = true;", MAP)
        self.assertIn("syncPicker();", MAP)

    def test_legacy_first_paint_is_removed(self):
        self.assertIn('class="dashboard-model-toggle unknown"', DASHBOARD)
        self.assertIn('class="dashboard-led-toggle"', DASHBOARD)
        self.assertEqual(DASHBOARD.count('class="navigation-mode-toggle unknown"'), 2)
        self.assertIn("document.body.classList.add('dark-mode')", DASHBOARD)
        self.assertIn('id="dashboardUiStartupError"', DASHBOARD)
        self.assertIn("root.querySelector('.navigation-mode-toggle')", NAV_MODE)
        self.assertIn("root.querySelector('.dashboard-led-toggle')", source("frontend/components/controls/led_toggle.js"))
        self.assertIn("root.querySelector('.dashboard-model-toggle')", source("frontend/components/controls/model_pipeline_toggle.js"))
        self.assertIn("syncDarkModeSwitch();", source("frontend/services/static/script.js"))
        self.assertNotIn("if (saved === '1') toggleDarkMode();", source("frontend/services/static/script.js"))

    def test_asset_versions(self):
        self.assertIn("saved_map_modal.js?v=20261014-custom-list-v1", DASHBOARD)
        self.assertIn("current_situation.js?v=20261014-finish-ui-v2", DASHBOARD)
        self.assertIn("image_detail.js?v=20261014-compact-ui-v2", DASHBOARD)
        self.assertIn("pagination.js?v=20261014-numbered-pages-v1", DASHBOARD)
        self.assertIn("dashboard_state.js?v=20261014-batched-pages-v1", DASHBOARD)
        self.assertIn("lidar_3d_viewer.css?v=20261014-ux-refine-v17", DASHBOARD)
        self.assertIn("record_modal.js?v=20261014-detail-overlay-v1", DASHBOARD)
        self.assertIn("system_control.js?v=20261014-hide-pi-service-v1", DASHBOARD)
        self.assertIn("record_table.js?v=20261014-ui-polish-v1", DASHBOARD)
        self.assertIn("navigation_mode_control.js?v=20261014-stable-hydration-v1", DASHBOARD)
        self.assertIn('selected_ui.js?v=20261014-stable-hydration-v1', DASHBOARD)
        self.assertIn('selected_ui.css?v=20261014-stable-hydration-v1', DASHBOARD)
        self.assertIn('selected_ui.css?v=20261014-stable-hydration-v1', LOGIN)


if __name__ == "__main__":
    unittest.main()
