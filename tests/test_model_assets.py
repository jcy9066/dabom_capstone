import io
import os
import zipfile
from pathlib import Path

import pytest

from perception import model_assets


def make_rtmo_zip(path: Path, payload_size=None):
    payload_size = payload_size or model_assets.RTMO_ONNX_MIN_BYTES
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr(
            "sdk/models/rtmo/end2end.onnx",
            b"x" * payload_size,
        )


def test_pipeline8_downloads_and_extracts_rtmo_onnx(tmp_path, monkeypatch):
    downloaded = []

    def fake_download(asset, target):
        downloaded.append(asset.key)
        if asset.key == "rtmo_archive":
            make_rtmo_zip(target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x" * asset.min_bytes)

    monkeypatch.setattr(model_assets, "_download_atomic", fake_download)

    assets = model_assets.ensure_pipeline_assets("8", weights_dir=tmp_path)

    assert set(assets) == {"rtmo_onnx", "scene_checkpoint"}
    assert downloaded == ["rtmo_archive", "scene_checkpoint"]
    assert assets["rtmo_onnx"].name == model_assets.RTMO_ONNX_NAME
    assert assets["rtmo_onnx"].stat().st_size >= model_assets.RTMO_ONNX_MIN_BYTES
    assert assets["scene_checkpoint"].exists()


def test_existing_rtmo_onnx_skips_archive_download(tmp_path, monkeypatch):
    rtmo = tmp_path / "rtmo" / model_assets.RTMO_ONNX_NAME
    rtmo.parent.mkdir(parents=True, exist_ok=True)
    rtmo.write_bytes(b"x" * model_assets.RTMO_ONNX_MIN_BYTES)

    downloaded = []

    def fake_download(asset, target):
        downloaded.append(asset.key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * asset.min_bytes)

    monkeypatch.setattr(model_assets, "_download_atomic", fake_download)

    assets = model_assets.ensure_pipeline_assets("8", weights_dir=tmp_path)

    assert "rtmo_archive" not in downloaded
    assert downloaded == ["scene_checkpoint"]
    assert assets["rtmo_onnx"] == rtmo


def test_rtmo_archive_must_contain_exactly_one_end2end_onnx(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "a/end2end.onnx",
            b"x" * model_assets.RTMO_ONNX_MIN_BYTES,
        )
        bundle.writestr(
            "b/end2end.onnx",
            b"x" * model_assets.RTMO_ONNX_MIN_BYTES,
        )

    with pytest.raises(RuntimeError, match="exactly one end2end.onnx"):
        model_assets._extract_rtmo_onnx_atomic(
            archive,
            tmp_path / "rtmo.onnx",
        )


def test_pipeline8_asset_urls_are_pinned_to_expected_models():
    assert model_assets.RTMO_ARCHIVE_NAME in model_assets.RTMO_ARCHIVE.url
    assert "onnx_sdk" in model_assets.RTMO_ARCHIVE.url
    assert "39e78cc4_20231211" in model_assets.RTMO_ARCHIVE.url
    assert model_assets.X3D_MODEL_REVISION in model_assets.SCENE_CHECKPOINT.url
    assert "final_x3d_realtime.pt" in model_assets.SCENE_CHECKPOINT.url


def test_non_pipeline8_does_not_download_assets(tmp_path, monkeypatch):
    def fail_download(*_args, **_kwargs):
        raise AssertionError("unexpected download")

    monkeypatch.setattr(model_assets, "_download_atomic", fail_download)

    assert model_assets.ensure_pipeline_assets("7", weights_dir=tmp_path) == {}


@pytest.mark.skipif(
    os.getenv("RUN_MODEL_ASSET_INTEGRATION") != "1",
    reason="requires network plus ONNX Runtime/PyTorchVideo runtime",
)
def test_pipeline8_real_download_files_exist(tmp_path):
    assets = model_assets.ensure_pipeline_assets("8", weights_dir=tmp_path)

    assert assets["rtmo_onnx"].stat().st_size >= model_assets.RTMO_ONNX_MIN_BYTES
    assert (
        assets["scene_checkpoint"].stat().st_size
        >= model_assets.SCENE_CHECKPOINT.min_bytes
    )


def test_rtmo_asset_has_huggingface_mirror():
    assert model_assets.RTMO_ARCHIVE.fallback_urls
    assert any(
        "huggingface.co/Tau-J/RTMPose/resolve/main/rtmo/onnx_sdk/"
        in url
        for url in model_assets.RTMO_ARCHIVE.fallback_urls
    )
