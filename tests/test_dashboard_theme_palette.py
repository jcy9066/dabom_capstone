"""Regression checks for color-only dashboard theme changes."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend/services/static"
CSS = [
    STATIC / "style.css",
    STATIC / "lidar_3d_viewer.css",
    STATIC / "navigation_control.css",
    STATIC / "navigation_map_control.css",
    STATIC / "system_control.css",
    STATIC / "login_auth.css",
    ROOT / "frontend/components/controls/controls.css",
    ROOT / "frontend/components/records/records.css",
    ROOT / "frontend/components/modal/modal.css",
]


def css_block(source: str, selector: str) -> str:
    pattern = r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}"
    match = re.search(pattern, source)
    if match is None:
        raise AssertionError(f"Missing CSS selector: {selector}")
    return match.group(1)


class ThemePaletteTests(unittest.TestCase):
    def test_light_and_dark_roles(self):
        source = (STATIC / "style.css").read_text(encoding="utf-8")
        light = css_block(source, ":root")
        dark = css_block(source, "body.dark-mode")
        light_values = {
            "--bg-color": "#F5F5F5",
            "--primary-color": "#65556B",
            "--accent": "#49697B",
            "--success-color": "#287658",
            "--warning-color": "#9A641F",
            "--danger-color": "#B43B4E",
        }
        dark_values = {
            "--bg-color": "#000000",
            "--primary-color": "#CEB6C7",
            "--accent": "#9FBBC9",
            "--success-color": "#7ECAA7",
            "--warning-color": "#E9B76C",
            "--danger-color": "#F1808D",
        }
        for name, value in light_values.items():
            with self.subTest(mode="light", token=name):
                self.assertIn(f"{name}: {value};", light)
        for name, value in dark_values.items():
            with self.subTest(mode="dark", token=name):
                self.assertIn(f"{name}: {value};", dark)

    def test_legacy_brand_colors_removed_from_css(self):
        old_hex = ("#2563eb", "#3b82f6", "#0891b2", "#22d3ee",
                   "#d97706", "#f59e0b", "#e11d48", "#f43f5e")
        for path in CSS:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8").lower()
                for old in old_hex:
                    self.assertNotIn(old, text, f"Legacy color in {path}")

    def test_semantic_status_controls(self):
        control_css = (ROOT / "frontend/components/controls/controls.css").read_text(encoding="utf-8")
        normal = css_block(control_css, ".dpad-estop-button")
        active = css_block(control_css, ".dpad-estop-button.active")
        unknown = css_block(control_css, ".dashboard-led-toggle.unknown .dashboard-led-toggle-state")
        self.assertIn("color: var(--danger-color);", normal)
        self.assertIn("color: var(--success-color);", active)
        self.assertIn("color: var(--text-muted);", unknown)
        records = (ROOT / "frontend/components/records/records.css").read_text(encoding="utf-8")
        self.assertIn("background: #F8ECD9;", css_block(records, ".record-event-system-error,\n.record-action-warning"))
        self.assertIn("color: #D1D0D0;", css_block(records, "body.dark-mode .record-action-note,\nbody.dark-mode .record-type-unknown"))

    def test_unchanged_theme_toggle_code_contract(self):
        script = (STATIC / "script.js").read_text(encoding="utf-8")
        self.assertIn("document.body.classList.toggle('dark-mode', darkMode)", script)
        self.assertIn("localStorage.setItem('darkMode'", script)
        self.assertIn("localStorage.getItem('darkMode')", script)

    def test_css_asset_cache_is_updated(self):
        for path in (ROOT / "frontend/templates/index.html", ROOT / "frontend/templates/login.html"):
            with self.subTest(page=path.name):
                html = path.read_text(encoding="utf-8")
                links = re.findall(r'href="[^"]+\.css\?v=([^"]+)"', html)
                self.assertTrue(links)
                self.assertTrue(all(x == "20261013-saltpepper-gothicnoir" for x in links))


if __name__ == "__main__":
    unittest.main()
