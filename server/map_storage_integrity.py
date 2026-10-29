"""Atomic creation of a four-file saved map bundle.

The metadata manifest is committed last; partial bundles cannot be listed.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Mapping


def commit_bundle(
    payloads: Mapping[Path, bytes], *, manifest: Path,
) -> None:
    if manifest not in payloads:
        raise ValueError("manifest must be included")
    if any(path.exists() for path in payloads):
        raise FileExistsError("map bundle destination already exists")
    staged: dict[Path, Path] = {}
    committed: list[Path] = []
    try:
        for path, data in payloads.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
            )
            staged[path] = Path(temp)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        for path in (*[p for p in payloads if p != manifest], manifest):
            # Manifest is the final visibility/commit marker.
            os.replace(staged[path], path)
            committed.append(path)
    except Exception:
        # Roll back only freshly created files, never an existing map.
        for path in reversed(committed):
            path.unlink(missing_ok=True)
        raise
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)
