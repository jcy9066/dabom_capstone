#!/usr/bin/env node
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "../..");
const js = fs.readFileSync(path.join(root, "frontend/services/static/recorded_loop_ui.js"), "utf8");
const html = fs.readFileSync(path.join(root, "frontend/templates/index.html"), "utf8");
assert.ok(html.includes('id="loop-modal-trigger"'));
assert.ok(html.includes('id="loop-goal-toggle"'));
assert.ok(!html.includes('id="recordedLoopPanel"'));
assert.ok(!html.includes('data-lidar-interaction="set-loop"'));
const events = {}, trigger = {
  disabled: true, events: {}, classesRemoved: [],
  addEventListener(n, f) { this.events[n] = f; },
  removeAttribute(name) { assert.equal(name, "aria-disabled"); },
  classList: { remove(name) { assert.equal(name, "disabled"); } },
};
let registered, opened=false;
const manager = {register(name, descriptor) {registered={name,descriptor};},
 open(name) {assert.equal(name,"loopModal");opened=true;},body:{}};
const document = {readyState:"loading",hidden:false,
 getElementById(id){return id==="loop-modal-trigger"?trigger:null;},
 addEventListener(n,f){events[n]=f;}};
vm.runInNewContext(js,{document,window:{DabomDashboardComponents:{modal:{
 getDefault:()=>manager}},confirm:()=>true},fetch:async()=>{throw Error("offline");},
 setInterval(){},CustomEvent:class{},console},{filename:"recorded_loop_ui.js"});
events.DOMContentLoaded();
assert.equal(trigger.disabled, false, "Offline Pi must not disable LOOP modal trigger");
assert.equal(registered.name,"loopModal");
const body=registered.descriptor.render();
assert.ok(body.includes('class="loop-modal-grid"'), "Box grid exists");
assert.equal((body.match(/class="loop-modal-section"/g) || []).length, 3,
  "GOALS, RECORD/PLAY and LOOP CONTROL occupy separate boxes");
assert.ok(body.includes('id="loop-bags" aria-label="Recorded manual loops" hidden disabled'),
  "Empty bag dropdown is hidden and disabled before state fetch");
for(const id of ["loop-map","loop-mode","loop-goals","loop-bags","loop-record",
 "loop-save","loop-delete","loop-start","loop-stop","loop-policy-toggle",
 "loop-connection"]) assert.ok(body.includes('id="'+id+'"'),id);
trigger.events.click();
assert.equal(opened,true, "Offline backend response must not block opening the modal");
for(const id of ["loop-record","loop-save","loop-delete","loop-start","loop-stop","loop-policy-toggle"]) {
  assert.ok(body.includes('id="' + id + '"') && body.includes('data-control-button='),
    "Existing shared button component used for " + id);
}
assert.ok(body.includes('id="loop-start" disabled'), "Record/Start are disabled until state check");
assert.ok(js.includes("dabom:loop-goal-picked"));
assert.ok(js.includes('policy === "REPLAN"'));
// Regression: Pi is offline and the shared modal was not mounted by
// the main dashboard initializer. LOOP must mount it lazily on click.
{
  let mounted = 0, launched = 0, deferred = null;
  const callbacks = {};
  const lateTrigger = {
    disabled: true, classList: { remove() {} },
    removeAttribute() {},
    addEventListener(name, callback) { callbacks[name] = callback; },
  };
  const delayedManager = {
    register(name) { assert.equal(name, "loopModal"); },
    open(name) { assert.equal(name, "loopModal"); launched++; },
    body: {},
  };
  const delayedDocument = {
    readyState: "complete", hidden: true,
    getElementById(id) {
      return id === "loop-modal-trigger" ? lateTrigger
        : id === "commonModal" ? {} : null;
    },
    addEventListener() {},
  };
  vm.runInNewContext(js, {
    document: delayedDocument,
    window: {
      DabomDashboardComponents: {
        modal: {
          getDefault: () => deferred,
          mount: () => { mounted++; deferred = delayedManager; return deferred; },
        },
      },
      alert: () => assert.fail("LOOP should open while Pi is offline"),
    },
    fetch: async () => { throw Error("offline"); },
    setInterval() {},
    CustomEvent: class {},
  }, {filename: "recorded_loop_ui_offline.js"});
  assert.equal(lateTrigger.disabled, false);
  assert.equal(mounted, 1);
  callbacks.click();
  assert.equal(launched, 1);
}
process.stdout.write("LOOP modal mock registration + offline lazy mount PASS\n");
