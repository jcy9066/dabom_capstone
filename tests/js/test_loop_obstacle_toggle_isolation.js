#!/usr/bin/env node
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(
  path.resolve(__dirname, "../../frontend/components/controls/navigation_mode_control.js"), "utf8");
function classes() { const values = new Set(); return {
  toggle(name, yes) { yes ? values.add(name) : values.delete(name); },
  contains(name) { return values.has(name); }
}; }
function fixture() {
  const mapping = { textContent: "Mapping", classList: classes() };
  const driving = { textContent: "Driving", classList: classes() };
  const track = {};
  const attributes = {}, listeners = {};
  const button = {
    classList: classes(), disabled: false,
    setAttribute(key, value) { attributes[key] = String(value); },
    addEventListener(name, fn) { listeners[name] = fn; },
    querySelector(selector) {
      if (selector.includes("MAPPING")) return mapping;
      if (selector.includes("DRIVING")) return driving;
      if (selector.includes("navigation-mode-toggle-track")) return track;
      return null;
    },
  };
  const root = {
    dataset: {},
    ownerDocument: { createElement() { throw Error("hydrated controls must be reused"); } },
    replaceChildren() { throw Error("unexpected replacement"); },
    querySelector(selector) { return selector === ".navigation-mode-toggle" ? button : null; },
  };
  return { root, button, mapping, driving, attributes, listeners };
}
const window = { DabomDashboardComponents: {} };
vm.runInNewContext(source, { window, document: {} });
const controls = window.DabomDashboardComponents.controls;
const obstacle = fixture();
let obstacleRequest = null;
const isolated = controls.mountNavigationMode(obstacle.root, {
  request(mode) {
    obstacleRequest = mode;
    isolated.sync(mode);
    return true;
  }
}, { isolated: true, labels: { MAPPING: "Wait", DRIVING: "Replan" } });
assert.equal(obstacle.mapping.textContent, "Wait");
assert.equal(obstacle.driving.textContent, "Replan");
assert.equal(controls.navigationModes.length, 0);
assert.equal(controls.navigationMode, undefined);
assert.equal(obstacle.attributes.role, "switch");
assert.equal(obstacle.attributes["aria-checked"], "false");
obstacle.listeners.click({
  preventDefault() {}, stopPropagation() {},
  target: { closest() { return null; } },
});
assert.equal(obstacleRequest, "DRIVING");
assert.equal(isolated.mode, "DRIVING");
assert.equal(obstacle.attributes["aria-checked"], "true");
assert.equal(obstacle.attributes["aria-label"], "Obstacle: Replan");
const nav = fixture();
const normal = controls.mountNavigationMode(nav.root, { request() { return true; } });
assert.equal(controls.navigationModes.length, 1);
assert.equal(controls.navigationMode, normal);
controls.syncNavigationMode("MAPPING");
assert.equal(normal.mode, "MAPPING");
assert.equal(isolated.mode, "DRIVING", "ROS navigation mode must not reset Wait/Replan");
process.stdout.write("Shared NavigationModeToggle isolation PASS\n");
