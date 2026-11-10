"""Mapping selection must recover an absent SLAM launch without resetting a live one."""

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_mapping_selection_checks_runtime_before_reusing_selected_mode():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the navigation behavior regression")
    javascript = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/services/static/navigation_control.js', 'utf8');
const start = source.indexOf('    function navigationIsMoving()');
const end = source.indexOf('    async function requestNavigationMode(');
assert(start >= 0 && end > start);
const functions = source.slice(start, end);

async function exercise(runtimeState, options = {}, mode = 'MAPPING') {
    const calls = [];
    const state = { control: { navigation_mode: mode, navigation_state: 'IDLE' } };
    const context = {
        state,
        window: { confirm: () => { throw new Error('Unexpected confirmation'); } },
        syncControlComponents: () => {},
        setFeedback: () => {},
        refreshState: async () => {},
        applyControlState: payload => { state.control = payload; },
        requestJson: async url => {
            calls.push(['GET', url]);
            return { ok: true, gpu: runtimeState === null ? [] : [
                { id: 'slam_mapping', state: runtimeState },
            ] };
        },
        mutate: async (url, payload) => {
            calls.push(['POST', url, payload]);
            return { navigation_mode: 'MAPPING', emergency_stop: false };
        },
    };
    vm.createContext(context);
    vm.runInContext(functions + '\nthis.run = setMappingMode;', context);
    const result = await context.run(options);
    assert.equal(state.busy, false);
    assert.equal(state.navigationPending, false);
    return { result, calls: JSON.parse(JSON.stringify(calls)) };
}

(async () => {
    const off = await exercise('off');
    assert.equal(off.result, true);
    assert.deepEqual(off.calls, [
        ['GET', '/api/system-control/status'],
        ['POST', '/api/navigation/control/mode', { mode: 'MAPPING' }],
    ]);
    for (const running of ['on', 'duplicate']) {
        const active = await exercise(running);
        assert.equal(active.result, true);
        assert.deepEqual(active.calls, [['GET', '/api/system-control/status']]);
    }
    for (const unavailable of [null, 'unreachable']) {
        const failed = await exercise(unavailable);
        assert.equal(failed.result, false);
        assert.equal(failed.calls.some(call => call[0] === 'POST'), false);
    }
    const restart = await exercise('on', { restart: true });
    assert.deepEqual(restart.calls, [
        ['POST', '/api/navigation/control/mode', { mode: 'MAPPING', restart: true }],
    ]);
    const driving = await exercise('off', {}, 'DRIVING');
    assert.equal(driving.result, true);
    assert.deepEqual(driving.calls, [
        ['POST', '/api/navigation/control/mode', { mode: 'MAPPING' }],
    ]);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", javascript], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
