"""Map bundle write failure must not expose incomplete map metadata."""
import os

import pytest

from server.map_storage_integrity import commit_bundle


def test_manifest_is_committed_last_and_files_match(tmp_path):
    pgm = tmp_path / "map.pgm"
    raw = tmp_path / "map.raw.json"
    meta = tmp_path / "map.meta.json"
    commit_bundle({pgm: b"PGM", raw: b"RAW", meta: b"META"}, manifest=meta)
    assert (pgm.read_bytes(), raw.read_bytes(), meta.read_bytes()) == (
        b"PGM", b"RAW", b"META"
    )
    assert not list(tmp_path.glob("*.tmp"))


def test_write_failure_rolls_back_partial_bundle(tmp_path, monkeypatch):
    pgm = tmp_path / "map.pgm"
    raw = tmp_path / "map.raw.json"
    meta = tmp_path / "map.meta.json"
    original_replace = os.replace

    def fail_raw(src, dst):
        if dst == raw:
            raise OSError("forced disk failure")
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_raw)
    with pytest.raises(OSError, match="forced disk failure"):
        commit_bundle({pgm: b"PGM", raw: b"RAW", meta: b"META"}, manifest=meta)
    assert not pgm.exists() and not raw.exists() and not meta.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_bundle_must_not_overwrite_existing_map(tmp_path):
    pgm = tmp_path / "m.pgm"
    meta = tmp_path / "m.meta.json"
    pgm.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        commit_bundle({pgm: b"new", meta: b"meta"}, manifest=meta)
    assert pgm.read_bytes() == b"old"


def test_renamed_bundle_recomputes_hashes_and_remains_loadable(tmp_path):
    import hashlib
    import json
    from server.navigation_map_service import NavigationMapService

    map_dir = tmp_path / "maps"
    map_dir.mkdir()
    original = "original"
    image = b"P5\\n# AI patrol robot map\\n1 1\\n255\\n" + bytes([254])
    yaml = (f"image: {original}.pgm\\nresolution: 0.1\\n"
            "origin: [0.0, 0.0, 0.0]\\nnegate: 0\\n"
            "occupied_thresh: 0.65\\nfree_thresh: 0.196\\n").encode()
    raw = json.dumps({
        "map_name": original, "width": 1, "height": 1,
        "resolution": 0.1, "origin": {"x": 0, "y": 0, "yaw": 0},
        "data": [0], "data_encoding": "raw",
    }).encode()
    paths = {
        "pgm": map_dir / f"{original}.pgm",
        "yaml": map_dir / f"{original}.yaml",
        "raw": map_dir / f"{original}.raw.json",
        "meta": map_dir / f"{original}.meta.json",
    }
    meta = {
        "map_name": original, "width": 1, "height": 1, "resolution": 0.1,
        "origin": {"x": 0, "y": 0, "yaw": 0}, "bundle_version": 2,
        "files": {key: str(path.relative_to(tmp_path)) for key, path in paths.items()},
        "checksums": {
            "pgm": hashlib.sha256(image).hexdigest(),
            "yaml": hashlib.sha256(yaml).hexdigest(),
            "raw": hashlib.sha256(raw).hexdigest(),
        },
    }
    for key, content in (
        ("pgm", image), ("yaml", yaml), ("raw", raw),
        ("meta", json.dumps(meta).encode()),
    ):
        paths[key].write_bytes(content)
    service = NavigationMapService(tmp_path, map_dir)
    selected = service.get_map(original)
    renamed = service._renamed_contents(selected, "renamed")
    for key, content in renamed.items():
        (map_dir / f"renamed{'.raw.json' if key == 'raw' else '.meta.json' if key == 'meta' else '.' + key}").write_bytes(content)
    assert service.get_map("renamed").map_name == "renamed"
