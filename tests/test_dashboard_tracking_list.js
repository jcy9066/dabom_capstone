// Regression tests for the live Now Object List (no real robot/browser needed).
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync('frontend/services/static/script.js', 'utf8');
new vm.Script(source); // Reject a syntax error anywhere in the dashboard script.
const first = source.indexOf('const OBJECT_LIST_POLL_MS');
const last = source.indexOf('// END TRACKED PEOPLE LIST', first);
assert(first >= 0 && last > first, 'tracking-list implementation is missing');
const block = source.slice(first, last);

function makeHarness(fetch) {
    const entries = [];
    const listeners = {};
    const timers = new Set();
    let visible = true;
    let id = 0;
    const list = {
        querySelectorAll: () => entries.slice(),
        appendChild: node => entries.push(node),
    };
    const document = {
        getElementById: name => name === 'detection-data' ? list : null,
        createElement: () => ({
            className: '', textContent: '',
            remove() {
                const at = entries.indexOf(this);
                if (at >= 0) entries.splice(at, 1);
            },
        }),
        addEventListener: (name, fn) => { listeners[name] = fn; },
    };
    class AbortController {
        constructor() { this.signal = { aborted: false }; }
        abort() { this.signal.aborted = true; }
    }
    const context = {
        document, fetch, AbortController,
        setInterval: fn => { const handle = ++id; timers.add(handle); return handle; },
        clearInterval: handle => timers.delete(handle),
        isDashboardDocumentVisible: () => visible,
    };
    vm.runInNewContext(block + '\nthis.api = { renderTrackedPeople, refreshTrackedPeople, stopTrackedPeoplePoll };', context);
    return {
        api: context.api, listeners, timers, entries,
        labels: () => entries.map(node => node.textContent),
        visible: state => { visible = state; },
    };
}
const tick = async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
};

(async () => {
    let counter = 0;
    const people = makeHarness(async () => ({
        ok: true, json: async () => ({
            ok: true, processed_at: Date.now() / 1000,
            detections: [{ id: 8, cls: 0 }, { id: 2, cls: 0 },
                { id: 8, cls: 0 }, { id: 4, cls: 1 }],
        }),
    }));
    await people.api.refreshTrackedPeople();
    assert.deepEqual(people.labels(), ['Person #2', 'Person #8']);
    people.api.renderTrackedPeople([{ id: 11, cls: 0 }]);
    assert.deepEqual(people.labels(), ['Person #11']);
    people.api.stopTrackedPeoplePoll();
    assert.deepEqual(people.labels(), []);
    assert.equal(people.timers.size, 0);

    const expired = makeHarness(async () => ({
        ok: true, json: async () => ({
            processed_at: Date.now() / 1000 - 12,
            detections: [{ id: 5, cls: 0 }],
        }),
    }));
    await expired.api.refreshTrackedPeople();
    assert.deepEqual(expired.labels(), []);

    for (const processed_at of [Date.now() / 1000 + 60, 'not-a-time', Infinity]) {
        const invalid = makeHarness(async () => ({ ok: true, json: async () => ({
            processed_at, detections: [{ id: 5, cls: 0 }],
        }) }));
        await invalid.api.refreshTrackedPeople();
        assert.deepEqual(invalid.labels(), []);
    }

    const requests = [];
    const racing = makeHarness(() => new Promise(resolve => requests.push(resolve)));
    // First request was launched during initialization.
    const old = racing.api.refreshTrackedPeople();
    const newest = racing.api.refreshTrackedPeople();
    assert.equal(requests.length, 3);
    requests[2]({ ok: true, json: async () => ({
        processed_at: Date.now() / 1000, detections: [{ id: 99, cls: 0 }],
    }) });
    await tick();
    requests[1]({ ok: true, json: async () => ({
        processed_at: Date.now() / 1000, detections: [{ id: 1, cls: 0 }],
    }) });
    await Promise.all([old, newest]);
    assert.deepEqual(racing.labels(), ['Person #99']);

    const hidden = racing.api.refreshTrackedPeople();
    racing.visible(false);
    racing.api.stopTrackedPeoplePoll();
    requests[3]({ ok: true, json: async () => ({
        processed_at: Date.now() / 1000, detections: [{ id: 44, cls: 0 }],
    }) });
    await hidden;
    assert.deepEqual(racing.labels(), []);
    racing.visible(true);
    racing.listeners.visibilitychange();
    assert.equal(racing.timers.size, 1);

    console.log('PASS: live tracking list, expiry, update order, and visibility');
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
