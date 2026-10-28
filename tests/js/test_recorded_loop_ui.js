#!/usr/bin/env node
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "../..");
const html = fs.readFileSync(path.join(root, "frontend/templates/index.html"), "utf8");
const ui = fs.readFileSync(path.join(root, "frontend/services/static/recorded_loop_ui.js"), "utf8");
const css = fs.readFileSync(path.join(root, "frontend/services/static/recorded_loop_ui.css"), "utf8");
const sharedToggle = fs.readFileSync(path.join(root, "frontend/components/controls/navigation_mode_control.js"), "utf8");
const sharedDrawer = fs.readFileSync(path.join(root, "frontend/components/controls/drawer_toggle.js"), "utf8");

assert.ok(!html.includes('id="loop-modal-trigger"'), "LOOP button is removed");
assert.ok(html.includes('id="loopDrawerToggle"'), "Right-edge handle is present");
assert.ok(html.includes('dashboard-drawer-toggle--right'), "Matches existing edge handle component");
assert.ok(html.includes('data-drawer-target="loop-side-drawer"'));
assert.ok(html.includes('id="loop-side-drawer"'));
assert.ok(html.includes('data-open="false"'), "Drawer should start closed");
assert.ok(html.includes('id="loop-side-drawer-content" aria-hidden="true" inert'),
  "Closed drawer hides only contents, not the reopen handle");
assert.ok(!html.includes('id="loop-drawer-close"'), "No extra close button");
assert.ok(!html.includes('aria-controls="commonModal">LOOP'));
assert.ok(!html.includes('id="recordedLoopPanel"'));
for (const id of [
  "loop-map", "loop-mode", "loop-connection", "loop-goals", "loop-bags",
  "loop-record", "loop-save", "loop-delete", "loop-start", "loop-stop", "loop-status",
  "loop-obstacle-toggle-mount"
]) {
  assert.ok(html.includes('id="' + id + '"'), "Missing drawer control: " + id);
}
for (const id of ["loop-record", "loop-save", "loop-delete", "loop-start", "loop-stop"]) {
  assert.match(html, new RegExp('id="' + id + '"[^>]*data-control-button'),
    "Use existing ControlButton for " + id);
}
assert.ok(html.includes('class="navigation-mode-toggle loop-obstacle-toggle"'),
  "Use existing NavigationModeToggle, not a plain button component");
assert.ok(html.includes('role="switch" aria-checked="false"'));
assert.ok(!html.includes('id="loop-policy-toggle"'));
assert.ok(!ui.includes("CycleFilterButton"));
assert.ok(!ui.includes("manager.register("));
assert.ok(ui.includes("drawerToggle"));
assert.ok(ui.includes("mountNavigationMode"));
assert.ok(ui.includes("isolated: true"));
assert.ok(sharedToggle.includes("settings.isolated === true"));
assert.ok(sharedDrawer.includes("controls.drawerToggle"));
assert.ok(css.includes('.loop-side-drawer[data-open="true"]'));
assert.ok(css.includes('right: 0; z-index: 39'));
assert.ok(css.includes('.loop-side-drawer-handle'));
assert.ok(css.includes('loop-side-drawer[data-open="false"] .loop-side-drawer-content'));
assert.ok(html.indexOf('id="loop-side-drawer"') < html.indexOf('id="lidarMapSelectBtn"'),
  "Drawer exists inside 3D viewer before the viewer action buttons");
assert.ok(css.includes('#loop-bags[hidden]'));
assert.ok(ui.includes('bagList.hidden = !showBagList;'));
assert.ok(ui.includes('bagList.disabled = !showBagList'));
assert.ok(html.includes("recorded_loop_ui.js?v=20261028-viewer-right-drawer-v5"));
process.stdout.write("Shared drawer and obstacle toggle component contract PASS\n");
