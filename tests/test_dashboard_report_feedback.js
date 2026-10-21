const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const script=fs.readFileSync('frontend/services/static/script.js','utf8');
const start=script.indexOf('let reportPending = false;'), end=script.indexOf('function warnTrespasser()',start);
assert(start!==-1&&end>start);
const handler=script.slice(start,end);
function setup({confirmed=true,mode='success',deferred=false}={}) {
    const states=[],calls=[],alerts=[],button={disabled:false};
    let confirms=0,release;
    const pending=new Promise(resolve=>{release=resolve;});
    const env={
        window:{DabomActionFeedback:{setReportState:(state,msg)=>states.push([state,msg])}},
        document:{querySelector:selector=>selector==='.action-report'?button:null},
        confirm:()=>{confirms++;return confirmed;},alert:msg=>alerts.push(msg),
        console:{error:()=>{}},
        fetch:async(url,options)=>{
            calls.push({url,options});
            if(url==='/api/auth/csrf')return {ok:true,json:async()=>({csrf_token:'mock-token'})};
            if(url==='/send_telegram'){
                if(deferred)await pending;
                return {ok:mode==='success',json:async()=>mode==='success'?{status:'success'}:{status:'error',detail:'mock failure'}};
            }
            throw Error('unexpected request');
        },
        JSON,Error,Date,Promise,setTimeout:fn=>{fn();return 1;}
    };
    vm.createContext(env);vm.runInContext(handler,env);
    return {env,states,calls,alerts,button,release,confirmCount:()=>confirms};
}
(async()=>{
    let t=setup({confirmed:false});assert.equal(await t.env.reportDanger(),false);assert.equal(t.calls.length,0);
    t=setup();assert.equal(await t.env.reportDanger(),true);
    assert.deepEqual(t.states.map(x=>x[0]),['sending','success']);
    assert.deepEqual(t.calls.map(x=>x.url),['/api/auth/csrf','/send_telegram']);
    assert.equal(t.calls[1].options.method,'POST');
    assert.equal(t.calls[1].options.headers['X-CSRF-Token'],'mock-token');
    assert.equal(t.button.disabled,false);
    assert.deepEqual(t.alerts,[]);
    t=setup({mode:'failure'});assert.equal(await t.env.reportDanger(),false);
    assert.deepEqual(t.states.map(x=>x[0]),['sending','error']);
    assert.equal(t.alerts.length,1);assert.equal(t.button.disabled,false);
    t=setup({deferred:true});const first=t.env.reportDanger();await Promise.resolve();
    assert.equal(await t.env.reportDanger(),false);assert.equal(t.confirmCount(),1);
    t.release();assert.equal(await first,true);
    assert.equal(t.calls.filter(x=>x.url==='/send_telegram').length,1);
    t=setup();assert.equal(await t.env.reportDanger(true),true);
    assert.equal(t.confirmCount(),0);assert.equal(t.alerts.length,0);
    console.log('PASS: report cancel/success/failure/CSRF/deduplication/automatic');
})().catch(e=>{console.error(e);process.exitCode=1;});
