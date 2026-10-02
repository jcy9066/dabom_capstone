from pathlib import Path

from perception import model_assets


def test_pipeline_8_downloads_only_missing_assets_atomically(tmp_path, monkeypatch):
    downloaded = []

    def fake_download(asset, target):
        downloaded.append(asset.key)
        target.write_bytes(b"x" * asset.min_bytes)

    monkeypatch.setattr(model_assets, "_download_atomic", fake_download)

    first = model_assets.PIPELINE_8_ASSETS[0]
    existing = tmp_path / first.filename
    existing.write_bytes(b"x" * first.min_bytes)

    assets = model_assets.ensure_pipeline_assets("8", weights_dir=tmp_path)

    assert set(assets) == {"yolo_pose", "action_config", "action_checkpoint"}
    assert first.key not in downloaded
    assert set(downloaded) == {"action_config", "action_checkpoint"}
    assert all(Path(path).exists() for path in assets.values())


def test_non_pipeline_8_does_not_download_assets(tmp_path, monkeypatch):
    def fail_download(*_args, **_kwargs):
        raise AssertionError("unexpected download")

    monkeypatch.setattr(model_assets, "_download_atomic", fail_download)

    assert model_assets.ensure_pipeline_assets("7", weights_dir=tmp_path) == {}
