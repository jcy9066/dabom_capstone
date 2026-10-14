"""Static regression checks for the compact selected-dashboard UI adaptation.

These tests do not replace Playwright or live Pi/ROS validation.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "frontend/services/static/selected_ui.css").read_text(encoding="utf-8")
JS = (ROOT / "frontend/services/static/selected_ui.js").read_text(encoding="utf-8")
INDEX = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
LOGIN = (ROOT / "frontend/templates/login.html").read_text(encoding="utf-8")
CYCLE = (ROOT / "frontend/components/controls/cycle_filter_button.js").read_text(encoding="utf-8")
DASHBOARD_JS = (ROOT / "frontend/services/static/script.js").read_text(encoding="utf-8")


class CompactSelectedUIRegressionTests(unittest.TestCase):
    def test_existing_theme_toggle_dimensions_preserved(self):
        self.assertRegex(CSS, r"width:44px;\s*min-width:44px;\s*height:24px")
        self.assertRegex(CSS, r"top:3px;\s*left:3px;\s*width:16px;\s*height:16px")
        self.assertIn("width:16px; height:1.5px", CSS)

    def test_existing_control_semantics_preserved(self):
        self.assertNotIn(".toggle-switch:not(.dark-toggle)", CSS)
        self.assertIn(".dashboard-model-toggle.active .dashboard-model-toggle-slider", CSS)
        self.assertIn(".dashboard-led-toggle.active .dashboard-led-toggle-slider", CSS)
        self.assertIn("localStorage.setItem('darkMode'", DASHBOARD_JS)

    def test_existing_filter_values_drive_active_appearance(self):
        self.assertIn('this.element.dataset.uiFilterSelected = String(this.index !== 0)', CYCLE)
        self.assertIn('[data-ui-filter-selected="true"]', CSS)
        self.assertIn(".record-filter-bar input[type=\"datetime-local\"]", CSS)

    def test_real_loading_states_covered(self):
        for selector in (".gallery-empty.is-loading", ".table-empty.is-loading",
                         ".saved-map-progress", ".record-delete-status"):
            with self.subTest(selector=selector):
                self.assertIn(selector, JS)
        self.assertIn(".ui-dots[hidden] { display:none !important; }", CSS)
        self.assertIn(".table-empty.is-loading::before { content:none; }", CSS)

    def test_selected_viewer_and_process_marks(self):
        self.assertIn(".lidar-view-controls button[data-lidar-view]", CSS)
        self.assertIn("position:absolute; z-index:0; box-sizing:border-box", CSS)
        self.assertIn(".lidar-display-options input[type=\"checkbox\"]", CSS)
        self.assertIn("text-decoration:none !important", CSS)
        self.assertIn(".system-control-switch.state-on::after", CSS)
        self.assertIn(".server-state-field strong.ui-state-marked", CSS)

    def test_pi_state_independent_of_camera(self):
        self.assertIn('id="pi-live-badge"', INDEX)
        self.assertIn("state.classList.contains('state-online')", JS)
        self.assertNotIn("badge.textContent = isLive ? 'LIVE'", DASHBOARD_JS)
        self.assertIn("setLiveBadge(false);", DASHBOARD_JS)

    def test_cache_versions_match_changed_assets(self):
        self.assertIn("selected_ui.css?v=20261014-hover-safe-v10", INDEX)
        self.assertIn("selected_ui.js?v=20261014-server-header-v3", INDEX)
        self.assertIn("selected_ui.css?v=20261014-hover-safe-v10", LOGIN)
        self.assertIn("cycle_filter_button.js?v=20261013-selected-ui-v3", INDEX)

    def test_no_react_runtime_needed(self):
        self.assertNotIn("react-dom", INDEX.lower())
        self.assertIn("prefers-reduced-motion", CSS)
        self.assertIn("ui-alert-enter", CSS)


if __name__ == "__main__":
    unittest.main()
