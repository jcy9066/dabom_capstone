/* Patrol selection must be confirmed by server/Pi, never start the motor. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(
  path.join(__dirname, '..', 'frontend/services/static/navigation_control.js'), 'utf8'
);
const begin = source.indexOf('    async function requestDriveMode(mode, options = {}) {');
const end = source.indexOf('    function navigationIsMoving()', begin);
assert(begin >= 0 && end > begin, 'Patrol API handler missing');
const make = new Function(
  'state','setFeedback','requestNavigationMode','mutate',
  'applyControlState','refreshState','syncControlComponents',
  source.slice(begin,end) + '; return requestDriveMode;'
);
async function scenario(target, options={}) {
  const calls=[],feedback=[];
  const state={
    control:{
      connected:options.connected??true,
      robot_mode:options.robot_mode??'manual',
      navigation_mode:options.navigation_mode??'DRIVING',
      ...options.state
    },
    drivePending:options.pending??false,navigationPending:false,
    pointerId:null,pointerStart:null,draftGoal:null
  };
  const action=make(
    state,(value,error)=>feedback.push({value,error:!!error}),
    async mode=>{calls.push(['prepare',mode]);return options.prepared??true},
    async (url,body)=>{
      calls.push([url,body]);
      if(options.failure)throw Error(options.failure);
      return options.answer??{
        robot_mode:target.toLowerCase(),patrol_ready:target==='AUTO',
        patrol_transition:null,patrol_transition_error:null
      };
    },
    result=>{state.control={...state.control,...result}},
    async()=>{calls.push(['refresh']);return state.control},
    ()=>{}
  );
  const ok=await action(target);
  return {ok,calls,feedback,state};
}
(async()=>{
  let r=await scenario('AUTO');
  assert(r.ok && r.calls.length===1);
  assert.equal(r.calls[0][0],'/api/navigation/control/patrol-mode');
  assert.equal(r.calls[0][1].mode,'auto');
  assert(!r.calls.some(x=>String(x[0]).includes('/start')));
  r=await scenario('AUTO',{navigation_mode:'MAPPING',answer:{
    robot_mode:'auto',navigation_mode:'MAPPING',patrol_ready:false,
    patrol_transition:null,patrol_transition_error:null
  }});
  assert(r.ok && r.calls.length===1);
  assert.equal(r.calls[0][0],'/api/navigation/control/patrol-mode');
  assert(!r.calls.some(x=>x[0]==='prepare' || x[0]==='/api/navigation/control/mode'));
  assert.equal(r.state.control.navigation_mode,'MAPPING');
  r=await scenario('MANUAL',{navigation_mode:'MAPPING',robot_mode:'auto',answer:{
    robot_mode:'manual',navigation_mode:'MAPPING',patrol_ready:false,
    patrol_transition:null,patrol_transition_error:null
  }});
  assert(r.ok && r.calls.length===1);
  assert.equal(r.state.control.navigation_mode,'MAPPING');
  r=await scenario('AUTO',{answer:{
    robot_mode:'auto',navigation_mode:'DRIVING',patrol_ready:false,
    patrol_transition:null,patrol_transition_error:null
  }});
  assert(!r.ok && r.calls.some(x=>x[0]==='refresh'));
  r=await scenario('AUTO',{failure:'Pi ACK missing'});
  assert(!r.ok && r.calls.some(x=>x[0]==='refresh'));
  assert(r.feedback.at(-1).error);
  r=await scenario('AUTO',{connected:false});
  assert(!r.ok && r.calls.length===0);
  r=await scenario('MANUAL',{answer:{
    robot_mode:'manual',patrol_transition:null,patrol_transition_error:null,
    active_goal:{x:2,y:2},planned_path:[{x:0,y:0},{x:2,y:2}]
  }});
  assert(r.ok && r.state.control.active_goal);
  assert.equal(r.state.control.planned_path.length,2);
  r=await scenario('AUTO',{answer:{
    robot_mode:'manual',patrol_ready:false,patrol_transition:null
  }});
  assert(!r.ok && r.calls.some(x=>x[0]==='refresh'));
  r=await scenario('AUTO',{pending:true});
  assert(!r.ok && r.calls.length===0);
  console.log('PASS: 9 server-authoritative patrol mode cases');
})().catch(e=>{console.error(e);process.exitCode=1});
