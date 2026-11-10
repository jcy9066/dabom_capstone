#!/usr/bin/env node
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "../..");
const script = fs.readFileSync(path.join(root, "frontend/services/static/recorded_loop_ui.js"), "utf8");
function element(tag = "div") {
  const node = {
    tagName: tag.toUpperCase(), dataset: {}, children: [], attrs: {},
    listeners: {}, hidden: false, disabled: false, inert: false, textContent: "",
    classList: { remove() {} }, focus() {},
    addEventListener(name, fn) { this.listeners[name] = fn; },
    setAttribute(key, val) { this.attrs[key] = String(val); },
    removeAttribute(key) { delete this.attrs[key]; },
    appendChild(child) { this.children.push(child); return child; },
    append(...children) { children.forEach(child => this.appendChild(child)); },
    replaceChildren(...children) { this.children = [...children]; },
  };
  if (tag === "select") {
    Object.defineProperty(node, "options", { get() { return this.children; } });
    let selectedValue = "";
    Object.defineProperty(node, "value", {
      get() { return selectedValue || this.children[0]?.value || ""; },
      set(value) { selectedValue = value; },
    });
  }
  return node;
}
async function main() {
  const ids = ["loopDrawerToggle", "loop-side-drawer", "loop-side-drawer-content",
    "loop-obstacle-toggle-mount", "loop-map", "loop-mode", "loop-connection",
    "loop-goals", "loop-help", "loop-bags", "loop-record", "loop-save",
    "loop-delete", "loop-start", "loop-stop", "loop-status", "loop-feedback"];
  const nodes = Object.fromEntries(ids.map(id => [id, element(id === "loop-bags" ? "select" : "button")]));
  const drawer = nodes["loop-side-drawer"];
  const panel = nodes["loop-side-drawer-content"];
  drawer.dataset.open = "true"; // Simulate a stale restored state.
  panel.inert = true;
  const listeners = {};
  let polling = null, enhanced = 0, isolated = false, toggleController = null;
  let state = { ok: true, map_name: null, mode: "MANUAL", connected: false,
    navigation_mode: "MAPPING", goals: [], recordings: [], recording: false,
    playing: false, status: "IDLE", lap: 0 };
  const doc = {
    readyState: "loading", hidden: false,
    getElementById(id) { return nodes[id] || null; },
    createElement(tag) { return element(tag); },
    addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
    dispatchEvent(event) { for (const fn of listeners[event.type] || []) fn(event); },
  };
  const drawerToggle = {
    enhance(trigger) {
      trigger.addEventListener("click", () => this.setOpen("loop-side-drawer", drawer.dataset.open !== "true"));
    },
    setOpen(id, value) {
      assert.equal(id, "loop-side-drawer");
      drawer.dataset.open = value ? "true" : "false";
      doc.dispatchEvent({ type: "dabom:drawer-toggle", detail: { targetId: id, open: value } });
      return value;
    },
  };
  const controls = {
    drawerToggle,
    controlButton: { enhanceAll() { enhanced++; } },
    mountNavigationMode(mount, handlers, options) {
      assert.equal(mount, nodes["loop-obstacle-toggle-mount"]);
      isolated = options.isolated === true;
      const controller = {
        mode: "MAPPING",
        sync(mode) { this.mode = mode; },
        request(value) { handlers.request(value); },
      };
      toggleController = controller;
      return controller;
    },
  };
  const window = { DabomDashboardComponents: { controls }, confirm: () => true };
  const ctx = {
    document: doc, window,
    fetch: async url => {
      if (!url.endsWith("/state")) throw Error("Unexpected request");
      return { ok: true, json: async () => state };
    },
    setInterval(fn) { polling = fn; },
    CustomEvent: class { constructor(type, opts) { this.type = type; this.detail = opts?.detail; } },
  };
  vm.runInNewContext(script, ctx);
  for (const fn of listeners.DOMContentLoaded || []) fn();
  assert.equal(nodes["loopDrawerToggle"].disabled, false);
  assert.equal(drawer.dataset.open, "false");
  assert.equal(isolated, true);
  nodes["loopDrawerToggle"].listeners.click();
  assert.equal(drawer.dataset.open, "true", "LOOP opens without Pi");
  assert.equal(panel.inert, false);
  assert.equal(panel.hidden, false);
  assert.equal(panel.attrs["aria-hidden"], "false");
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(enhanced >= 1, "Shared ControlButton applied");
  assert.equal(nodes["loop-bags"].hidden, true, "Empty list completely hidden");
  assert.equal(nodes["loop-bags"].disabled, true);
  assert.equal(nodes["loop-bags"].children.length, 0);
  assert.equal(nodes["loop-bags"].attrs["aria-hidden"], "true");

  state = { ...state, mode: "MANUAL",
    recordings: [{ name: "Loop 1", events: 6, seconds: 0.3 }] };
  polling();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(nodes["loop-bags"].hidden, false);
  assert.equal(nodes["loop-bags"].children.length, 1);
  assert.equal(nodes["loop-bags"].value, "Loop 1");
  toggleController.request("DRIVING");
  assert.equal(toggleController.mode, "DRIVING", "Replan uses existing toggle control");

  state = { ...state, mode: "AUTO" };
  polling();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(nodes["loop-bags"].hidden, true);
  assert.equal(nodes["loop-bags"].disabled, true);

  nodes["loopDrawerToggle"].listeners.click();
  assert.equal(drawer.dataset.open, "false");
  assert.equal(panel.inert, true);
  assert.equal(panel.hidden, true);
  assert.equal(panel.attrs["aria-hidden"], "true");
  nodes["loopDrawerToggle"].listeners.click();
  assert.equal(drawer.dataset.open, "true");
  for (const fn of listeners.keydown || []) fn({ key: "Escape", preventDefault() {} });
  assert.equal(drawer.dataset.open, "false", "Escape closes drawer");
  process.stdout.write("LOOP drawer offline/open/close/empty-list/toggle PASS\n");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
