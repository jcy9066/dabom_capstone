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
