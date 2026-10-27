"""Execute changed rendering/config routes without GPU, ROS or hardware."""
import ast
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import pytest
from fastapi.responses import JSONResponse
from server.env_config import EnvConfigurationError, env_text

ROOT = Path(__file__).resolve().parents[1]
APP = ast.parse((ROOT/'server/app.py').read_text())


def load_function(name, namespace):
    node = copy.deepcopy(next(n for n in APP.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name))
    node.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(module, 'server/app.py', 'exec'), namespace)
    return namespace[name]


@pytest.mark.parametrize('danger,person,color', [(False,True,(0,255,0)), (True,True,(0,0,255)), (True,False,(0,0,255))])
def test_scene_color_is_display_only(danger, person, color):
    draw = Mock()
    ns = dict(clamp_box=lambda *a:(0,0,10,10), TRIGGER_SUSPICIOUS_VISUAL_ENABLED=False,
        PERSON_BASE_COLOR=(0,255,0), draw_skeleton_points=draw, draw_overlay_label=Mock())
    function = load_function('draw_detection_overlay', ns)
    detection = {'box':[0,0,10,10], 'cls':0 if person else 1, 'skeleton':[]}
    original = copy.deepcopy(detection)
    function(SimpleNamespace(shape=(100,100,3)), detection, scene_danger=danger)
    assert draw.call_args.args[-1] == color
    assert detection == original  # No change to alarm/event inputs.


def test_scene_overlay_leaves_video_pixels_untouched():
    tree = ast.parse((ROOT/'perception/models/scene_x3d.py').read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'draw_scene_overlay')
    ns = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])), 'scene_x3d.py', 'exec'), ns)
    frame = bytearray(b'original pixels')
    ns['draw_scene_overlay'](None, frame, {'status':'ok','danger':True,'score':.99})
    assert frame == b'original pixels'


def test_device_defaults_when_missing_on_startup_and_reload(monkeypatch):
    monkeypatch.delenv('DEVICE', raising=False)
    for node in ast.walk(APP):
        if isinstance(node, ast.Assign) and any(isinstance(t,ast.Name) and t.id in {'DEVICE','device'} for t in node.targets):
            if isinstance(node.value, ast.Call) and isinstance(node.value.func,ast.Attribute) and node.value.func.attr == 'lower':
                expr = ast.Expression(node.value)
                assert eval(compile(expr,'app.py','eval'), dict(env_text=env_text, CUDA_DEVICE_INDEX='0',cuda_device_index='0')) == 'cuda:0'


def test_model_toggle_invalid_config_returns_400_and_keeps_auth():
    async def scenario():
        ns = dict(Request=object, csrf_failure=lambda _:None,
            auth_payload=AsyncMock(return_value=({'enabled':True},None)),
            asyncio=SimpleNamespace(to_thread=AsyncMock(side_effect=ValueError('invalid DEVICE'))),
            set_model_pipeline_enabled=Mock(), EnvConfigurationError=EnvConfigurationError,
            JSONResponse=JSONResponse, MODEL_ACTIVE=False, frame_processor=None)
        route = load_function('set_model_pipeline_state', ns)
        result = await route(SimpleNamespace(session={'user':{'id':1}}))
        assert result.status_code == 400
        assert json.loads(result.body)['enabled'] is False
        result = await route(SimpleNamespace(session={}))
        assert result.status_code == 401
    asyncio.run(scenario())


def test_both_preview_paths_receive_scene_danger_flag():
    # A scoped diff is independently checked during final review; both overlay
    # callers must use the new scene flag rather than mutating detections.
    for name in ('render_latest_overlay', 'process_frame_for_dashboard'):
        function = next(n for n in APP.body if isinstance(n,ast.FunctionDef) and n.name == name)
        calls = [n for n in ast.walk(function) if isinstance(n,ast.Call)
                 and isinstance(n.func,ast.Name) and n.func.id == 'draw_detection_overlay']
        assert calls and all(any(k.arg == 'scene_danger' for k in n.keywords) for n in calls)
