'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');

const viewer = fs.readFileSync('frontend/services/static/lidar_3d_viewer.js', 'utf8');
const control = fs.readFileSync('frontend/services/static/navigation_control.js', 'utf8');
const modal = fs.readFileSync('frontend/components/navigation/saved_map_modal.js', 'utf8');
const template = fs.readFileSync('frontend/templates/index.html', 'utf8');
const server = fs.readFileSync('server/navigation_control_api.py', 'utf8');
const mapApi = fs.readFileSync('server/navigation_map_api.py', 'utf8');

assert(template.includes('data-lidar-interaction="set-initial-pose"'));
assert(viewer.includes("mode === 'set-initial-pose'"));
assert(viewer.includes('dabom:initial-pose-draft'));
assert(control.includes("if (beginInitialPose(event)) return;"));
assert(control.includes("if (moveInitialPose(event)) return;"));
assert(control.includes("source: 'current'"));
assert(control.includes("map_revision: selection.mapRevision"));
assert(control.includes("warning_acknowledged: acknowledged"));
assert(modal.includes('saved-map-initialpose-canvas'));
assert(modal.includes('previewData.origin.yaw'));
assert(modal.includes('previewData.height - 1 - imageRow'));
assert(modal.includes('INITIAL_POSE_CONFIRMATION_REQUIRED'));
assert(mapApi.includes('MAP_REVISION_CHANGED'));
assert(server.includes('self._validate_live_initial_pose'));
assert(!server.includes('"x": 0.0, "y": 0.0, "yaw_degrees": 0.0'));
console.log('Initial Pose UI and server contract: PASS');
