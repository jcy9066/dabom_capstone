"""Visual integration contracts for Uiverse dashboard controls.

These tests are intentionally static: action requests must never be triggered by
a UI regression test, especially Telegram reporting or robot speech.
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
THEME_CSS = (ROOT / "frontend/services/static/uiverse_dashboard_controls.css").read_text(encoding="utf-8")
FEEDBACK_CSS = (ROOT / "frontend/services/static/action_feedback.css").read_text(encoding="utf-8")
FEEDBACK_JS = (ROOT / "frontend/services/static/action_feedback.js").read_text(encoding="utf-8")
DASHBOARD_JS = (ROOT / "frontend/services/static/script.js").read_text(encoding="utf-8")


class UiverseDashboardControlsTest(unittest.TestCase):
    def test_three_existing_action_buttons_reuse_press_surface(self):
        for role in ("beep", "warning", "report"):
            with self.subTest(role=role):
                self.assertRegex(
                    HTML,
                    rf'class="card auth-btn action-btn action-{role} uiverse-press-button"'
                )
        self.assertEqual(HTML.count('uiverse-press-button"'), 3)
        self.assertIn('id="dashboardBeepBtn"', HTML)
        self.assertIn('onclick="warnTrespasser()"', HTML)
        self.assertIn('onclick="reportDanger()"', HTML)
        self.assertIn("box-shadow: 0 3px 0 var(--uiverse-button-edge)", THEME_CSS)
        self.assertIn("transform: translateY(1px);", THEME_CSS)

    def test_telegram_feedback_states_and_markup_are_untouched(self):
        for part in (
            "action-report-content", "action-report-mail-icon",
            "action-report-success-icon", "action-report-error-icon",
            "action-report-label",
        ):
            with self.subTest(part=part):
                self.assertIn(part, HTML)
        for state in ("sending", "success", "error", "idle"):
            self.assertIn(state, FEEDBACK_JS)
        for caption in ("전송 중", "신고 완료", "전송 실패"):
            self.assertIn(caption, FEEDBACK_JS)
        self.assertIn("report.dataset.reportState = next", FEEDBACK_JS)
        self.assertIn("reportLabel.textContent = captions[next]", FEEDBACK_JS)
        self.assertIn("DabomActionFeedback?.setReportState('sending')", DASHBOARD_JS)
        self.assertIn("DabomActionFeedback?.setReportState('success')", DASHBOARD_JS)
        self.assertIn("DabomActionFeedback?.setReportState('error', error.message)", DASHBOARD_JS)
        self.assertIn("dashboard-report-message-send", FEEDBACK_CSS)
        self.assertNotIn(".action-report-content {", THEME_CSS)
        self.assertNotIn(".action-report-label {", THEME_CSS)

    def test_day_night_switch_preserves_theme_state_contract(self):
        self.assertEqual(HTML.count('id="darkToggle"'), 1)
        self.assertIn('role="switch" tabindex="0" aria-label="다크 모드"', HTML)
        self.assertIn('aria-checked="false" onclick="toggleDarkMode()"', HTML)
        self.assertIn("event.key==='Enter'||event.key===' '", HTML)
        self.assertIn('class="uiverse-toggle-scene" aria-hidden="true"', HTML)
        for count, cls in ((3, "uiverse-moon-dot-"), (3, "uiverse-light-ray-"),
                           (6, "uiverse-cloud-"), (4, "uiverse-star-")):
            items = re.findall(r'class="[^"]*\b' + cls + r'\d+\b', HTML)
            self.assertEqual(len(items), count, cls)
        self.assertIn("document.body.classList.toggle('dark-mode', darkMode)", DASHBOARD_JS)
        self.assertIn("setAttribute('aria-checked', String(darkMode))", DASHBOARD_JS)
        self.assertIn("localStorage.setItem('darkMode', darkMode ? '1' : '0')", DASHBOARD_JS)
        self.assertIn("#darkToggle.uiverse-day-night.active .uiverse-stars", THEME_CSS)
        self.assertIn("#darkToggle.uiverse-day-night.active .uiverse-toggle-scene", THEME_CSS)
        self.assertIn("@media (prefers-reduced-motion: reduce)", THEME_CSS)

    def test_equal_action_insets_and_click_animation(self):
        self.assertIn(".dashboard-action-controls.split-btn-container {", THEME_CSS)
        self.assertIn("document?.addEventListener('pointerdown'", FEEDBACK_JS)
        self.assertIn("document?.addEventListener('click'", FEEDBACK_JS)
        self.assertIn("button.closest('.dashboard-action-controls')", FEEDBACK_JS)
        self.assertIn("outline-offset: -3px;", THEME_CSS)
        self.assertIn("background: color-mix(in srgb, var(--uiverse-button-face) 64%, var(--uiverse-button-edge));", THEME_CSS)
        self.assertIn("padding: 4px 0 2px;", THEME_CSS)
        self.assertIn("row-gap: 8px;", THEME_CSS)
        self.assertIn("uiverse-action-clicked", FEEDBACK_JS)
        self.assertIn("pressTimers.set(button, global.setTimeout", FEEDBACK_JS)
        self.assertIn("}, 680));", FEEDBACK_JS)
        self.assertIn("controls.addEventListener('pointerdown'", FEEDBACK_JS)
        self.assertIn("@keyframes uiverse-button-depress", THEME_CSS)
        self.assertIn("@keyframes uiverse-press-flash", THEME_CSS)
        self.assertIn("transform: translateY(1px);", THEME_CSS)
        self.assertNotIn("if (global.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches) return;", FEEDBACK_JS)
        self.assertIn("uiverse-action-clicked {", THEME_CSS)
        self.assertIn("transform: translateY(1px);", THEME_CSS)
        self.assertIn("action-ripple-running", FEEDBACK_JS)
        self.assertIn("dashboard-action-ripple", FEEDBACK_CSS)

    def test_full_day_night_scene_and_eighty_one_percent_original_scale(self):
        # Clouds must move with the sun/moon, not remain fixed on the track.
        moon_content = HTML.split('class="dark-toggle-slider uiverse-sun-moon">', 1)[1].split("</div>", 1)[0]
        for i in range(1, 7):
            self.assertIn(f"uiverse-cloud-{i}", moon_content)
        for i in range(1, 4):
            self.assertIn(f"uiverse-moon-dot-{i}", moon_content)
            self.assertIn(f"uiverse-light-ray-{i}", moon_content)
        self.assertRegex(
            HTML, r'(?s)uiverse-cloud-6[^>]*>.*?</svg>\s*</div>\s*<div class="uiverse-stars">'
        )
        for prop in ("flex: 0 0 48.6px;", "width: 48.6px;", "height: 27.54px;",
                     "width: 60px;", "height: 34px;", "transform: scale(.81);",
                     "transform-origin: top left;", "transform: translateX(26px);",
                     "background: #2196f3;", "background: #000;"):
            with self.subTest(prop=prop):
                self.assertIn(prop, THEME_CSS)
        self.assertIn("animation: uiverse-moon-rotate .6s", THEME_CSS)
        self.assertIn("uiverse-cloud-drift", THEME_CSS)
        self.assertIn("uiverse-star-twinkle", THEME_CSS)

    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_beep_warning_ui_and_report_state_runtime(self):
        # A DOM stub verifies visual handlers without sending robot or Telegram commands.
        source = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const handlers = {};
const scheduled = new Map();
let nextId = 0;
const makeButton = () => {
    const names = new Set();
    return {
        disabled: false,
        classList: {
            add(...values) { values.forEach(name => names.add(name)); },
            remove(...values) { values.forEach(name => names.delete(name)); },
            contains(name) { return names.has(name); }
        },
        style: { setProperty() {} },
        offsetWidth: 100,
        closest() { return this; },
        getBoundingClientRect() { return { left: 0, top: 0, width: 120, height: 48 }; }
    };
};
const beep = makeButton();
const warning = makeButton();
const label = { textContent: '' };
const report = {
    dataset: {},
    querySelector() { return label; },
    setAttribute(name, value) { this[name] = value; },
    removeAttribute(name) { delete this[name]; }
};
const controls = {
    contains(node) { return [beep, warning, report].includes(node); },
    querySelector() { return report; },
    addEventListener(name, listener) { handlers[name] = listener; }
};
const context = {
    document: {
        querySelector() { return controls; },
        addEventListener(name, listener) { handlers[name] = listener; }
    },
    setTimeout(handler, delay) { const id = ++nextId; scheduled.set(id, { handler, delay }); return id; },
    clearTimeout(id) { scheduled.delete(id); },
    matchMedia() { return { matches: false }; },
    console
};
context.window = context;
vm.runInNewContext(fs.readFileSync('frontend/services/static/action_feedback.js', 'utf8'), context);
assert.ok(handlers.click);
assert.ok(handlers.pointerdown);
// A newly mounted button must work even though the original controls instance
// was captured during initialization; handlers live on document.
const remountedBeep = makeButton();
handlers.pointerdown({ target: remountedBeep, button: 0, detail: 1, clientX: 16, clientY: 8 });
assert.strictEqual(remountedBeep.classList.contains('uiverse-action-clicked'), true);
assert.strictEqual(label.textContent, '신고');
// Pointerdown must start the motion before the actual command callback.
handlers.pointerdown({ target: beep, button: 0, detail: 1, clientX: 30, clientY: 20 });
assert.strictEqual(beep.classList.contains('uiverse-action-clicked'), true);
assert.strictEqual(beep.classList.contains('action-ripple-running'), true);
const firstBeepId = [...scheduled.keys()][0];
handlers.click({ target: beep, detail: 1 });
assert.strictEqual(scheduled.has(firstBeepId), true, 'click must not restart pointer motion');
handlers.click({ target: beep, detail: 0 });
assert.strictEqual(scheduled.has(firstBeepId), false, 'keyboard click can restart motion');
// Warning keyboard click must also animate.
handlers.click({ target: warning, detail: 0 });
assert.strictEqual(warning.classList.contains('uiverse-action-clicked'), true);
assert.strictEqual(warning.classList.contains('action-ripple-running'), true);
for (const [id, timer] of [...scheduled]) {
    if (timer.delay === 680) {
        scheduled.delete(id);
        timer.handler();
    }
}
assert.strictEqual(beep.classList.contains('uiverse-action-clicked'), false);
assert.strictEqual(warning.classList.contains('uiverse-action-clicked'), false);
warning.disabled = true;
handlers.pointerdown({ target: warning, button: 0, detail: 1 });
handlers.click({ target: warning, detail: 0 });
assert.strictEqual(warning.classList.contains('uiverse-action-clicked'), false);
// Reduced motion still shows a static pressed state.
context.matchMedia = () => ({ matches: true });
handlers.click({ target: beep, detail: 0 });
assert.strictEqual(beep.classList.contains('uiverse-action-clicked'), true);
context.DabomActionFeedback.setReportState('sending');
assert.strictEqual(report.dataset.reportState, 'sending');
assert.strictEqual(label.textContent, '전송 중');
context.DabomActionFeedback.setReportState('success');
assert.strictEqual(report.dataset.reportState, 'success');
assert.strictEqual(label.textContent, '신고 완료');
context.DabomActionFeedback.setReportState('error', 'failed');
assert.strictEqual(label.textContent, '전송 실패');
assert.strictEqual(report.title, 'failed');
"""
        result = subprocess.run(
            ["node", "-e", source], cwd=ROOT,
            capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_style_only_and_cache_are_scoped(self):
        self.assertIn("uiverse_dashboard_controls.css?v=20261020-press-animation-v3", HTML)
        self.assertIn("action_feedback.js?v=20261020-pointer-press-v3", HTML)
        self.assertLess(HTML.index("action_feedback.css?v="), HTML.index("uiverse_dashboard_controls.css?v="))
        self.assertIn(".dashboard-action-controls .action-btn.uiverse-press-button", THEME_CSS)
        self.assertIn("#darkToggle.uiverse-day-night", THEME_CSS)
        self.assertNotIn("sendRobotCommand(", THEME_CSS)
        self.assertNotIn("/send_telegram", THEME_CSS)


if __name__ == "__main__":
    unittest.main()
