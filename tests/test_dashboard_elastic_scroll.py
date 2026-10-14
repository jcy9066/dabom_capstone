"""Dashboard elastic overscroll: boundaries, scroll chaining, and exclusions."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_visual_elastic_scroll_only_at_real_edges():
    source = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const listeners = {};
function element(name, parent, height=400, client=150, top=0, overflow='auto') {
    const classes = new Set(), vars = new Map();
    return {name,nodeType:1,parentElement:parent,scrollHeight:height,clientHeight:client,
        scrollTop:top,isConnected:true,overflow,
        classList:{add:v=>classes.add(v),remove:v=>classes.delete(v),contains:v=>classes.has(v)},
        style:{setProperty:(k,v)=>vars.set(k,v),removeProperty:k=>vars.delete(k),
            getPropertyValue:k=>vars.get(k)||''},
        closest:query=>query.includes('.lidar-3d-viewer') && name==='viewer' ? {} : null};
}
const html=element('html',null,1000,500,0,'visible');
const body=element('body',html,1000,500,0,'visible');
const list=element('list',body);
const item=element('item',list,100,100,0,'visible');
const viewer=element('viewer',list,100,100,0,'visible');
const document={scrollingElement:html,documentElement:html,body,hidden:false,
    addEventListener:(k,fn)=>listeners[k]=fn};
const timers=new Map();let tid=0;
const window={document,innerHeight:500,navigator:{userAgent:'TestBrowser'},
    CSS:{supports:()=>true},getComputedStyle:el=>({overflowY:el.overflow,overscrollBehaviorY:''}),
    matchMedia:()=>({matches:false,addEventListener(){}}),
    setTimeout:fn=>{const id=++tid;timers.set(id,fn);return id;},
    clearTimeout:id=>timers.delete(id),addEventListener(){}};
vm.runInNewContext(fs.readFileSync('frontend/services/static/elastic_scroll.js','utf8'),{window,document});
function wheel(target,dy){let prevented=false;
    listeners.wheel({target,deltaY:dy,deltaX:0,deltaMode:0,cancelable:true,
        preventDefault(){prevented=true;}});return prevented;}
function flush(){for(const [id,fn] of [...timers]){timers.delete(id);fn();}}
assert.equal(wheel(item,30),false); // list can scroll
list.scrollTop=250;
assert.equal(wheel(item,30),false); // page can scroll: chain normally
html.scrollTop=500;
assert.equal(wheel(item,30),true); // now all at bottom
assert(Number.parseFloat(list.style.getPropertyValue('--elastic-scroll-offset'))<0);
for(let i=0;i<25;i++)wheel(item,100);
assert(Math.abs(Number.parseFloat(list.style.getPropertyValue('--elastic-scroll-offset')))<=24);
flush();
assert(list.classList.contains('elastic-scroll-returning'));
flush();
assert(!list.classList.contains('elastic-scroll-returning'));
list.scrollTop=0;html.scrollTop=0;
assert.equal(wheel(item,-30),true);
assert(Number.parseFloat(list.style.getPropertyValue('--elastic-scroll-offset'))>0);
assert.equal(wheel(viewer,-30),false); // 3D gestures excluded
list.scrollTop=100;
assert.equal(wheel(item,30),false);
assert(!list.classList.contains('elastic-scroll-active'));
list.scrollTop=0;html.scrollTop=0;
let prevented=false;
listeners.touchstart({target:item,touches:[{identifier:1,clientX:10,clientY:50}]});
listeners.touchmove({cancelable:true,touches:[{identifier:1,clientX:10,clientY:85}],
    preventDefault(){prevented=true;}});
assert(prevented,'touch at boundary');
listeners.touchend({});
flush();flush();
assert(!list.classList.contains('elastic-scroll-active'));
"""
    result = subprocess.run(
        ["node", "-e", source], cwd=ROOT, text=True,
        capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_elastic_scroll_assets_on_dashboard_and_login():
    for template in ("index.html", "login.html"):
        source = (ROOT / "frontend/templates" / template).read_text(encoding="utf-8")
        assert '/static/elastic_scroll.css?v=20261014-elastic-v1' in source
        assert '/static/elastic_scroll.js?v=20261014-boundary-v2' in source
    css = (ROOT / "frontend/services/static/elastic_scroll.css").read_text(encoding="utf-8")
    assert "250ms cubic-bezier" in css
    assert "prefers-reduced-motion" in css

def test_fixed_dashboard_without_native_scroll_still_bounces():
    # Main dashboard uses body overflow:hidden; scrollHeight == clientHeight.
    # A gesture above its right panel must animate without moving the page.
    source = r"""
const assert=require('node:assert/strict'),fs=require('fs'),vm=require('vm');
const listeners={};
const cls=()=>{const x=new Set();return {add:n=>x.add(n),remove:n=>x.delete(n),contains:n=>x.has(n)}};
const style=()=>{const x=new Map();return {setProperty:(k,v)=>x.set(k,v),
    removeProperty:k=>x.delete(k),getPropertyValue:k=>x.get(k)||''}};
const html={nodeType:1,scrollHeight:800,clientHeight:800,scrollTop:0};
const body={nodeType:1,scrollHeight:800,clientHeight:800,scrollTop:0,parentElement:html};
const panel={nodeType:1,parentElement:body,scrollHeight:300,clientHeight:300,
    scrollTop:0,classList:cls(),style:style(),isConnected:true};
const item={nodeType:1,parentElement:panel,closest:q=>
    q.includes('.right-panel')?panel:null};
const doc={scrollingElement:html,documentElement:html,body,hidden:false,
    addEventListener:(name,fn)=>{listeners[name]=fn;}};
const timers=new Map();let next=0;
const win={document:doc,innerHeight:800,navigator:{userAgent:'Desktop'},
    CSS:{supports:()=>true},getComputedStyle:()=>({overflowY:'visible',overscrollBehaviorY:''}),
    matchMedia:()=>({matches:false,addEventListener(){}}),
    setTimeout:fn=>{const n=++next;timers.set(n,fn);return n},
    clearTimeout:n=>timers.delete(n),addEventListener(){}};
vm.runInNewContext(fs.readFileSync('frontend/services/static/elastic_scroll.js','utf8'),{window:win,document:doc});
let prevented=false;
listeners.wheel({target:item,deltaY:70,deltaX:0,deltaMode:0,cancelable:true,
    preventDefault(){prevented=true;}});
assert(prevented,'non-scrollable dashboard area must visually bounce');
assert(panel.classList.contains('elastic-scroll-active'));
assert(Number.parseFloat(panel.style.getPropertyValue('--elastic-scroll-offset'))<0);
assert.equal(html.scrollTop,0,'native page scroll stays unchanged');
"""
    result = subprocess.run(["node","-e",source], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout
