"""Prevent primary-brand and layout regressions in the selected UI theme layer.

The original dev styles remain the source of truth for page surfaces and
primary buttons. The selected UI layer changes secondary/semantic accents only.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASE_CSS = (ROOT / "frontend/services/static/style.css").read_text(encoding="utf-8")
UI_CSS = (ROOT / "frontend/services/static/selected_ui.css").read_text(encoding="utf-8")
INDEX = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
LOGIN = (ROOT / "frontend/templates/login.html").read_text(encoding="utf-8")
SECTION = UI_CSS.split("Enterprise dashboard visual alignment", 1)[1]


def rule(text, selector):
    match = re.search(re.escape(selector) + r"\s*\{([^{}]+)\}", text)
    if match is None:
        raise AssertionError("CSS selector not found: " + selector)
    return match.group(1)


class DevPrimaryAccentContractTests(unittest.TestCase):
    def test_original_dev_primary_for_both_modes(self):
        self.assertIn("--primary-color: #2563eb;", rule(BASE_CSS, ":root"))
        self.assertIn("--primary-color: #3b82f6;", rule(BASE_CSS, "body.dark-mode"))
        self.assertNotRegex(SECTION, r"--primary-color\s*:")
        self.assertNotRegex(SECTION, r"--primary-glow\s*:")

    def test_original_dev_main_surfaces_are_inherited(self):
        protected = ("bg-color", "card-bg", "card-bg2", "text-dark",
                     "text-muted", "border-color", "sidebar-bg", "header-bg",
                     "table-head-bg", "input-bg", "alert-bg")
        for token in protected:
            with self.subTest(token=token):
                self.assertNotRegex(SECTION, r"--" + token + r"\s*:")

    def test_secondary_and_semantic_colors_are_theme_specific(self):
        light = rule(SECTION, ":root")
        dark = rule(SECTION, "body.dark-mode")
        palettes = (
            (light, {"accent": "#49697B", "success-color": "#287658",
                     "warning-color": "#9A641F", "danger-color": "#B43B4E"}),
            (dark, {"accent": "#9FBBC9", "success-color": "#7ECAA7",
                    "warning-color": "#E9B76C", "danger-color": "#F1808D"}),
        )
        for block, values in palettes:
            for name, color in values.items():
                with self.subTest(color=color):
                    self.assertIn(f"--{name}: {color};", block)

    def test_dark_primary_button_still_has_dev_contrast_text(self):
        self.assertIn("body.dark-mode.login-page .auth-btn.primary", SECTION)
        self.assertIn("body.dark-mode .signup-dialog .auth-btn.primary", SECTION)
        primary_rule = rule(SECTION, "body.dark-mode .signup-dialog .auth-btn.primary")
        self.assertIn("color: #161B22 !important;", primary_rule)

    def test_css_is_presentation_only_and_assets_are_fresh(self):
        for banned in ("fetch(", "addEventListener", "onclick=", "onkeydown="):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, SECTION)
        self.assertIn("selected_ui.css?v=20261014-final-spacing-v1", INDEX)
        self.assertIn("selected_ui.css?v=20261014-final-spacing-v1", LOGIN)


if __name__ == "__main__":
    unittest.main()
