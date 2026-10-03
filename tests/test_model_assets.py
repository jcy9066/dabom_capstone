import os
from pathlib import Path

import pytest

from perception import model_assets


def test_pipeline_8_downloads_only_missing_assets_atomically(tmp_path, monkeypatch):
    downloaded = []

    def fake_download(asset, target):
        downloaded.append(asset.key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * asset.min_bytes)

    monkeypatch.setattr(model_assets, "_download_atomic", fake_download)

    first = model_assets.PIPELINE_8_ASSETS[0]
    existing = tmp_path / first.relative_path
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"x" * first.min_bytes)

    assets = model_assets.ensure_pipeline_assets("8", weights_dir=tmp_path)

    assert set(assets) == {
        "yolo_pose",
        "action_base_config",
        "action_config",
        "action_checkpoint",
    }
    assert first.key not in downloaded
    assert set(downloaded) == {
        "action_base_config",
        "action_config",
        "action_checkpoint",
    }
    assert all(Path(path).exists() for path in assets.values())


def test_action_config_preserves_mmaction_relative_base_layout(tmp_path, monkeypatch):
    def fake_download(asset, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * asset.min_bytes)

    monkeypatch.setattr(model_assets, "_download_atomic", fake_download)
    assets = model_assets.ensure_pipeline_assets("8", weights_dir=tmp_path)

    expected_base = (
        Path(assets["action_config"]).parent / "../../_base_/default_runtime.py"
    ).resolve()
    assert expected_base == Path(assets["action_base_config"]).resolve()


def test_non_pipeline_8_does_not_download_assets(tmp_path, monkeypatch):
    def fail_download(*_args, **_kwargs):
        raise AssertionError("unexpected download")

    monkeypatch.setattr(model_assets, "_download_atomic", fail_download)

    assert model_assets.ensure_pipeline_assets("7", weights_dir=tmp_path) == {}


@pytest.mark.skipif(
    os.getenv("RUN_MODEL_ASSET_INTEGRATION") != "1",
    reason="requires network plus MMAction2/MMEngine runtime",
)
def test_pipeline_8_real_download_config_and_model_init(tmp_path):
    mmengine_config = pytest.importorskip("mmengine.config")
    mmaction_apis = pytest.importorskip("mmaction.apis")

    assets = model_assets.ensure_pipeline_assets("8", weights_dir=tmp_path)

    config = mmengine_config.Config.fromfile(str(assets["action_config"]))
    assert config.model.type == "RecognizerGCN"

    model = mmaction_apis.init_recognizer(
        str(assets["action_config"]),
        str(assets["action_checkpoint"]),
        device="cpu",
    )
    assert model is not None
