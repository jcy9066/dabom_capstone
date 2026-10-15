"""Presentation and safety contracts for the 2026-10-14 dashboard UI update.

These tests are static safety guards; interactive browser and real hardware
verification is still required before merging.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
def read(path):
    return (ROOT / path).read_text(encoding="utf-8")

CSS = read("frontend/services/static/selected_ui.css")
INDEX = read("frontend/templates/index.html")
LOGIN = read("frontend/templates/login.html")
RECORDS = read("frontend/components/records/record_modal.js")
FILTERS = read("frontend/components/records/record_filters.js")
GALLERY = read("frontend/components/gallery/image_detail.js")
DRIVE = read("frontend/components/controls/drive_mode_control.js")
MODEL = read("frontend/components/controls/model_pipeline_toggle.js")
LED = read("frontend/components/controls/led_toggle.js")
SYSTEM = read("frontend/services/static/system_control.js")
VISUAL = read("frontend/services/static/selected_ui.js")

class EnterpriseDashboardUiTests(unittest.TestCase):
    def test_device_status_keeps_both_time_filters(self):
        self.assertIn('data-filter="startAt"', RECORDS)
        self.assertIn('data-filter="endAt"', RECORDS)
        self.assertIn("return dateRange;", RECORDS)
        self.assertIn("readDateTimeRange(root)", RECORDS)
        self.assertIn("statusModal: Object.freeze({ startAt: '', endAt: '' })", FILTERS)

    def test_robot_actions_remain_mapped_to_original_handlers(self):
        self.assertIn("this.active ? 'RESUME' : 'STOP'", DRIVE)
        self.assertIn("return this.handlers.request?.(next);", MODEL)
        self.assertIn("return this.handlers.request?.(next);", LED)
        self.assertIn("control(group, component.id, action)", SYSTEM)
        self.assertIn("control(group, component.id, 'normalize')", SYSTEM)

    def test_gpu_disable_rules_match_the_existing_runtime(self):
        self.assertIn("component.control_available === false || component.state === 'unreachable'", SYSTEM)
        self.assertIn("button.disabled = pending || unreachable;", SYSTEM)
        self.assertIn("normalize.disabled = pending || unreachable;", SYSTEM)
        self.assertIn("system-control-switch.is-unavailable .ui-process-track", CSS)

    def test_control_state_labels_are_visual(self):
        self.assertIn("'PLAY' : 'STOP'", DRIVE)
        self.assertIn("ui-toggle-side-off", MODEL)
        self.assertIn("ui-toggle-side-on", MODEL)
        self.assertIn("ui-toggle-side-off", LED)
        self.assertIn("ui-toggle-side-on", LED)
        self.assertIn("ui-process-track", SYSTEM)

    def test_requested_relabeling_and_five_server_tiles(self):
        for label in ("순찰 내역", "조치 내역", "갤러리", "상황 기록", "기기 상태 내역"):
            self.assertIn(label, INDEX)
        self.assertIn("id=\"operation-mode-status\" hidden", INDEX)
        self.assertIn(".server-state-grid{grid-template-columns:repeat(5", CSS)
        self.assertIn("id=\"sidebarGuideMount\"", INDEX)

    def test_gallery_zoom_limits_and_navigation(self):
        self.assertIn("Math.min(4, Math.max(1", GALLERY)
        self.assertIn("viewport.addEventListener('wheel'", GALLERY)
        self.assertIn("viewport.addEventListener('pointermove'", GALLERY)
        self.assertIn("data-image-zoom-reset", GALLERY)
        self.assertIn("data-detail-delete", GALLERY)
        self.assertIn("data-detail-change", GALLERY)

    def test_loader_is_animated_and_offline_is_inactive(self):
        self.assertIn("@keyframes enterprise-loader-orbit", CSS)
        self.assertIn(".ui-loading-visual.is-offline .ui-dots>i {animation:none;}", CSS)
        self.assertIn("function installConnectionVisuals()", VISUAL)
        self.assertIn("function installExistingLoaders()", VISUAL)

    def test_records_keep_semantics(self):
        for event in ("INTRUSION", "ASSAULT", "SYSTEM_ERROR", "NETWORK_LOSS", "SENSOR_ANOMALY"):
            self.assertIn(event, RECORDS)
        self.assertIn("gpsPrecision(row.gps_lat)", RECORDS)
        self.assertIn("this.updateTitleCount()", RECORDS)

    def test_no_primary_background_replacement(self):
        self.assertIn("--accent: #49697B", CSS)
        self.assertIn("--accent: #9FBBC9", CSS)
        self.assertNotIn("--bg-color: #F5F5F5", CSS)
        self.assertNotIn("--bg-color: #000000", CSS)

    def test_stylesheet_cache_for_both_pages(self):
        self.assertIn("selected_ui.css?v=20261015-map-footer-tight-v6", INDEX)
        self.assertIn("selected_ui.css?v=20261015-map-footer-tight-v6", LOGIN)
        self.assertIn("selected_ui.js?v=20261014-shared-drawer-v2", INDEX)

if __name__ == "__main__":
    unittest.main()
