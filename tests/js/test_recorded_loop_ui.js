#!/usr/bin/env node
/* Recorded Loop dashboard smoke test: no browser, network, Pi or ROS. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
const source = fs.readFileSync(
  path.join(root, "frontend/services/static/recorded_loop_ui.js"), "utf8");

async function runScenario(kind, policy, id, expectedLaps) {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) {
      elements.set(id, {
        value: "", textContent: "", disabled: false, hidden: false,
        dataset: {},
        events: {},
        addEventListener(type, cb) { this.events[type] = cb; },
        querySelector() { return null; },
        click() {
          if (this.disabled) throw Error("Attempted to click disabled " + id);
          assert.equal(typeof this.events.click, "function", "Missing click " + id);
          this.events.click();
        }
      });
    }
    return elements.get(id);
  };
  const panel = { querySelector: selector => get(selector.slice(1)) };
  const document = {
    hidden: false,
    getElementById(id) {
      if (id === "recordedLoopPanel") return panel;
      if (id === "recordedLoopViewerStatus") return get(id);
      return null;
    }
  };
  const calls = [];
  const fetch = async (url, options = {}) => {
    if (url === "/api/auth/csrf") {
      return { ok: true, json: async () => ({ csrf_token: "test-csrf" }) };
    }
    if (url.endsWith("/state")) {
      return { ok: true, json: async () => ({
        ok: true, map_name: "test-map",
        data: { goals: { A: {}, B: {} }, segments: { AB: {}, BA: {} } },
        current_pose: { x: 0, y: 0 }, play_active: false,
        recording: { active: false },
        playback: { state: "IDLE", label: "준비", lap: 0 },
      }) };
    }
    calls.push({ url, options });
    return { ok: true, json: async () => ({ ok: true }) };
  };
  vm.runInNewContext(source, {
    document, fetch, setInterval() {},
    window: { confirm: () => true }, console
  }, { filename: "recorded_loop_ui.js" });
  const flush = () => new Promise(resolve => setImmediate(resolve));
  await flush();
  get("recorded-play-kind").value = kind;
  get("recorded-policy").value = policy;
  get("recorded-play-route").value = id;
  get("recorded-play-start").click();
  await flush();
  await flush();
  assert.equal(calls.length, 1);
  const request = calls[0];
  assert.equal(request.url, "/api/navigation/recorded/replay/start");
  assert.equal(request.options.headers["X-CSRF-Token"], "test-csrf");
  assert.equal(request.options.credentials, "same-origin");
  assert.deepEqual(JSON.parse(request.options.body), {
    kind, route_id: id, policy, max_laps: expectedLaps
  });
  assert.equal(get("recorded-feedback").textContent.includes("실패"), false);
}

async function main() {
  await runScenario("path", "REPLAN", "AB", 1);
  await runScenario("path", "WAIT", "BA", 1);
  await runScenario("loop", "WAIT", "A_B_A", null);
  await runScenario("loop", "REPLAN", "A_B_A", null);
  process.stdout.write("Recorded Path/Loop dashboard: 4/4 mocked UI PASS\n");
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
