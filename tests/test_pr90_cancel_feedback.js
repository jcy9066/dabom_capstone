/* Cancel results must never treat Pi STOP failure as cancel success. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const js = fs.readFileSync(
  path.join(__dirname, '..', 'frontend/services/static/navigation_control.js'), 'utf8');
const start = js.indexOf('    async function simpleAction(path, progress) {');
const end = js.indexOf('    async function requestLedToggle(', start);
assert(start >= 0 && end > start, 'Missing Goal cancel action');
const createAction = new Function(
  'state', 'setFeedback', 'mutate', 'applyControlState', 'refreshState',
  js.slice(start, end) + '; return simpleAction;'
);
async function scenario(response, failure = null) {
  const messages = [];
  let refreshed = false;
  const run = createAction(
    {busy:false, drivePending:false},
    (text, error) => messages.push({text, error: Boolean(error)}),
    async () => {if (failure) throw new Error(failure); return response;},
    () => {},
    async () => {refreshed = true;}
  );
  await run('/api/navigation/control/cancel', 'Cancelling');
  assert(refreshed, 'Cancel must refresh actual server state');
  return messages.at(-1);
}
(async () => {
  let r = await scenario({
    navigation_mode:'DRIVING', navigation_state:'READY',
    cancel_completed:true, physical_stop_confirmed:true, stop_delivered:true
  });
  assert(!r.error && r.text.includes('확인되었습니다'));
  r = await scenario({
    navigation_mode:'DRIVING', navigation_state:'CANCELING_STOP',
    stop_delivered:true, cancel_completed:false
  });
  assert(!r.error && r.text.includes('정지 확인 중'));
  r = await scenario({
    navigation_mode:'DRIVING', navigation_state:'FAILED',
    stop_delivered:false, last_error:'STOP_NOT_DELIVERED'
  });
  assert(r.error && r.text.includes('STOP_NOT_DELIVERED'));
  r = await scenario({
    navigation_mode:'DRIVING', navigation_state:'EMERGENCY_STOPPED',
    stop_delivered:true
  });
  assert(r.error && r.text.includes('E-STOP'));
  r = await scenario({
    navigation_mode:'DRIVING', navigation_state:'NAVIGATING',
    stop_delivered:true, cancel:{terminal:'SUCCEEDED'}
  });
  assert(!r.error && r.text.includes('취소보다 먼저'));
  r = await scenario({}, 'socket timeout');
  assert(r.error && r.text.includes('socket timeout'));
  console.log('PASS: 6 Goal cancel result variants');
})().catch(error => {console.error(error); process.exitCode = 1;});
