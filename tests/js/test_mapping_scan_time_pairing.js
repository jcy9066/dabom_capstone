'use strict';
// Pure Node regression of the exact dashboard scan/pose acceptance functions.
// Does not require ROS2, hardware, or a browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('frontend/services/static/script.js', 'utf8');
const start = source.indexOf('function acceptNavigationPose(');
const end = source.indexOf('function noteScanUpdate(', start);
assert(start >= 0 && end > start, 'scan acceptance functions must exist');
const state = {
    scan: null, pose: null,
    lastScanKey: null, lastScanSeenAtMs: 0, scanIntervalsMs: [],
};
const sandbox = {
    lidarState: state,
    clearNavigationScan() { state.scan = null; },
    noteScanUpdate() {},
    Date,
    Number,
    Array,
};
vm.createContext(sandbox);
vm.runInContext(source.slice(start, end), sandbox);

function scan({source: src = 'lidar_ros_bridge', timestamp,
    received_at, sequence = null, matched = false, session_id = 'session-a'}) {
    return {
        source: src, timestamp, received_at, sequence, session_id,
        ranges: [1.0, 2.0],
        ...(matched ? {map_sensor_pose: {
            frame_id: 'map', sensor_frame_id: 'laser',
            x: 0, y: 0, z: 0, yaw: 0,
        }} : {}),
    };
}

const t1 = '2026-10-29T03:00:01.000Z';
const t2 = '2026-10-29T03:00:02.000Z';
const raw = scan({timestamp: t1, received_at: 100, sequence: 1});
assert.equal(sandbox.acceptNavigationScan(raw), true);
assert.equal(sandbox.acceptNavigationScan(raw), false, 'duplicate sequence');

const newer = scan({timestamp: t2, received_at: 101, sequence: 2});
assert.equal(sandbox.acceptNavigationScan(newer), true);
assert.equal(sandbox.acceptNavigationScan(raw), false, 'reversed timestamp');

const stamped = scan({
    source: 'map_bridge', timestamp: t1,
    received_at: 102, matched: true,
});
assert.equal(sandbox.acceptNavigationScan(stamped), true,
    'exact TF should replace an unmatched Pi scan');

const rawAfter = scan({timestamp: t2, received_at: 102.1, sequence: 3});
assert.equal(sandbox.acceptNavigationScan(rawAfter), false,
    'fresh matched TF must not be overwritten by an unpaired scan');
assert.equal(state.scan.source, 'map_bridge');

const rawAfterTimeout = scan({timestamp: t2, received_at: 104, sequence: 4});
assert.equal(sandbox.acceptNavigationScan(rawAfterTimeout), true,
    'after matched TF is stale, raw preview may resume');

const nextSession = scan({
    timestamp: t1, received_at: 105, sequence: 1, session_id: 'session-b',
});
assert.equal(sandbox.acceptNavigationScan(nextSession), true,
    'new session resets old scan sequence');

assert.equal(sandbox.acceptNavigationPose({
    timestamp: t2, session_id: 'session-b', x: 2, y: 1,
}), true);
assert.equal(sandbox.acceptNavigationPose({
    timestamp: t1, session_id: 'session-b', x: -4, y: 0,
}), false, 'reversed pose time');
assert.equal(state.pose.x, 2);

const bridge = fs.readFileSync(
    'navigation/ros/patrol_navigation/patrol_navigation/map_bridge.py', 'utf8'
);
assert(bridge.includes('Time.from_msg(msg.header.stamp)'),
    'ROS bridge must query TF at scan acquisition time');
assert(bridge.includes('"map_sensor_pose"'),
    'stamped map sensor pose must be transmitted');

console.log('scan/pose time pairing contracts: PASS');
