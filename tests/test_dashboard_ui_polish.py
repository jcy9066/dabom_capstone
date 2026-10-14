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
        self.assertIn('fieldset class="auth-protected-fields" id="protectedFields" disabled', LOGIN)
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

    def test_asset_versions(self):
        for name in ("selected_ui.js", "saved_map_modal.js",
                     "record_table.js", "current_situation.js", "navigation_mode_control.js",
                     "system_control.js", "image_detail.js"):
            with self.subTest(asset=name):
                self.assertIn(name + "?v=20261014-ui-polish-v1", DASHBOARD)
        self.assertIn('selected_ui.css?v=20261014-server-pi-toggle-v3', DASHBOARD)
        self.assertIn('selected_ui.css?v=20261014-ui-polish-v1', LOGIN)


if __name__ == "__main__":
    unittest.main()
