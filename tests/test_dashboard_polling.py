from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DashboardPollingRegressionTests(unittest.TestCase):
    def run_node(self, source):
        completed = subprocess.run(
            ["node", "-e", source],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_telemetry_and_camera_polling_timing_visibility_and_in_flight_guard(self):
        self.run_node(r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync('frontend/services/static/script.js', 'utf8').replace(/\r\n/g, '\n');
const polling = source.slice(
    source.indexOf('// 서버 상태 폴링'),
    source.indexOf('// 카메라 에러 처리'),
);
const visibilityStart = source.indexOf(
    "document.addEventListener(\n    'visibilitychange'",
);
const visibility = source.slice(
    visibilityStart,
    source.indexOf('// 사이드바 메뉴', visibilityStart),
);

class FakeElement {
    constructor() {
        this.innerText = '';
        this.textContent = '';
        this.style = {};
        this.classes = new Set();
        this.classList = {
            add: name => this.classes.add(name),
            remove: name => this.classes.delete(name),
            toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name),
            contains: name => this.classes.has(name),
        };
        this.listeners = {};
    }
    addEventListener(type, handler) {
        this.listeners[type] = handler;
    }
}

const elements = new Map([
    ['sys-cpu-usage', new FakeElement()],
    ['sys-cpu-temp', new FakeElement()],
    ['sys-ram', new FakeElement()],
    ['sys-internet', new FakeElement()],
    ['video-wrapper', new FakeElement()],
    ['camera-stream', new FakeElement()],
    ['no-camera-msg', new FakeElement()],
]);
const badge = new FakeElement();
const listeners = {};
const document = {
    hidden: false,
    visibilityState: 'visible',
    getElementById: id => elements.get(id) || null,
    querySelector: selector => selector === '.live-badge' ? badge : null,
    addEventListener: (type, handler) => {
        listeners[type] = listeners[type] || [];
        listeners[type].push(handler);
    },
    dispatchEvent: () => true,
};

let nextTimer = 1;
const timers = new Map();
const requests = [];
const active = new Map();
const maximumActive = new Map();
function setTimeoutFake(handler, delay) {
    const id = nextTimer++;
    timers.set(id, { handler, delay });
    return id;
}
function clearTimeoutFake(id) {
    timers.delete(id);
}
function fetchFake(url) {
    active.set(url, (active.get(url) || 0) + 1);
    maximumActive.set(url, Math.max(maximumActive.get(url) || 0, active.get(url)));
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    requests.push({ url, resolve, reject, settled: false });
    return promise;
}
function resolveRequest(request, payload) {
    assert.strictEqual(request.settled, false);
    request.settled = true;
    active.set(request.url, active.get(request.url) - 1);
    request.resolve({ ok: true, status: 200, json: async () => payload });
}
function requestCount(url) {
    return requests.filter(request => request.url === url).length;
}
function runTimersWithDelay(delay) {
    const matching = [...timers.entries()].filter(([, timer]) => timer.delay === delay);
    for (const [id, timer] of matching) {
        timers.delete(id);
        timer.handler();
    }
}
async function flush() {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise(resolve => setImmediate(resolve));
}

const context = {
    console,
    document,
    CustomEvent: class CustomEvent {
        constructor(type, options = {}) { this.type = type; this.detail = options.detail; }
    },
    fetch: fetchFake,
    setTimeout: setTimeoutFake,
    clearTimeout: clearTimeoutFake,
    currentPatrolMode: 'auto',
    emergencyStopRobot: () => {},
};
context.window = context;
vm.createContext(context);
vm.runInContext(`${polling}\n${visibility}`, context, { filename: 'script-polling.js' });

(async () => {
    assert.strictEqual(requestCount('/api/stream_status'), 1);
    assert.strictEqual(requestCount('/get_status'), 0);
    context.refreshTelemetryStatusPolling();
    assert.strictEqual(requestCount('/get_status'), 1);

    resolveRequest(requests.find(request => request.url === '/get_status'), {
        cpu_usage: '10', cpu_temp: '40', ram_usage: '20', internet: 'ok', mode: 'auto',
    });
    resolveRequest(requests.find(request => request.url === '/api/stream_status'), {
        camera_state: 'live', message: 'camera live',
    });
    await flush();
    assert.deepStrictEqual([...timers.values()].map(timer => timer.delay).sort(), [2000, 4000]);
    assert.strictEqual(badge.textContent, 'LIVE');
    assert.strictEqual(elements.get('camera-stream').style.display, '');

    document.hidden = true;
    document.visibilityState = 'hidden';
    listeners.visibilitychange[0]();
    assert.strictEqual(timers.size, 0);

    document.hidden = false;
    document.visibilityState = 'visible';
    listeners.visibilitychange[0]();
    assert.strictEqual(requestCount('/get_status'), 2);
    assert.strictEqual(requestCount('/api/stream_status'), 2);

    listeners.visibilitychange[0]();
    assert.strictEqual(requestCount('/get_status'), 2);
    assert.strictEqual(requestCount('/api/stream_status'), 2);
    const secondTelemetry = requests.filter(request => request.url === '/get_status')[1];
    const secondCamera = requests.filter(request => request.url === '/api/stream_status')[1];
    resolveRequest(secondTelemetry, {
        cpu_usage: '11', cpu_temp: '41', ram_usage: '21', internet: 'ok', mode: 'auto',
    });
    resolveRequest(secondCamera, { camera_state: 'offline', message: 'waiting' });
    await flush();
    assert.deepStrictEqual([...timers.values()].map(timer => timer.delay).sort(), [0, 0]);
    assert.strictEqual(badge.textContent, 'OFFLINE');

    runTimersWithDelay(0);
    assert.strictEqual(requestCount('/get_status'), 3);
    assert.strictEqual(requestCount('/api/stream_status'), 3);
    assert.strictEqual(maximumActive.get('/get_status'), 1);
    assert.strictEqual(maximumActive.get('/api/stream_status'), 1);
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
""")

    def test_system_control_polling_is_sidebar_aware_and_reuses_csrf(self):
        self.run_node(r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const elements = new Map();
class FakeElement {
    constructor(tag = 'div') {
        this.tag = tag;
        this.children = [];
        this.listeners = {};
        this.className = '';
        this.textContent = '';
        this.disabled = false;
        this.type = '';
        this.style = {};
        this.dataset = {};
        this._id = '';
        this.classes = new Set();
        this.classList = {
            contains: name => this.classes.has(name),
            add: name => this.classes.add(name),
            remove: name => this.classes.delete(name),
        };
    }
    set id(value) { this._id = value; if (value) elements.set(value, this); }
    get id() { return this._id; }
    get lastChild() { return this.children[this.children.length - 1] || null; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    addEventListener(type, handler) { this.listeners[type] = handler; }
}

let sidebarOpen = false;
const sidebar = new FakeElement();
sidebar.id = 'sidebar';
sidebar.classList.contains = name => name === 'open' && sidebarOpen;
const sidebarBody = new FakeElement();
const panelSlot = new FakeElement();
panelSlot.id = 'systemControlPanelSlot';
const documentListeners = {};
const document = {
    readyState: 'complete',
    hidden: false,
    visibilityState: 'visible',
    createElement: tag => new FakeElement(tag),
    querySelector: selector => selector === '.sidebar-body' ? sidebarBody : null,
    getElementById: id => elements.get(id) || null,
    addEventListener: (type, handler) => {
        documentListeners[type] = documentListeners[type] || [];
        documentListeners[type].push(handler);
    },
    dispatchEvent: event => {
        for (const handler of documentListeners[event.type] || []) handler(event);
        return true;
    },
};

let nextTimer = 1;
const timers = new Map();
function setTimeoutFake(handler, delay) {
    const id = nextTimer++;
    timers.set(id, { handler, delay });
    return id;
}
function clearTimeoutFake(id) { timers.delete(id); }
function runTimer(delay) {
    const entry = [...timers.entries()].find(([, timer]) => timer.delay === delay);
    assert.ok(entry, `missing timer ${delay}`);
    timers.delete(entry[0]);
    entry[1].handler();
}

const requests = [];
const active = new Map();
const maximumActive = new Map();
function fetchFake(url, options = {}) {
    active.set(url, (active.get(url) || 0) + 1);
    maximumActive.set(url, Math.max(maximumActive.get(url) || 0, active.get(url)));
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    requests.push({ url, options, resolve, reject, settled: false });
    return promise;
}
function resolveRequest(request, payload, status = 200) {
    assert.strictEqual(request.settled, false);
    request.settled = true;
    active.set(request.url, active.get(request.url) - 1);
    request.resolve({
        ok: status >= 200 && status < 300,
        status,
        json: async () => payload,
    });
}
function requestCount(url) {
    return requests.filter(request => request.url === url).length;
}
function latestPending(url) {
    return [...requests].reverse().find(request => request.url === url && !request.settled);
}
function findButton(root) {
    if (root.tag === 'button' && root.listeners.click) return root;
    for (const child of root.children) {
        const found = findButton(child);
        if (found) return found;
    }
    return null;
}
async function flush() {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise(resolve => setImmediate(resolve));
}
const statusPayload = {
    ok: true,
    updated_at: null,
    gpu: [{
        id: 'camera', label: 'Camera', description: '', state: 'off',
        instance_count: 0, pids: [], duplicate: false, control_available: true,
    }],
    pi: [],
};

const context = {
    console,
    document,
    CustomEvent: class CustomEvent {
        constructor(type, options = {}) { this.type = type; this.detail = options.detail; }
    },
    fetch: fetchFake,
    setTimeout: setTimeoutFake,
    clearTimeout: clearTimeoutFake,
    alert: () => {},
};
context.window = context;
vm.createContext(context);
vm.runInContext(
    fs.readFileSync('frontend/services/static/system_control.js', 'utf8'),
    context,
    { filename: 'system_control.js' },
);

(async () => {
    assert.strictEqual(requestCount('/api/system-control/status'), 0);
    sidebarOpen = true;
    document.dispatchEvent(new context.CustomEvent(
        'dabom:sidebar-visibility',
        { detail: { open: true } },
    ));
    assert.strictEqual(requestCount('/api/system-control/status'), 1);
    resolveRequest(latestPending('/api/system-control/status'), statusPayload);
    await flush();
    assert.deepStrictEqual([...timers.values()].map(timer => timer.delay).sort(), [0, 5000]);

    runTimer(5000);
    assert.strictEqual(requestCount('/api/system-control/status'), 2);
    document.dispatchEvent(new context.CustomEvent(
        'dabom:sidebar-visibility',
        { detail: { open: true } },
    ));
    assert.strictEqual(requestCount('/api/system-control/status'), 2);
    resolveRequest(latestPending('/api/system-control/status'), statusPayload);
    await flush();
    assert.strictEqual(requestCount('/api/system-control/status'), 3);
    resolveRequest(latestPending('/api/system-control/status'), statusPayload);
    await flush();
    assert.strictEqual(maximumActive.get('/api/system-control/status'), 1);

    document.hidden = true;
    document.visibilityState = 'hidden';
    document.dispatchEvent(new context.CustomEvent('visibilitychange'));
    assert.strictEqual(timers.size, 0);
    document.hidden = false;
    document.visibilityState = 'visible';
    document.dispatchEvent(new context.CustomEvent('visibilitychange'));
    assert.strictEqual(requestCount('/api/system-control/status'), 4);
    resolveRequest(latestPending('/api/system-control/status'), statusPayload);
    await flush();

    let button = findButton(elements.get('gpuSystemControls'));
    const firstAction = button.listeners.click();
    assert.strictEqual(requestCount('/api/auth/csrf'), 1);
    resolveRequest(latestPending('/api/auth/csrf'), { csrf_token: 'cached-token' });
    await flush();
    const firstPost = requests.find(request => request.url.includes('/api/system-control/gpu/camera/start'));
    assert.ok(firstPost);
    assert.strictEqual(firstPost.options.headers['X-CSRF-Token'], 'cached-token');
    resolveRequest(firstPost, { ok: true });
    await flush();
    assert.strictEqual(requestCount('/api/system-control/status'), 5);
    resolveRequest(latestPending('/api/system-control/status'), statusPayload);
    await firstAction;
    await flush();

    button = findButton(elements.get('gpuSystemControls'));
    const secondAction = button.listeners.click();
    await flush();
    assert.strictEqual(requestCount('/api/auth/csrf'), 1);
    const posts = requests.filter(request => request.url.includes('/api/system-control/gpu/camera/start'));
    assert.strictEqual(posts.length, 2);
    resolveRequest(posts[1], { ok: true });
    await flush();
    assert.strictEqual(requestCount('/api/system-control/status'), 6);
    resolveRequest(latestPending('/api/system-control/status'), statusPayload);
    await secondAction;
    await flush();

    sidebarOpen = false;
    document.dispatchEvent(new context.CustomEvent(
        'dabom:sidebar-visibility',
        { detail: { open: false } },
    ));
    assert.strictEqual(timers.size, 0);
    assert.strictEqual(maximumActive.get('/api/system-control/status'), 1);
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
""")


if __name__ == "__main__":
    unittest.main()
