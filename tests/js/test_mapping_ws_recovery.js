'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('frontend/services/static/script.js', 'utf8');
const begin = source.indexOf('function requestNavigationStreamResync()');
const end = source.indexOf("\ndocument.addEventListener('dabom:navigation-control-state'", begin);
assert(begin > 0 && end > begin, 'visualization stream handler must be present');

const ctx = {
    navigationResyncRequired: false,
    navigationEventSeq: 0,
    navigationSnapshotInFlight: false,
    navigationSnapshotRefreshQueued: false,
    lidarState: {sessionId: 'map-a', pose: null},
    snapshotRequests: 0,
    emissions: 0,
    fetchNavigationSnapshot() { ctx.snapshotRequests += 1; },
    applyNavigationSnapshot(snapshot) {
        ctx.navigationEventSeq = snapshot.event_seq;
        ctx.navigationResyncRequired = false;
    },
    acceptNavigationPose(pose) { ctx.lidarState.pose = pose; return true; },
    acceptNavigationScan() {},
    clearNavigationScan() {},
    resetNavigationGlobalPath() {},
    emitNavigationVisualizationState() { ctx.emissions += 1; },
    applyNavigationMapPayload() {},
    navigationMapRevision: null,
    navigationMapDisplaySuppressed: false,
    navigationMapSuppressedRevision: null,
    navigationGlobalPathRunKey: null,
};
vm.createContext(ctx);
vm.runInContext(source.slice(begin, end), ctx);

ctx.applyNavigationStreamMessage({type: 'snapshot', snapshot: {event_seq: 10}});
ctx.applyNavigationStreamMessage({
    type: 'pose', session_id: 'map-a', event_seq: 11, pose: {x: 1},
});
assert.equal(ctx.lidarState.pose.x, 1);
ctx.applyNavigationStreamMessage({
    type: 'pose', session_id: 'map-a', event_seq: 13, pose: {x: 3},
});
assert.equal(ctx.snapshotRequests, 1, 'missing sequence must request snapshot');
assert.equal(ctx.lidarState.pose.x, 1, 'dropped-event state must not be applied');
assert.equal(ctx.navigationResyncRequired, true);
ctx.applyNavigationStreamMessage({type: 'snapshot', snapshot: {event_seq: 13}});
ctx.applyNavigationStreamMessage({
    type: 'pose', session_id: 'map-a', event_seq: 14, pose: {x: 4},
});
assert.equal(ctx.lidarState.pose.x, 4, 'stream resumes after snapshot');
ctx.applyNavigationStreamMessage({
    type: 'resync_required', event_seq: 15, session_id: 'map-a',
});
assert.equal(ctx.snapshotRequests, 2, 'overflow marker must request snapshot');
console.log('WebSocket event gap and overflow recovery: PASS');
