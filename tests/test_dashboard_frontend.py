from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest

from jinja2 import Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "frontend" / "templates"
STATIC_DIR = ROOT / "frontend" / "services" / "static"
COMPONENT_DIR = ROOT / "frontend" / "components"


class DashboardFrontendContractTests(unittest.TestCase):
    def setUp(self):
        self.template_source = (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")
        self.script_source = (STATIC_DIR / "script.js").read_text(encoding="utf-8")
        self.state_source = (STATIC_DIR / "dashboard_state.js").read_text(encoding="utf-8")
        self.style_source = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        self.records_source = (COMPONENT_DIR / "records" / "record_modal.js").read_text(encoding="utf-8")
        self.gallery_source = (COMPONENT_DIR / "gallery" / "image_detail.js").read_text(encoding="utf-8")
        self.current_situation_source = (
            COMPONENT_DIR / "current_situation" / "current_situation.js"
        ).read_text(encoding="utf-8")
        self.app_source = (ROOT / "server" / "app.py").read_text(encoding="utf-8")

    def render_dashboard(self, user):
        environment = Environment(
            loader=FileSystemLoader(TEMPLATE_DIR),
            autoescape=select_autoescape(("html",)),
        )
        return environment.get_template("index.html").render(
            request=SimpleNamespace(session={"user": user}),
            url_for=lambda _name: "/video_feed",
        )

    def run_node_component_test(self, source):
        completed = subprocess.run(
            ["node", "-e", source],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_sidebar_renders_authenticated_account_without_duplicating_logout(self):
        rendered = self.render_dashboard({"name": "테스트 관리자", "login_id": "dashboard_test"})

        self.assertIn("테스트 관리자", rendered)
        self.assertIn("dashboard_test", rendered)
        self.assertEqual(rendered.count('id="logoutBtn"'), 1)
        self.assertIn('id="systemControlPanelSlot"', rendered)

    def test_initial_robot_mode_is_unknown_and_manual_controls_are_disabled(self):
        self.assertIn('id="robot-mode-status">UNKNOWN</span>', self.template_source)
        self.assertIn('class="toggle-switch unknown"', self.template_source)
        self.assertIn('class="card d-pad-container disabled"', self.template_source)
        self.assertIn("applyServerPatrolMode(null)", self.script_source)

    def test_state_module_uses_navigation_control_and_optional_backend_contracts(self):
        self.assertIn("dabom:navigation-control-state", self.state_source)
        self.assertIn("applyControlState(event.detail)", self.state_source)
        self.assertNotIn("robotState: '/api/robots/pi-01'", self.state_source)
        self.assertNotIn("pollRobotState", self.state_source)
        self.assertIn("window.DABOM_DASHBOARD_ENDPOINTS", self.state_source)
        self.assertIn("fetchLogRows", self.state_source)
        self.assertIn("state: 'unavailable'", self.state_source)

    def test_control_state_disconnect_fails_closed(self):
        navigation_source = (STATIC_DIR / "navigation_control.js").read_text(encoding="utf-8")
        self.assertIn("currentRobotConnected !== false", self.script_source)
        self.assertIn("stopAllLocalInputs(false)", self.script_source)
        self.assertIn("window.setDashboardRobotConnection?.(payload?.connected === true)", navigation_source)
        self.assertIn("window.applyServerPatrolMode?.(payload?.robot_mode)", navigation_source)
        self.assertIn("payload.connected !== true || state.warningPending", navigation_source)

    def test_changed_assets_have_matching_cache_busters(self):
        version = "v=20260930-lidar3d-controls-v16"
        assets = (
            "static/style.css",
            "static/system_control.css",
            "static/navigation_map_control.css",
            "static/navigation_control.css",
            "static/script.js",
            "static/system_control.js",
            "static/navigation_map_control.js",
            "static/dashboard_state.js",
            "static/navigation_control.js",
        )
        for asset in assets:
            self.assertIn(f'{asset}?{version}', self.template_source)
        self.assertEqual(
            sum(self.template_source.count(f'{asset}?{version}') for asset in assets),
            len(assets),
        )
        self.assertNotIn("v=20260823-records-followup", self.template_source)

    def test_robot_commands_include_session_csrf_contract(self):
        self.assertIn("function robotCommandCsrfToken()", self.script_source)
        self.assertIn("'/api/auth/csrf'", self.script_source)
        self.assertIn("'X-CSRF-Token': csrfToken", self.script_source)
        self.assertIn("credentials: 'same-origin'", self.script_source)

    def test_existing_safety_and_logout_contracts_remain_present(self):
        for contract in (
            "button_release",
            "pointer_cancel",
            "key_release",
            "window_blur",
            "page_hidden",
            "/api/auth/logout",
        ):
            self.assertIn(contract, self.script_source)

    def test_state_script_loads_after_existing_state_producers(self):
        script_position = self.template_source.index("static/script.js")
        system_position = self.template_source.index("static/system_control.js")
        map_position = self.template_source.index("static/navigation_map_control.js")
        state_position = self.template_source.index("static/dashboard_state.js")

        self.assertLess(script_position, state_position)
        self.assertLess(system_position, state_position)
        self.assertLess(map_position, state_position)

    def test_dashboard_component_foundation_exposes_stable_mounts_and_assets(self):
        for mount_id in (
            "records-toolbar-mount",
            "dashboard-mode-controls-mount",
            "dpad-center-action-mount",
            "current-situation-mount",
        ):
            self.assertIn(f'id="{mount_id}"', self.template_source)

        component_assets = (
            "modal/modal_manager.js",
            "modal/modal.css",
            "navigation/saved_map_modal.js",
            "records/record_modal.js",
            "records/record_filters.js",
            "records/record_table.js",
            "records/pagination.js",
            "records/records.css",
            "controls/cycle_filter_button.js",
            "controls/drive_mode_control.js",
            "controls/navigation_mode_control.js",
            "controls/viewer_status.js",
            "controls/led_toggle.js",
            "controls/controls.css",
            "gallery/image_detail.js",
            "current_situation/current_situation.js",
        )
        for relative_path in component_assets:
            self.assertTrue((COMPONENT_DIR / relative_path).is_file(), relative_path)
            self.assertIn(f'/components/{relative_path}', self.template_source)

        modal_source = (COMPONENT_DIR / "modal" / "modal_manager.js").read_text(encoding="utf-8")
        self.assertIn("class ModalManager", modal_source)
        self.assertIn("components.modal", modal_source)
        self.assertIn("initializeDashboardComponentFoundation", self.script_source)

        record_entries = ('patrolModal', 'galleryModal', 'currentSituation', 'actionsModal', 'statusModal')
        mount_start = self.template_source.index('id="records-toolbar-mount"')
        mount_end = self.template_source.index("\n            </div>\n        </div>", mount_start)
        mount_source = self.template_source[mount_start:mount_end]
        self.assertEqual(self.template_source.count("data-record-view="), len(record_entries))
        positions = []
        for view in record_entries:
            marker = f'data-record-view="{view}"'
            self.assertIn(marker, mount_source)
            positions.append(mount_source.index(marker))
        self.assertEqual(positions, sorted(positions))
        for component_mount in (
            "components.records?.mount(components.mounts.recordsToolbar);",
            "components.navigationMaps?.mount({",
            "components.gallery?.mountImageDetail();",
            "components.currentSituation?.mount(components.mounts.currentSituation);",
        ):
            self.assertIn(component_mount, self.script_source)

    def test_legacy_record_gallery_and_current_situation_implementations_are_removed(self):
        for legacy_contract in (
            "const modalState =",
            "function buildModalHTML(",
            "function openCurrentSituationModal(",
            "function openGalleryModal(",
            "function openGalleryDetail(",
        ):
            self.assertNotIn(legacy_contract, self.script_source)
        self.assertIn("records.mount = function mountRecords", self.records_source)
        self.assertIn("gallery.registerModalViews = function registerModalViews", self.gallery_source)
        self.assertIn("currentSituation.mount = function mountCurrentSituation", self.current_situation_source)

    def test_dashboard_mode_group_separates_drive_and_navigation_clicks(self):
        mount_start = self.template_source.index('id="dashboard-mode-controls-mount"')
        mount_end = self.template_source.index('id="d-pad-area"', mount_start)
        mount_source = self.template_source[mount_start:mount_end]
        outer_open_tag = mount_source[:mount_source.index(">") + 1]

        self.assertNotIn("onclick=", outer_open_tag)
        self.assertEqual(mount_source.count('onclick="togglePatrolMode()"'), 1)
        self.assertIn('class="dashboard-drive-mode-control"', mount_source)
        self.assertIn('id="dashboard-navigation-mode-controls-mount"', mount_source)
        self.assertLess(
            mount_source.index("</button>"),
            mount_source.index('id="dashboard-navigation-mode-controls-mount"'),
        )
        self.assertNotIn('class="navigation-mode-option"', mount_source)
        self.assertNotIn('id="navigation-mode-mapping"', self.template_source)
        self.assertNotIn('id="navigation-mode-driving"', self.template_source)
        navigation_mode_source = (
            COMPONENT_DIR / "controls" / "navigation_mode_control.js"
        ).read_text(encoding="utf-8")
        self.assertIn("navigation-mode-toggle", navigation_mode_source)
        self.assertIn("mappingLabel.textContent = 'MAPPING'", navigation_mode_source)
        self.assertIn("drivingLabel.textContent = 'DRIVING'", navigation_mode_source)

    def test_dashboard_layout_contract_is_compact_and_consistent(self):
        toolbar_start = self.template_source.index('id="records-toolbar-mount"')
        toolbar_end = self.template_source.index("\n            </div>\n        </div>", toolbar_start)
        toolbar_source = self.template_source[toolbar_start:toolbar_end]
        toolbar_order = ('patrolModal', 'galleryModal', 'currentSituation', 'actionsModal', 'statusModal')
        positions = [toolbar_source.index(f'data-record-view="{view}"') for view in toolbar_order]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('data-record-view="patrolModal"] { order:', self.style_source)
        self.assertNotIn('.current-situation-record-btn { order:', self.style_source)

        for contract in (
            "grid-template-rows: repeat(3, minmax(0, 1fr))",
            "grid-column: 2;",
            "grid-row: 1 / 4;",
            ".d-pad-container { grid-column: 3; grid-row: 1 / 4;",
            "min-height: 32px",
            "--font-family: 'Noto Sans KR', 'Noto Sans', sans-serif",
        ):
            self.assertIn(contract, self.style_source)
        self.assertNotIn("Space Mono", self.style_source + self.template_source)
        for decorative_emoji in ("⚙️", "🌙", "📋", "📌", "🖼", "🚨", "🤖", "🎮"):
            self.assertNotIn(decorative_emoji, self.template_source)

    def test_modal_manager_restores_view_state_and_scroll_position(self):
        self.run_node_component_test(r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

class FakeNode {}
class FakeElement extends FakeNode {
    constructor() {
        super();
        this.style = {};
        this.textContent = '';
        this.innerHTML = '';
        this.scrollTop = 0;
        this.children = [];
    }
    replaceChildren(...children) {
        this.children = children;
        this.innerHTML = '';
    }
}

const elements = {
    '#commonModal': new FakeElement(),
    '#modalTitle': new FakeElement(),
    '#modalBody': new FakeElement(),
};
const context = {
    console,
    Node: FakeNode,
    document: { querySelector: selector => elements[selector] || null },
};
context.window = context;
vm.createContext(context);
vm.runInContext(
    fs.readFileSync('frontend/components/modal/modal_manager.js', 'utf8'),
    context,
    { filename: 'modal_manager.js' },
);

(async () => {
    let restoredState = null;
    const manager = context.DabomDashboardComponents.modal.mount();
    manager.register('records', {
        title: '기록',
        render: () => '<table>records</table>',
        captureState: () => ({ page: 3, sort: 'created_at' }),
        restoreState: state => { restoredState = state; },
    });
    manager.register('detail', {
        title: '상세',
        render: () => '<img alt="detail">',
    });

    await manager.open('records');
    elements['#modalBody'].scrollTop = 84;
    await manager.open('detail');
    assert.strictEqual(manager.stack.length, 1);
    assert.strictEqual(await manager.back(), true);
    assert.strictEqual(manager.activeView.name, 'records');
    assert.strictEqual(restoredState.page, 3);
    assert.strictEqual(restoredState.sort, 'created_at');
    assert.strictEqual(elements['#modalBody'].scrollTop, 84);
    assert.strictEqual(elements['#modalTitle'].textContent, '기록');
    manager.close();
    assert.strictEqual(elements['#commonModal'].style.display, 'none');
    assert.strictEqual(manager.stack.length, 0);
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
""")

    def test_component_filter_pagination_and_control_facades(self):
        self.run_node_component_test(r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const context = { console };
context.window = context;
vm.createContext(context);
for (const source of [
    'frontend/components/records/record_filters.js',
    'frontend/components/records/pagination.js',
    'frontend/components/records/record_modal.js',
    'frontend/components/controls/cycle_filter_button.js',
    'frontend/components/controls/drive_mode_control.js',
    'frontend/components/controls/navigation_mode_control.js',
]) {
    vm.runInContext(fs.readFileSync(source, 'utf8'), context, { filename: source });
}

const components = context.DabomDashboardComponents;
const filters = components.records.createFilterState({ type: 'all', reported: null });
assert.strictEqual(filters.update({ type: 'danger' }).type, 'danger');
assert.strictEqual(filters.values.reported, null);
assert.strictEqual(filters.reset().type, 'all');

const pagination = components.records.createPaginationState({ total: 101 });
assert.strictEqual(pagination.pageSize, 50);
assert.strictEqual(pagination.totalPages, 3);
assert.strictEqual(pagination.setPage(99), 3);
pagination.setTotal(1);
assert.strictEqual(pagination.page, 1);
pagination.setPage(1);
pagination.reset();
assert.strictEqual(pagination.page, 1);

let opened = null;
const registeredViews = [];
const modalManager = {
    activeView: null,
    stack: [],
    register: name => { registeredViews.push(name); },
    open: name => { opened = name; return name; },
    close: () => { opened = null; },
};
components.modal = { getDefault: () => modalManager };
const recordsRoot = { dataset: {} };
const records = components.records.mount(recordsRoot);
records.open('patrolModal');
assert.strictEqual(opened, 'patrolModal');
assert.strictEqual(recordsRoot.dataset.dashboardComponent, 'records-toolbar');
assert.ok(registeredViews.includes('patrolModal'));

let requestedDriveMode = null;
const driveRoot = { dataset: {} };
const drive = components.controls.mountDriveMode(driveRoot, {
    request: mode => { requestedDriveMode = mode; },
});
drive.request('AUTO');
drive.sync('MANUAL');
assert.strictEqual(requestedDriveMode, 'AUTO');
assert.strictEqual(driveRoot.dataset.driveMode, 'MANUAL');

let requestedNavigationMode = null;
const navigationRoot = { dataset: {} };
const navigation = components.controls.mountNavigationMode(navigationRoot, {
    request: mode => { requestedNavigationMode = mode; },
});
navigation.request('DRIVING');
navigation.sync('MAPPING');
assert.strictEqual(requestedNavigationMode, 'DRIVING');
assert.strictEqual(navigationRoot.dataset.navigationMode, 'MAPPING');

const listeners = {};
const cycleButton = {
    dataset: {},
    textContent: '',
    addEventListener: (name, handler) => { listeners[name] = handler; },
    removeEventListener: name => { delete listeners[name]; },
};
const cycle = new components.controls.CycleFilterButton(cycleButton, {
    options: [
        { label: '전체', value: '' },
        { label: '경고', value: 'WARNING' },
    ],
});
assert.strictEqual(cycleButton.textContent, '전체');
listeners.click();
assert.strictEqual(cycle.value, 'WARNING');
assert.strictEqual(cycleButton.textContent, '경고');
cycle.reset();
assert.strictEqual(cycle.value, '');
cycle.destroy();
assert.strictEqual(listeners.click, undefined);
""")
        self.assertEqual(self.records_source.count('class="filter-btn secondary" data-cycle='), 6)
        self.assertIn('class="filter-btn secondary" data-cycle="actionType"', self.records_source)
        self.assertNotIn('record-cycle-filter', self.records_source)

    def test_current_situation_frontend_matches_authenticated_json_api(self):
        for contract in (
            "/api/logs/current-situation/preview",
            "X-Frame-Token",
            "description_content: description",
            "include_image: includeImage",
            "frame_token: includeImage ? state.frameToken : null",
            "JSON.stringify",
            "response.status === 503",
        ):
            self.assertIn(contract, self.current_situation_source)
        self.assertIn('@app.post("/api/logs/current-situation/preview")', self.app_source)
        self.assertIn('@app.post("/api/logs/current-situation")', self.app_source)

    def test_gallery_filters_and_media_urls_match_backend_contract(self):
        for contract in (
            "/api/gallery?source=",
            'data-source="all"',
            'data-source="event"',
            'data-source="action"',
        ):
            self.assertIn(contract, self.gallery_source)
        for route in (
            '@app.get("/api/gallery")',
            '@app.get("/api/media/events/{event_id}")',
            '@app.get("/api/media/actions/{action_id}")',
        ):
            self.assertIn(route, self.app_source)

    def test_media_rendering_trusts_availability_contract_and_has_fallbacks(self):
        self.assertIn(
            "return item?.has_image && item?.image_url ? String(item.image_url) : '';",
            self.gallery_source,
        )
        self.assertNotIn("`/api/media/events/${encodeURIComponent(id)}`", self.gallery_source)
        self.assertNotIn("`/api/media/actions/${encodeURIComponent(id)}`", self.gallery_source)
        for contract in (
            "data-gallery-image",
            "gallery-media-fallback",
            "image.addEventListener('error'",
            "image.replaceWith(fallback)",
        ):
            self.assertIn(contract, self.gallery_source)
        self.assertIn("data-thumbnail-image", self.records_source)
        self.assertIn("button?.replaceWith(fallback)", self.records_source)
        self.assertIn("has_image: row.has_image === true && Boolean(imageUrl)", self.state_source)
        self.assertNotIn("row.image_path", self.state_source)

    def test_dashboard_log_panels_use_existing_backend_endpoints(self):
        self.assertIn("deviceLogs: '/api/logs/system-status'", self.state_source)
        self.assertIn("patrolLogs: '/api/logs/events'", self.state_source)
        self.assertIn("actionLogs: '/api/logs/actions'", self.state_source)
        for route in (
            '@app.get("/api/logs/system-status")',
            '@app.get("/api/logs/events")',
            '@app.get("/api/logs/actions")',
        ):
            self.assertIn(route, self.app_source)

    def test_log_time_filters_are_sent_to_backend_queries(self):
        for contract in (
            "query.set('start_at', filters.startAt)",
            "query.set('end_at', filters.endAt)",
        ):
            self.assertIn(contract, self.state_source)
        self.assertIn(
            "fetchLogRows(this.type, this.requestFilters())",
            self.records_source,
        )
        self.assertNotIn("st.allRows.filter(row =>", self.script_source)

    def test_patrol_records_support_detected_time_false_alarm_and_gallery_detail(self):
        self.assertIn(
            "source.detected_at, source.timestamp, source.created_at",
            self.state_source,
        )
        self.assertIn("is_false_alarm: row.is_false_alarm === true", self.state_source)
        for contract in (
            "/api/logs/events/${encodeURIComponent(row.event_id)}/false-alarm",
            "method: 'PATCH'",
            "JSON.stringify({ is_false_alarm: nextValue })",
            "components.gallery?.openRecordDetail?.",
        ):
            self.assertIn(contract, self.records_source)

    def test_header_is_compact_and_gallery_keeps_three_column_maximum(self):
        self.assertIn("min-height: 36px;", self.style_source)
        self.assertIn("margin: 0 0 6px;", self.style_source)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", self.style_source)

    def test_current_situation_has_no_browser_capture_or_binary_upload(self):
        for forbidden in ("getUserMedia", "toDataURL", "FormData", "multipart", "canvas"):
            self.assertNotIn(forbidden, self.current_situation_source)
        self.assertIn("'Content-Type': 'application/json'", self.current_situation_source)

    def test_gallery_delete_uses_frozen_contract_and_updates_detail_state(self):
        for contract in (
            "confirm('이 이미지를 갤러리에서 삭제하시겠습니까?')",
            "`/api/gallery/${source}/${encodeURIComponent(recordId)}`",
            "method: 'DELETE'",
            "credentials: 'same-origin'",
            "'X-CSRF-Token': csrf.csrf_token",
            "state.items.splice(removedIndex, 1)",
            "state.selectedIndex = -1",
            "await manager.back()",
        ):
            self.assertIn(contract, self.gallery_source)
        self.assertNotIn("loadGallery(state.source)", self.gallery_source)

    def test_gallery_action_source_wins_over_related_event_id(self):
        explicit_source = "if (item?.source === 'event' || item?.source === 'action') return item.source;"
        action_fallback = "if (item?.action_id != null) return 'action';"
        self.assertIn(explicit_source, self.gallery_source)
        self.assertIn(action_fallback, self.gallery_source)
        self.assertLess(self.gallery_source.index(explicit_source), self.gallery_source.index(action_fallback))
        self.assertIn("item?.action_id ?? item?.source_id ?? item?.id", self.gallery_source)
        self.assertIn("`/api/gallery/${source}/${encodeURIComponent(recordId)}`", self.gallery_source)


if __name__ == "__main__":
    unittest.main()
