"""Real Chromium smoke test for dashboard button visual feedback.

No Pi, microphone, speaker or Telegram APIs are invoked. The original
dashboard handlers remain untouched; only isolated button DOM is mounted.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend/services/static"

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright is not None, "Playwright is not installed")
class ActionPressChromiumTest(unittest.TestCase):
    def test_pressed_surface_survives_click_and_control_replacement(self):
        html = """
        <style>
          body { --card-bg2:#f5f7fa; --border-color:#a4afbe;
                 --accent:#49697b; --danger-color:#b43b4e;
                 --warning-color:#a57c3c; --success-color:#287658; }
          .dashboard-action-controls { display:grid;grid-template-rows:repeat(3,60px);width:190px;gap:8px; }
          .action-btn { display:flex;align-items:center;justify-content:center; }
        </style>
        <div class="dashboard-action-controls">
          <button id="beep" class="card auth-btn action-btn action-beep uiverse-press-button">Beep</button>
          <button id="warning" class="card auth-btn action-btn action-warning uiverse-press-button">경고</button>
          <button class="card auth-btn action-btn action-report uiverse-press-button">
            <span class="action-report-label">신고</span>
          </button>
        </div>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                page.add_style_tag(content=(STATIC / "action_feedback.css").read_text(encoding="utf-8"))
                page.add_style_tag(content=(STATIC / "uiverse_dashboard_controls.css").read_text(encoding="utf-8"))
                page.add_script_tag(content=(STATIC / "action_feedback.js").read_text(encoding="utf-8"))
                for name in ("beep", "warning"):
                    btn = page.locator(f"#{name}")
                    initial = btn.evaluate("(node) => getComputedStyle(node).backgroundColor")
                    btn.click()
                    self.assertTrue(btn.evaluate("(node) => node.classList.contains('uiverse-action-clicked')"))
                    self.assertIn("uiverse-button-depress",
                                  btn.evaluate("(node) => getComputedStyle(node).animationName"))
                    pressed = btn.evaluate("(node) => getComputedStyle(node).backgroundColor")
                    self.assertNotEqual(initial, pressed)
                    page.wait_for_timeout(720)
                    self.assertFalse(btn.evaluate("(node) => node.classList.contains('uiverse-action-clicked')"))

                # Replace the whole control panel after initialization.
                page.eval_on_selector(".dashboard-action-controls", """node => {
                    node.innerHTML = '<button id="remounted" class="action-btn action-beep uiverse-press-button">Beep</button>';
                }""")
                page.locator("#remounted").click()
                self.assertTrue(page.locator("#remounted").evaluate(
                    "(node) => node.classList.contains('uiverse-action-clicked')"))
                page.wait_for_timeout(720)
                page.emulate_media(reduced_motion="reduce")
                page.locator("#remounted").click()
                self.assertTrue(page.locator("#remounted").evaluate(
                    "(node) => node.classList.contains('uiverse-action-clicked')"))
                self.assertEqual(
                    "none", page.locator("#remounted").evaluate(
                        "(node) => getComputedStyle(node).animationName"))
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
