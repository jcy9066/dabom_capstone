/* Goal path is server-owned through cancel / manual pause. No browser or robot. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const js = fs.readFileSync(
  path.join(__dirname, '..', 'frontend/services/static/script.js'), 'utf8');
const start = js.indexOf("document.addEventListener('dabom:navigation-control-state',");
const end = js.indexOf("function scheduleNavigationSnapshot(", start);
assert(start >= 0 && end > start, 'Missing navigation state event handler');
const handler = new Function(`
  let navigationGlobalPathRunKey = null, route = [], resets = 0, events = {};
  const document = {addEventListener:(name,cb) => events[name] = cb};
  const navigationControlGoalKey = c => c?.active_goal
    ? JSON.stringify([c.goal_generation, c.active_goal]) : null;
  const beginNavigationGlobalPathPending = () => {route = []};
  const applyNavigationGlobalPath = path => {route = path.slice()};
  const resetNavigationGlobalPath = () => {route = []; resets++};
  const emitNavigationVisualizationState = () => {};
  ${js.slice(start, end)}
  return {
    dispatch: control => events['dabom:navigation-control-state']({detail:control}),
    current: () => route.slice(), resets: () => resets
  };
`)();
const goal = {x: 1, y: 2, yaw: 0};
const route = [{x: 0, y: 0}, {x: 1, y: 2}];
for (const status of [
  'PATH_READY', 'NAVIGATING', 'PAUSING_FOR_MANUAL',
  'CANCELING', 'CANCELING_STOP', 'READY'
]) {
  handler.dispatch({
    navigation_state: status, active_goal: goal,
    planned_path: route, goal_generation: status.startsWith('CANCEL') ? 2 : 1
  });
  assert.equal(handler.current().length, 2, status + ' removed Goal route early');
}
handler.dispatch({
  navigation_state: 'READY', active_goal: null,
  planned_path: [], goal_generation: 2
});
assert.equal(handler.current().length, 0, 'Canceled Goal route reappeared');
console.log('PASS: 7 Goal-owned navigation path events');
