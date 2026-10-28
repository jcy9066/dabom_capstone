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
assert.ok(html.includes('id="loop-side-drawer-content" aria-hidden="true" inert hidden'),
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
assert.ok(css.includes('.loop-side-drawer-content[hidden]'));
assert.ok(ui.includes('drawerContent.hidden = !opened;'));
assert.ok(css.includes('right: 0;') && css.includes('z-index: 70;'),
  "LOOP drawer must remain above viewer overlays");
assert.ok(css.includes('.loop-side-drawer-handle'));
assert.ok(css.includes('loop-side-drawer[data-open="false"] .loop-side-drawer-content'));
assert.ok(html.indexOf('id="loop-side-drawer"') < html.indexOf('id="lidarMapSelectBtn"'),
  "Drawer exists inside 3D viewer before the viewer action buttons");
assert.ok(css.includes('#loop-bags[hidden]'));
assert.ok(ui.includes('bagList.hidden = !showBagList;'));
assert.ok(ui.includes('bagList.disabled = !showBagList'));
assert.ok(html.includes("recorded_loop_ui.js?v=20261028-drawer-close-v8"));
assert.ok(html.includes("recorded_loop_ui.css?v=20261028-visible-set-loop-v9"));
assert.ok(css.includes(".loop-side-drawer-content::-webkit-scrollbar"));
assert.ok(css.includes("scrollbar-width: none"));
assert.ok(css.includes("overflow-y: auto"));
assert.ok(css.includes(".loop-side-drawer .loop-side-drawer-handle[aria-expanded=\"true\"]"),
  "LOOP edge handle must stay visible in the open state");
assert.ok(css.includes("opacity: 1;") && css.includes("pointer-events: auto;"));
assert.ok(css.includes("scrollbar-width: none;"));
assert.ok(css.includes(".loop-controls-inline") && css.includes("grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr)"));
assert.ok(html.includes('class="loop-controls-inline"'), "Obstacle and Execution share a horizontal row");
assert.ok(html.includes('class="loop-map-status-row"'), "Map name and status share a row");
assert.ok(html.indexOf('id="loop-status"') < html.indexOf('id="loop-goals"'),
  "LOOP status belongs next to the Map name, not at the bottom");
assert.equal((html.match(/id="loop-status"/g) || []).length, 1);
assert.ok(!css.includes(".loop-modal-section {\n  box-sizing: border-box; min-width: 0;"),
  "Remove the old individually framed sections");
assert.ok(css.includes("border: 0;") && css.includes("background: transparent;"),
  "LOOP sections are inside one panel box");
assert.ok(ui.includes('drawer.dataset.open = "false";'),
  "LOOP must reset to collapsed on initialization");
process.stdout.write("Shared drawer and obstacle toggle component contract PASS\n");
