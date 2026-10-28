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

function testDiagnosticSummaryHidden() {
  const html = fs.readFileSync(
    path.join(root, "frontend/templates/index.html"), "utf8");
  const start = html.indexOf('<details id="recordedLoopPanel"');
  const end = html.indexOf("</details>", start);
  assert.ok(start >= 0 && end > start, "Recorded Path / Loop panel missing");
  const panel = html.slice(start, end);
  for (const id of ["recorded-map", "recorded-position",
                    "recorded-goals", "recorded-segments"]) {
    assert.ok(!panel.includes(`id="${id}"`), `Summary field still visible: ${id}`);
    assert.ok(!source.includes(`$("${id}")`), `Stale JS reference: ${id}`);
  }
  for (const id of [
    "recorded-goal-id", "recorded-add-goal", "recorded-segment-id",
    "recorded-from", "recorded-to", "recorded-source",
    "recorded-start", "recorded-save", "recorded-discard",
    "recorded-route-id", "recorded-route-segments", "recorded-save-route",
    "recorded-play-kind", "recorded-play-route", "recorded-policy",
    "recorded-play-start", "recorded-play-stop", "recorded-play-reset",
    "recorded-delete-kind", "recorded-delete-id", "recorded-delete"
  ]) {
    assert.ok(panel.includes(`id="${id}"`), `Required control missing: ${id}`);
  }
  assert.ok(source.includes("state.current_pose"),
            "Saving a Goal still requires the current pose from the API");
}

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


async function testOutOfOrderStateResponses() {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) {
      elements.set(id, {
        value: "", textContent: "", disabled: false, hidden: false,
        dataset: {},
        addEventListener() {},
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
    },
  };
  let poll;
  const pending = [];
  const fetch = url => {
    assert.ok(url.endsWith("/state"));
    return new Promise(resolve => pending.push(resolve));
  };
  const response = active => ({
    ok: true,
    json: async () => ({
      ok: true,
      data: { goals: {}, segments: {} },
      recording: { active: false },
      play_active: active,
      playback: active
        ? { state: "RUNNING", label: "순찰 중" }
        : { state: "IDLE", label: "준비" },
    }),
  });
  const flush = () => new Promise(resolve => setImmediate(resolve));

  vm.runInNewContext(source, {
    document, fetch,
    setInterval(callback) { poll = callback; },
    window: { confirm: () => true },
    console,
  }, { filename: "recorded_loop_ui.js" });

  poll();
  assert.equal(pending.length, 2);
  pending[1](response(true));
  await flush();
  assert.equal(get("recorded-playback-state").textContent, "순찰 중");
  assert.equal(get("recorded-play-start").disabled, true);

  pending[0](response(false));
  await flush();
  assert.equal(get("recorded-playback-state").textContent, "순찰 중");
  assert.equal(get("recorded-play-start").disabled, true);

  // A slow response may still update the UI while a newer request is pending.
  poll();
  poll();
  assert.equal(pending.length, 4);
  pending[2](response(false));
  await flush();
  assert.equal(get("recorded-playback-state").textContent, "준비");
  assert.equal(get("recorded-play-start").disabled, false);
  pending[3](response(true));
  await flush();
  assert.equal(get("recorded-playback-state").textContent, "순찰 중");
  assert.equal(get("recorded-play-start").disabled, true);
}


async function testStateFailureAllowsStopAndRecovers() {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) {
      elements.set(id, {
        value: "", textContent: "", disabled: false, hidden: false,
        dataset: {}, events: {},
        addEventListener(type, cb) { this.events[type] = cb; },
        click() {
          assert.equal(this.disabled, false);
          this.events.click();
        },
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
    },
  };
  let poll;
  let stateCalls = 0;
  const fetch = async url => {
    if (url.endsWith("/state")) {
      stateCalls++;
      if (stateCalls === 2) throw new Error("simulated network error");
      const active = stateCalls >= 3;
      return {
        ok: true,
        json: async () => ({
          ok: true,
          data: { goals: {}, segments: {} },
          recording: { active: false },
          play_active: active,
          playback: active
            ? { state: "RUNNING", label: "순찰 중" }
            : { state: "IDLE", label: "준비" },
        }),
      };
    }
    if (url === "/api/auth/csrf") {
      return { ok: true, json: async () => ({ csrf_token: "test" }) };
    }
    if (url.endsWith("/replay/start")) {
      return { ok: true, json: async () => ({ ok: true, started: true }) };
    }
    throw new Error("Unexpected request: " + url);
  };
  const flush = () => new Promise(resolve => setImmediate(resolve));

  vm.runInNewContext(source, {
    document, fetch,
    setInterval(callback) { poll = callback; },
    window: { confirm: () => true },
    console,
  }, { filename: "recorded_loop_ui.js" });

  await flush();
  get("recorded-play-kind").value = "path";
  get("recorded-policy").value = "WAIT";
  get("recorded-play-route").value = "AB";
  get("recorded-play-start").click();
  await flush();
  await flush();

  assert.equal(get("recorded-status").textContent, "연결 확인 필요");
  assert.equal(get("recorded-playback-state").textContent, "상태 확인 불가");
  assert.equal(get("recorded-play-start").disabled, true);
  assert.equal(get("recorded-play-stop").disabled, false);
  assert.equal(get("recorded-delete").disabled, true);

  poll();
  await flush();
  assert.equal(get("recorded-playback-state").textContent, "순찰 중");
  assert.equal(get("recorded-play-start").disabled, true);
  assert.equal(get("recorded-play-stop").disabled, false);
}


async function testMutationRejectsEarlierStateResponse() {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) {
      elements.set(id, {
        value: "", textContent: "", disabled: false, hidden: false,
        dataset: {}, events: {},
        addEventListener(type, cb) { this.events[type] = cb; },
        click() {
          assert.equal(this.disabled, false);
          this.events.click();
        },
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
    },
  };
  let poll;
  let stateCall = 0;
  let resolveOldState;
  let resolveStart;
  const stateResponse = active => ({
    ok: true,
    json: async () => ({
      ok: true,
      data: { goals: {}, segments: {} },
      recording: { active: false },
      play_active: active,
      playback: active
        ? { state: "RUNNING", label: "순찰 중" }
        : { state: "IDLE", label: "준비" },
    }),
  });
  const fetch = url => {
    if (url.endsWith("/state")) {
      stateCall++;
      if (stateCall === 1) return Promise.resolve(stateResponse(false));
      if (stateCall === 2) {
        return new Promise(resolve => { resolveOldState = resolve; });
      }
      return Promise.resolve(stateResponse(true));
    }
    if (url === "/api/auth/csrf") {
      return Promise.resolve({
        ok: true, json: async () => ({ csrf_token: "test" }),
      });
    }
    if (url.endsWith("/replay/start")) {
      return new Promise(resolve => { resolveStart = resolve; });
    }
    throw new Error("Unexpected request: " + url);
  };
  const flush = () => new Promise(resolve => setImmediate(resolve));

  vm.runInNewContext(source, {
    document, fetch,
    setInterval(callback) { poll = callback; },
    window: { confirm: () => true },
    console,
  }, { filename: "recorded_loop_ui.js" });

  await flush();
  poll(); // An old state request remains pending.
  get("recorded-play-kind").value = "path";
  get("recorded-policy").value = "WAIT";
  get("recorded-play-route").value = "AB";
  get("recorded-play-start").click();
  await flush();

  assert.equal(get("recorded-play-start").disabled, true);
  resolveStart({ ok: true, json: async () => ({ ok: true }) });
  await flush();
  await flush();
  assert.equal(get("recorded-playback-state").textContent, "순찰 중");
  assert.equal(get("recorded-play-start").disabled, true);

  resolveOldState(stateResponse(false));
  await flush();
  assert.equal(get("recorded-playback-state").textContent, "순찰 중");
  assert.equal(get("recorded-play-start").disabled, true);
}

async function main() {
  testDiagnosticSummaryHidden();
  await runScenario("path", "REPLAN", "AB", 1);
  await runScenario("path", "WAIT", "BA", 1);
  await runScenario("loop", "WAIT", "A_B_A", null);
  await runScenario("loop", "REPLAN", "A_B_A", null);
  await testOutOfOrderStateResponses();
  await testStateFailureAllowsStopAndRecovers();
  await testMutationRejectsEarlierStateResponse();
  process.stdout.write("Recorded Path/Loop dashboard: 8/8 mocked UI PASS\n");
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
