#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ROOT = path.resolve(__dirname, "../..");
const read = name => fs.readFileSync(path.join(ROOT, name), "utf8");
const markup = read("frontend/templates/index.html");
const styles = read("frontend/services/static/recorded_loop_ui.css");
const shared = read("frontend/components/controls/drawer_toggle.js");
const session = read("frontend/services/static/recorded_loop_ui.js");

assert.ok(!markup.includes('id="loop-modal-trigger"'), "No toolbar LOOP button");
assert.ok(markup.includes('id="loopDrawerToggle"'), "Right-edge handle");
assert.ok(markup.includes('dashboard-drawer-toggle--right'), "Shared drawer handle class");
assert.ok(markup.indexOf('id="lidar-layer-drawer"') <
    markup.indexOf('id="loop-side-drawer"'), "LOOP follows LAYERS");
assert.ok(markup.indexOf('id="loop-side-drawer"') <
    markup.indexOf('id="lidar-bottom-status"'), "LOOP is inside 3D viewer");
assert.ok(styles.includes(".loop-side-drawer-content::-webkit-scrollbar"),
    "Invisible WebKit drawer scrollbar");
assert.ok(styles.includes("scrollbar-width: none"), "Invisible Firefox scrollbar");
assert.ok(styles.includes("overflow-y: auto"), "Scroll remains enabled");

function element(tag = "div") {
  const attributes = {}, handlers = {}, children = [];
  const object = {
    dataset: {}, disabled: false, hidden: false, inert: false, style: {},
    attributes, handlers, children, textContent: "", title: "",
    classList: { remove() {} }, focus() {},
    setAttribute(name, value) { attributes[name] = String(value); },
    removeAttribute(name) { delete attributes[name]; },
    addEventListener(name, fn) { (handlers[name] ||= []).push(fn); },
    appendChild(item) { children.push(item); },
    append(...items) { children.push(...items); },
    replaceChildren(...items) { children.length = 0; children.push(...items); },
  };
  if (tag === "select") {
    Object.defineProperty(object, "options", { get() { return children; } });
    let selected = "";
    Object.defineProperty(object, "value", {
      get() { return selected || children[0]?.value || ""; },
      set(value) { selected = value; },
    });
  }
  return object;
}
async function main() {
  const ids = [
    "loopDrawerToggle", "loop-side-drawer", "loop-side-drawer-content",
    "loop-obstacle-toggle-mount", "loop-map", "loop-mode", "loop-connection",
    "loop-goals", "loop-help", "loop-bags", "loop-record", "loop-save",
    "loop-delete", "loop-start", "loop-stop", "loop-status", "loop-feedback",
  ];
  const nodes = Object.fromEntries(ids.map(id =>
    [id, element(id === "loop-bags" ? "select" : "div")]));
  const handle = nodes.loopDrawerToggle;
  const drawer = nodes["loop-side-drawer"];
  const contents = nodes["loop-side-drawer-content"];
  drawer.id = "loop-side-drawer";
  drawer.dataset.open = "false";
  contents.inert = true;
  handle.id = "loopDrawerToggle";
  handle.dataset.drawerTarget = "loop-side-drawer";
  handle.dataset.drawerOpenLabel = "LOOP 제어 패널 접기";
  handle.dataset.drawerClosedLabel = "LOOP 제어 패널 펼치기";
  const documentHandlers = {};
  const document = {
    hidden: false, readyState: "loading",
    getElementById(id) { return nodes[id] || null; },
    querySelectorAll(selector) {
      return selector === "[data-drawer-toggle]" ? [handle] : [];
    },
    querySelector(selector) {
      return selector.includes("loop-side-drawer") ? handle : null;
    },
    addEventListener(name, fn) {
      (documentHandlers[name] ||= []).push(fn);
    },
    dispatchEvent(event) {
      for (const fn of documentHandlers[event.type] || []) fn(event);
    },
    createElement(tag) { return element(tag); },
  };
  const window = {
    document,
    DabomDashboardComponents: {
      controls: {
        controlButton: { enhanceAll() {} },
        mountNavigationMode: () => ({ sync() {} }),
      },
    },
  };
  let state = {
    ok: true, map_name: null, mode: "MANUAL", navigation_mode: "MAPPING",
    connected: false, goals: [], recordings: [], playing: false,
    recording: false, status: "IDLE", lap: 0,
  };
  const context = {
    window, document, CSS: { escape: value => value },
    CustomEvent: class { constructor(type, options) {
      this.type = type; this.detail = options?.detail;
    }},
    fetch: async () => ({ ok: true, json: async () => state }),
    setInterval() {},
  };
  vm.runInNewContext(shared, context, { filename: "drawer_toggle.js" });
  vm.runInNewContext(session, context, { filename: "recorded_loop_ui.js" });
  for (const fn of documentHandlers.DOMContentLoaded || []) fn();

  assert.equal(handle.dataset.drawerToggleReady, "true", "Enhanced shared component");
  assert.equal(handle.handlers.click.length, 1, "No duplicate click handler");
  assert.equal(handle.attributes["aria-expanded"], "false");
  assert.equal(contents.inert, true);
  const press = () => handle.handlers.click[0]({ preventDefault() {} });

  press();
  assert.equal(drawer.dataset.open, "true", "First click opens");
  assert.equal(handle.attributes["aria-expanded"], "true");
  assert.equal(contents.inert, false);
  assert.equal(contents.attributes["aria-hidden"], "false");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(nodes["loop-bags"].hidden, true, "No empty recording dropdown");

  press();
  assert.equal(drawer.dataset.open, "false", "Second click closes");
  assert.equal(handle.attributes["aria-expanded"], "false");
  assert.equal(contents.inert, true);
  assert.equal(contents.attributes["aria-hidden"], "true");

  press();
  assert.equal(drawer.dataset.open, "true", "Third click reopens");
  for (const fn of documentHandlers.keydown || []) {
    fn({ key: "Escape", preventDefault() {} });
  }
  assert.equal(drawer.dataset.open, "false", "Escape closes via shared component");

  window.DabomDashboardComponents.controls.drawerToggle.setOpen("loop-side-drawer", true);
  assert.equal(drawer.dataset.open, "true", "Shared API opens");
  window.DabomDashboardComponents.controls.drawerToggle.setOpen("loop-side-drawer", false);
  assert.equal(drawer.dataset.open, "false", "Shared API closes");

  process.stdout.write("Real DrawerToggle integration: open/close/reopen/Escape/Pi offline PASS\n");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
