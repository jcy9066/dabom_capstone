#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.resolve(__dirname, "../../frontend/services/static/recorded_loop_ui.js"), "utf8");

function node(tag = "div") {
  const self = {
    tagName: tag.toUpperCase(), dataset: {}, listeners: {}, attributes: {},
    hidden: false, disabled: false, textContent: "", children: [],
    addEventListener(type, callback) { this.listeners[type] = callback; },
    setAttribute(key, value) { this.attributes[key] = String(value); },
    removeAttribute(key) { delete this.attributes[key]; },
    appendChild(child) { this.children.push(child); return child; },
    append(...children) { children.forEach(child => this.appendChild(child)); },
    replaceChildren(...children) { this.children = [...children]; },
    classList: { remove() {} },
  };
  if (tag === "select") {
    Object.defineProperty(self, "options", { get() { return this.children; } });
    let selected = "";
    Object.defineProperty(self, "value", {
      get() { return selected || this.children[0]?.value || ""; },
      set(v) { selected = v; },
    });
  }
  return self;
}

async function main() {
  const ids = [
    "loop-modal-trigger", "loop-map", "loop-mode", "loop-connection",
    "loop-goals", "loop-help", "loop-bags", "loop-record", "loop-save",
    "loop-delete", "loop-policy-toggle", "loop-start", "loop-stop",
    "loop-status", "loop-feedback",
  ];
  const elements = Object.fromEntries(ids.map(id => [id, node(id === "loop-bags" ? "select" : "button")]));
  const events = {};
  let enhanced = 0;
  let polling = null;
  let data = {
    ok: true, map_name: "map_test", mode: "MANUAL", connected: false,
    navigation_mode: "DRIVING", goals: [], recordings: [], recording: false,
    playing: false, status: "IDLE", lap: 0,
  };
  const manager = {
    activeView: null, body: {},
    register(name, descriptor) { this.descriptor = descriptor; },
    async open(name) {
      this.activeView = { name };
      this.content = this.descriptor.render();
      await this.descriptor.onOpen();
    },
  };
  const document = {
    readyState: "loading", hidden: false,
    getElementById(id) { return elements[id] || (id === "commonModal" ? node() : null); },
    addEventListener(name, fn) { events[name] = fn; },
    dispatchEvent() {},
    createElement(tag) { return node(tag); },
  };
  const window = {
    DabomDashboardComponents: {
      modal: { getDefault: () => manager },
      controls: {
        controlButton: { enhanceAll() { enhanced++; } },
        CycleFilterButton: class {
          constructor(element, options) {
            element.dataset.value = options.options[options.index].value;
            element.addEventListener("click", () => options.onChange("REPLAN"));
          }
        },
      },
    },
  };
  const context = {
    document, window,
    fetch: async () => ({ ok: true, json: async () => data }),
    setInterval(fn) { polling = fn; },
    CustomEvent: class {},
  };
  vm.runInNewContext(script, context, { filename: "recorded_loop_ui.js" });
  events.DOMContentLoaded();
  await elements["loop-modal-trigger"].listeners.click();
  assert.ok(enhanced >= 1, "Shared ControlButton must be applied before API refresh");
  assert.equal(elements["loop-bags"].hidden, true, "Empty recordings must hide dropdown");
  assert.equal(elements["loop-bags"].disabled, true, "Empty dropdown must not open");
  assert.equal(elements["loop-bags"].options.length, 0, "Empty dropdown has no options");
  assert.equal(elements["loop-bags"].attributes["aria-hidden"], "true");
  assert.ok(manager.content.includes('class="loop-modal-grid"'));
  assert.equal((manager.content.match(/class="loop-modal-section"/g) || []).length, 3);

  data = { ...data, recordings: [{ name: "Loop 1", events: 7, seconds: 1.3 }] };
  polling();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements["loop-bags"].hidden, false, "Saved MANUAL bags show the dropdown");
  assert.equal(elements["loop-bags"].options.length, 1);
  assert.equal(elements["loop-bags"].value, "Loop 1");

  data = { ...data, mode: "AUTO" };
  polling();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements["loop-bags"].hidden, true, "AUTO must hide MANUAL bag dropdown");
  assert.equal(elements["loop-bags"].disabled, true);
  process.stdout.write("LOOP 3-box UI and empty-record dropdown PASS\n");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
