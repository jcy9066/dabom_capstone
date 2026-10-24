"""Only RTMO-M + BotSORT + X3D-M is supported in the production pipeline."""
from pathlib import Path
import inspect

import pytest

from perception import pipeline_factory

ROOT = Path(__file__).resolve().parents[1]


def test_only_pipeline8_is_available():
    assert pipeline_factory.PIPELINE_OPTIONS == {"8": "RTMOM_BotSORT_X3DMScene"}


@pytest.mark.parametrize("legacy", ["1", "2", "3", "4", "5", "6", "7", "9", ""])
def test_old_pipeline_ids_fail_before_any_model_loading(monkeypatch, legacy):
    def fail_device(_device=None):
        raise AssertionError("Legacy models must not initialize CUDA")

    monkeypatch.setattr(pipeline_factory, "resolve_device", fail_device)
    with pytest.raises(ValueError, match="RTMO-M"):
        pipeline_factory.create_pipeline(legacy)


def test_single_pipeline_is_default_for_local_video():
    assert inspect.signature(pipeline_factory.create_pipeline).parameters["choice"].default == "8"
    source = (ROOT / "perception" / "main.py").read_text(encoding="utf-8")
    assert 'choices=["8"], default="8"' in source
    assert "print_pipeline_menu()" not in source


def test_launcher_has_only_rtmo_x3d_preflight():
    source = (ROOT / "start_gpu_server.sh").read_text(encoding="utf-8")
    assert 'Only PIPELINE=8 (RTMO-M + BotSORT + X3D-M) is supported' in source
    assert "from pytorchvideo.models.hub import x3d_m" in source
    assert "from ultralytics.trackers.bot_sort import BOTSORT" in source
    assert "import mmaction" not in source


def test_server_retains_model_toggle_but_restricts_pipeline_id():
    source = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
    assert 'if pipeline != "8":' in source
    assert '"pipelines": PIPELINE_OPTIONS' in source
    assert "def set_model_pipeline_enabled(enabled):" in source
