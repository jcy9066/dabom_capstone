#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "../..");
const shared = fs.readFileSync(path.join(root, "frontend/components/controls/drawer_toggle.js"), "utf8");
const loop = fs.readFileSync(path.join(root, "frontend/services/static/recorded_loop_ui.js"), "utf8");
const css = fs.readFileSync(path.join(root, "frontend/services/static/recorded_loop_ui.css"), "utf8");

function mockElement() {
  const attrs = {}, callbacks = {};
  return {
    attrs, callbacks, dataset: {}, disabled: false, inert: false,
    classList: { remove() {} },
    addEventListener(event, listener) { (callbacks[event] ||= []).push(listener); },
    setAttribute(name, value) { attrs[name] = String(value); },
    removeAttribute(name) { delete attrs[name]; },
    focus() {},
  };
}

async function run() {
  const handle = mockElement(), drawer = mockElement(), panel = mockElement();
  const callbacks = {}, ids = {
    loopDrawerToggle: handle,
    "loop-side-drawer": drawer,
    "loop-side-drawer-content": panel,
  };
  handle.id = "loopDrawerToggle";
  handle.dataset.drawerTarget = "loop-side-drawer";
  handle.dataset.drawerOpenLabel = "접기";
  handle.dataset.drawerClosedLabel = "펼치기";
  drawer.id = "loop-side-drawer";
  drawer.dataset.open = "true"; // External initialization restored a stale open state.
  const doc = {
    hidden: true, readyState: "loading",
    getElementById(id) { return ids[id] || mockElement(); },
    querySelectorAll(selector) { return selector === "[data-drawer-toggle]" ? [handle] : []; },
    querySelector(selector) { return selector.includes('data-drawer-target') ? handle : null; },
    addEventListener(event, listener) { (callbacks[event] ||= []).push(listener); },
    dispatchEvent(event) { for (const f of callbacks[event.type] || []) f(event); },
  };
  const win = {
    document: doc,
    DabomDashboardComponents: {
      controls: {
        controlButton: { enhanceAll() {} },
        mountNavigationMode() { return { sync() {} }; },
      },
    },
  };
  const context = {
    window: win, document: doc, CSS: { escape: x => x },
    CustomEvent: class { constructor(type, config) { this.type = type; this.detail = config?.detail; } },
    fetch: async () => { throw new Error("Pi offline"); },
    setInterval() {},
  };
  vm.runInNewContext(shared, context, { filename: "drawer_toggle.js" });
  vm.runInNewContext(loop, context, { filename: "recorded_loop_ui.js" });
  for (const callback of callbacks.DOMContentLoaded || []) callback();
  assert.equal(drawer.dataset.open, "false", "LOOP must initialize closed");
  assert.equal(handle.attrs["aria-expanded"], "false");
  assert.equal(panel.inert, true);
  assert.equal(panel.attrs["aria-hidden"], "true");

  const click = () => {
    for (const callback of handle.callbacks.click || []) callback({ preventDefault() {} });
  };
  click();
  assert.equal(drawer.dataset.open, "true", "Shared component must open LOOP");
  assert.equal(handle.attrs["aria-expanded"], "true");
  assert.equal(panel.inert, false);
  assert.equal(panel.attrs["aria-hidden"], "false");
  assert.match(css, /\.loop-side-drawer \.loop-side-drawer-handle\[aria-expanded="true"\][\s\S]*?\{\s*opacity:\s*1;/,
    "Handle must remain visible for close");

  click();
  assert.equal(drawer.dataset.open, "false", "The same handle must close LOOP");
  assert.equal(handle.attrs["aria-expanded"], "false");
  assert.equal(panel.inert, true);
  assert.equal(panel.attrs["aria-hidden"], "true");

  click();
  for (const callback of callbacks.keydown || []) {
    callback({ key: "Escape", preventDefault() {} });
  }
  assert.equal(drawer.dataset.open, "false", "Escape uses the real shared DrawerToggle");

  assert.match(css, /\.loop-side-drawer-content\s*\{[^}]*overflow-y:\s*auto;/);
  assert.match(css, /\.loop-side-drawer-content\s*\{[^}]*scrollbar-width:\s*none;/);
  assert.match(css, /\.loop-side-drawer-content::-webkit-scrollbar/);
  process.stdout.write("Real DrawerToggle: default closed, open, close, Escape, scrollbars PASS\n");
}

run().catch(error => { console.error(error); process.exitCode = 1; });
