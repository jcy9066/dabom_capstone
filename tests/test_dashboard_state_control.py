from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "services" / "static"
CONTROLS = ROOT / "frontend" / "components" / "controls"
TEMPLATE = ROOT / "frontend" / "templates" / "index.html"


class DashboardStateControlContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (STATIC / "script.js").read_text(encoding="utf-8")
        cls.state = (STATIC / "dashboard_state.js").read_text(encoding="utf-8")
        cls.navigation = (STATIC / "navigation_control.js").read_text(encoding="utf-8")
        cls.drive = (CONTROLS / "drive_mode_control.js").read_text(encoding="utf-8")
        cls.template = TEMPLATE.read_text(encoding="utf-8")

    def test_existing_lever_is_the_only_dashboard_drive_mode_control(self):
        self.assertEqual(self.template.count('data-dashboard-control="drive-mode"'), 1)
        self.assertEqual(self.template.count('onclick="togglePatrolMode()"'), 1)
        self.assertNotIn("createButton(root.ownerDocument, '자동'", self.drive)
        self.assertNotIn("createButton(root.ownerDocument, '수동'", self.drive)
        self.assertIn("root.querySelector('[data-dashboard-control=\"drive-mode\"]')", self.drive)

    def test_navigation_control_state_is_the_only_control_state_source(self):
        self.assertNotIn("robotState: '/api/robots/pi-01'", self.state)
        self.assertNotIn("pollRobotState", self.state)
        self.assertNotIn("dabom:system-control-status", self.state)
        self.assertNotIn("dabom:robot-status", self.state)
        self.assertIn("dabom:navigation-control-state", self.state)
        self.assertIn("window.applyServerPatrolMode?.(payload?.robot_mode)", self.navigation)
        telemetry = self.script[
            self.script.index("function fetchRobotStatus()"):
            self.script.index("// 카메라 연결 상태 폴링")
        ]
        self.assertNotIn("applyServerPatrolMode", telemetry)
        self.assertIn("dabom:telemetry-status", telemetry)

    def test_mode_lever_uses_the_navigation_command_path(self):
        toggle = self.script[
            self.script.index("async function togglePatrolMode()"):
            self.script.index("window.moveRobot")
        ]
        self.assertIn("window.navigationControl?.requestDriveMode", toggle)
        self.assertNotIn("sendRobotCommand({", toggle)

    def test_keyboard_allows_arrows_only_and_blocks_interactive_contexts(self):
        drive_keys = self.script[
            self.script.index("const DRIVE_KEYS"):
            self.script.index("let pointerMoveInterval")
        ]
        for key in ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"):
            self.assertIn(f"'{key}'", drive_keys)
        for key in ("'w'", "'a'", "'s'", "'d'"):
            self.assertNotIn(key, drive_keys)
        for selector in (
            "element?.matches('input, textarea, select')",
            "element?.isContentEditable",
            "[contenteditable]:not([contenteditable=\"false\"])",
            "modal.dataset.modalView",
            "modal.style.display === 'flex'",
            "classList.contains('open')",
        ):
            self.assertIn(selector, self.script)
        self.assertIn("isKeyboardDrivingBlocked(event.target)", self.script)

    def test_keyboard_release_only_stops_an_active_keyboard_session(self):
        result = subprocess.run(
            ["node", "-e", r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync(
    'frontend/services/static/script.js',
    'utf8',
);
const declarations = source.slice(
    source.indexOf('const DRIVE_KEYS'),
    source.indexOf('let robotCommandCsrfPromise'),
);
const logicStart = source.indexOf('function normalizeDriveKey');
const pointerListener = source.indexOf("'pointerup'", logicStart);
const logic = source.slice(
    logicStart,
    source.lastIndexOf('document.addEventListener(', pointerListener),
);

class MockElement {
    constructor(kind = 'div') {
        this.kind = kind;
        this.isContentEditable = kind === 'contenteditable';
    }
    matches() {
        return ['input', 'textarea', 'select'].includes(this.kind);
    }
    closest() {
        return this.kind === 'contenteditable' ? this : null;
    }
}

function buildRuntime({ modalOpen = false, sidebarOpen = false } = {}) {
    const listeners = {};
    const moves = [];
    const stops = [];
    const modal = {
        dataset: { modalView: modalOpen ? 'records' : '' },
        style: { display: modalOpen ? 'flex' : 'none' },
        classList: { contains: name => modalOpen && name === 'open' },
    };
    const sidebar = {
        classList: { contains: name => sidebarOpen && name === 'open' },
    };
    const document = {
        activeElement: new MockElement(),
        addEventListener: (type, handler) => { listeners[type] = handler; },
        getElementById: id => id === 'commonModal' ? modal : sidebar,
        querySelectorAll: () => [],
    };
    const context = {
        console,
        document,
        Element: MockElement,
        currentPatrolMode: 'manual',
        COMMAND_REPEAT_MS: 120,
        moveRobot: direction => moves.push(direction),
        stopRobot: reason => stops.push(reason),
        stopPointerMove: () => {},
        setInterval: () => 1,
        clearInterval: () => {},
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(`${declarations}\n${logic}`, context);
    return { context, listeners, moves, stops };
}

function keyEvent(key, target = new MockElement()) {
    return { key, target, preventDefault: () => {} };
}

for (const kind of ['input', 'textarea', 'select', 'contenteditable']) {
    const runtime = buildRuntime();
    runtime.listeners.keydown(keyEvent('ArrowUp', new MockElement(kind)));
    runtime.listeners.keyup(keyEvent('ArrowUp', new MockElement(kind)));
    assert.deepStrictEqual(runtime.moves, []);
    assert.deepStrictEqual(runtime.stops, []);
}
for (const state of [{ modalOpen: true }, { sidebarOpen: true }]) {
    const runtime = buildRuntime(state);
    runtime.listeners.keydown(keyEvent('ArrowUp'));
    runtime.listeners.keyup(keyEvent('ArrowUp'));
    assert.deepStrictEqual(runtime.moves, []);
    assert.deepStrictEqual(runtime.stops, []);
}

const wasd = buildRuntime();
wasd.listeners.keydown(keyEvent('w'));
wasd.listeners.keyup(keyEvent('w'));
assert.deepStrictEqual(wasd.moves, []);
assert.deepStrictEqual(wasd.stops, []);

const active = buildRuntime();
active.listeners.keydown(keyEvent('ArrowUp'));
active.listeners.keyup(keyEvent('ArrowUp'));
assert.deepStrictEqual(active.moves, ['\u2191']);
assert.deepStrictEqual(active.stops, ['key_release']);

const cancelled = buildRuntime();
cancelled.listeners.keydown(keyEvent('ArrowUp'));
cancelled.context.stopAllLocalInputs(true, 'test_cancel');
cancelled.listeners.keyup(keyEvent('ArrowUp'));
assert.deepStrictEqual(cancelled.moves, ['\u2191']);
assert.deepStrictEqual(cancelled.stops, ['test_cancel']);

const diagonal = buildRuntime();
diagonal.listeners.keydown(keyEvent('ArrowUp'));
diagonal.listeners.keydown(keyEvent('ArrowLeft'));
diagonal.listeners.keyup(keyEvent('ArrowLeft'));
diagonal.listeners.keyup(keyEvent('ArrowUp'));
assert.deepStrictEqual(diagonal.moves, ['\u2191', '\u2196', '\u2191']);
assert.deepStrictEqual(diagonal.stops, ['key_release']);
"""],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dpad_and_arrow_commands_follow_the_visible_direction(self):
        expected = {
            "↑": "forward",
            "↓": "backward",
            "←": "rotate_left",
            "→": "rotate_right",
            "↖": "forward_left",
            "↗": "forward_right",
            "↙": "backward_left",
            "↘": "backward_right",
        }
        mapping = self.script[
            self.script.index("function directionToCommand"):
            self.script.index("function robotCommandCsrfToken")
        ]
        for glyph, command in expected.items():
            self.assertIn(f"'{glyph}': '{command}'", mapping)

    def test_warning_is_connection_aware_but_report_is_independent(self):
        self.assertIn("document.querySelector('.action-warning')", self.navigation)
        self.assertIn("payload.connected !== true || state.warningPending", self.navigation)
        self.assertIn("if (state.control?.connected !== true)", self.navigation)
        self.assertNotIn(".action-report", self.navigation)
        self.assertIn("document.querySelector('.action-report')", self.script)
        self.assertNotIn("currentRobotConnected", self.script[
            self.script.index("async function reportDanger"):
            self.script.index("function warnTrespasser")
        ])

    def test_conflicts_have_user_facing_messages(self):
        for code in (
            "PI_OFFLINE",
            "DRIVING_MODE_REQUIRED",
            "NAVIGATION_NOT_READY",
            "AUTO_MODE_REQUIRED",
            "GOAL_OUT_OF_BOUNDS",
        ):
            self.assertIn(f"{code}:", self.navigation)
        self.assertIn("현재 Navigation 상태와 요청이 충돌했습니다.", self.navigation)
        self.assertIn("Pi가 연결되지 않아 명령을 전달하지 못했습니다.", self.script)

    def test_goal_uses_3d_marker_and_alert_clear_has_a_cutoff(self):
        viewer = (
            ROOT / "frontend/services/static/lidar_3d_viewer.js"
        ).read_text(encoding="utf-8")
        self.assertIn("createGoalMarker", viewer)
        self.assertIn("dabom:navigation-goal-draft", self.navigation)
        self.assertIn("dabom:alerts-cleared", self.script)
        self.assertIn("alertClearCutoffMs", self.state)
        self.assertIn("parsed.getTime() > alertClearCutoffMs", self.state)
        self.assertIn("state.hazardEntries = []", self.navigation)


if __name__ == "__main__":
    unittest.main()
