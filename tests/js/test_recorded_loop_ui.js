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
const events = {}, trigger = {events: {}, addEventListener(n, f) {this.events[n]=f;}};
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
assert.equal(registered.name,"loopModal");
const body=registered.descriptor.render();
for(const id of ["loop-map","loop-mode","loop-goals","loop-bags","loop-record",
 "loop-save","loop-delete","loop-start","loop-stop","loop-policy-toggle"]) assert.ok(body.includes('id="'+id+'"'),id);
trigger.events.click();
assert.equal(opened,true);
assert.ok(js.includes("dabom:loop-goal-picked"));
assert.ok(js.includes('policy === "REPLAN"'));
process.stdout.write("LOOP modal mock registration PASS\n");
