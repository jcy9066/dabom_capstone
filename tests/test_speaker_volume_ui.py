"""Static and browser-style contract checks for shared Beep/warning volume."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SharedSpeakerVolumeUiTests(unittest.TestCase):
    def test_pico_speaker_volume_defaults_and_shared_beep_path(self):
        source = (ROOT / "raspberry/pico_w_sdk/main.c").read_text(encoding="utf-8")
        self.assertIn("#define BEEP_DEFAULT_VOLUME_PERCENT 20U", source)
        self.assertIn('strcmp(command, "VOLUME_GET")', source)
        self.assertIn('strcmp(command, "VOLUME")', source)
        self.assertIn('BEEP_PWM_WRAP * speaker_volume_percent', source)
        self.assertIn("speaker_volume_percent = (uint8_t)percent;", source)
        pi = (ROOT / "raspberry/robot_command_client.py").read_text(encoding="utf-8")
        self.assertIn('elif command_type == "set_volume":', pi)
        self.assertIn('self.motor.beep', pi)
        self.assertIn('async def _run_warning_pattern(', pi)

    def test_header_speaker_control_precedes_model_toggle(self):
        html = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
        camera_header = html.split('id="camera-card"', 1)[1].split('id="video-wrapper"', 1)[0]
        self.assertLess(camera_header.index('id="speaker-volume-control"'), camera_header.index('id="model-pipeline-toggle-mount"'))
        self.assertIn('id="speaker-volume-slider"', camera_header)
        self.assertIn('type="range" min="0" max="100" step="1" value="20"', camera_header)
        self.assertIn("speaker_volume.css?v=20261015-border-v2", html)
        self.assertIn("speaker_volume.js?v=20261015-volume-v1", html)
        styles = (ROOT / "frontend/services/static/speaker_volume.css").read_text(encoding="utf-8")
        self.assertIn("height:calc(var(--slider-height) * 2)", styles)
        self.assertIn("border:1pt solid var(--text-muted);", styles)
        self.assertIn("box-sizing:border-box;", styles)

    def test_keyboard_commits_final_volume_and_mouse_keeps_one_percent_resolution(self):
        test = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
function el(value='') {return {value,disabled:true,textContent:'',dataset:{},attributes:{},listeners:{},
  setAttribute(k,v){this.attributes[k]=v;},addEventListener(k,f){this.listeners[k]=f;},
  dispatch(k,arg={}){this.listeners[k]?.(arg);},querySelector(){return null;}};}
const slider=el(20),readout=el(),root=el();
root.querySelector=s=>s==='#speaker-volume-slider'?slider:s==='#speaker-volume-value'?readout:null;
const listeners={},requests=[];
const document={getElementById:id=>id==='speaker-volume-control'?root:null,
  addEventListener(k,fn){listeners[k]=fn;}};
const fetch=async(url,options)=>{
 if(url==='/api/auth/csrf')return {ok:true,json:async()=>({csrf_token:'abc'})};
 if(url==='/api/navigation/control/speaker-volume'){
  const value=JSON.parse(options.body).volume_percent;requests.push(value);
  return {ok:true,json:async()=>({ok:true,volume_percent:value})};
 }
 return {ok:true,json:async()=>({ok:true,connected:true,speaker_volume_percent:20})};
};
const ctx={document,fetch,window:{}};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync('frontend/services/static/speaker_volume.js','utf8'),ctx);
function state(n){listeners['dabom:navigation-control-state']({detail:{connected:true,speaker_volume_percent:n}});}
const ev=key=>({key,preventDefault(){this.prevented=true;}});
const flush=()=>new Promise(resolve=>setTimeout(resolve,10));
(async()=>{
 state(20);assert.strictEqual(slider.disabled,false);
 slider.dispatch('keydown',ev('ArrowRight'));slider.dispatch('keydown',ev('ArrowRight'));
 assert.strictEqual(Number(slider.value),30);assert.strictEqual(requests.length,0);
 slider.dispatch('keyup',ev('ArrowRight'));await flush();
 assert.strictEqual(requests.at(-1),30);
 slider.value=31;slider.dispatch('input');slider.dispatch('change');await flush();
 assert.strictEqual(requests.at(-1),31);
 slider.value=0;slider.dispatch('input');slider.dispatch('change');await flush();
 assert.strictEqual(requests.at(-1),0);
 slider.value=100;slider.dispatch('input');slider.dispatch('change');await flush();
 assert.strictEqual(requests.at(-1),100);
 slider.dispatch('keydown',ev('ArrowRight'));slider.dispatch('keyup',ev('ArrowRight'));
 await flush();assert.strictEqual(requests.at(-1),100);
 state(20);slider.dispatch('keydown',ev('ArrowLeft'));slider.dispatch('keyup',ev('ArrowLeft'));
 await flush();assert.strictEqual(requests.at(-1),15);
})().catch(e=>{process.stderr.write(String(e.stack));process.exitCode=1;});
'''
        result = subprocess.run(['node','-e',test],cwd=ROOT,capture_output=True,text=True,check=False)
        self.assertEqual(result.returncode,0,result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
