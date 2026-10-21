const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const listeners = new Map();
const timers = new Map();
let nextTimer = 0;
const classes = () => { const set = new Set(); return {
    add: name => set.add(name), remove: name => set.delete(name), has: name => set.has(name)
}; };
const reportLabel = { textContent: '신고' };
const report = { dataset:{}, attributes:{}, disabled:false, title:'',
    querySelector: selector => selector === '.action-report-label' ? reportLabel : null,
    setAttribute(k,v){this.attributes[k] = String(v);}, removeAttribute(k){delete this[k];},
    classList:classes() };
function action() { return {style:{values:new Map(),setProperty(k,v){this.values.set(k,v);}},
    classList:classes(),disabled:false,
    getBoundingClientRect:()=>({left:0,top:0,width:90,height:36}),offsetWidth:90}; }
const beep=action(), warning=action();
let capture=false;
const controls = {
    querySelector: selector=>selector==='.action-report'?report:null,
    contains:node=>[beep,warning,report].includes(node),
    addEventListener:(name,handler,options)=>{listeners.set(name,handler);
        if(name==='click')capture=Boolean(options?.capture);}
};
const window = { document:{querySelector:selector=>selector==='.dashboard-action-controls'?controls:null},
    setTimeout(fn){const id=++nextTimer;timers.set(id,fn);return id;},
    clearTimeout(id){timers.delete(id);},matchMedia:()=>({matches:false})};
vm.runInNewContext(fs.readFileSync('frontend/services/static/action_feedback.js','utf8'),{window});
assert(capture,'capture click so ripple starts before disabled state');
assert.equal(report.dataset.reportState,'idle');
assert.equal(reportLabel.textContent,'신고');
const feedback=window.DabomActionFeedback;
feedback.setReportState('sending');
assert.equal(report.attributes['aria-busy'],'true');
feedback.setReportState('success');
assert.equal(reportLabel.textContent,'신고 완료');
assert.equal(timers.size,1);
feedback.setReportState('sending');
assert.equal(timers.size,0,'new report cancels old reset');
feedback.setReportState('error','mock failure');
assert.equal(report.title,'mock failure');
assert.equal(reportLabel.textContent,'전송 실패');
[...timers.values()][0]();
assert.equal(report.dataset.reportState,'idle');
listeners.get('click')({target:{closest:()=>beep},detail:1,clientX:20,clientY:10});
assert(beep.classList.has('action-ripple-running'));
listeners.get('click')({target:{closest:()=>warning},detail:0});
assert(warning.classList.has('action-ripple-running'));
warning.disabled=true;
warning.classList.remove('action-ripple-running');
listeners.get('click')({target:{closest:()=>warning},detail:1,clientX:10,clientY:10});
assert(!warning.classList.has('action-ripple-running'));
listeners.get('animationend')({target:beep,animationName:'dashboard-action-ripple'});
assert(!beep.classList.has('action-ripple-running'));
console.log('PASS: report status + ripple capture + keyboard + disabled + reset');
