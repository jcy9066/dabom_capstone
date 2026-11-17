#!/usr/bin/env python3
"""Read-only GPU/Pi source identity check. Never sync or execute robot commands."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_SHA = re.compile(r"^[0-9a-f]{40}$")


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=True, timeout=10,
    )
    return completed.stdout.strip()


def inspect_checkout(root: Path) -> dict[str, object]:
    root = root.resolve()
    inside = _git(root, "rev-parse", "--show-toplevel")
    sha = _git(root, "rev-parse", "HEAD").lower()
    if not _SHA.fullmatch(sha):
        raise ValueError("Git returned an invalid HEAD SHA")
    branch = _git(root, "branch", "--show-current")
    dirty = bool(_git(root, "status", "--porcelain=v1", "--untracked-files=normal"))
    return {"schema": 1, "sha": sha, "branch": branch, "dirty": dirty,
            "checkout": str(Path(inside).resolve())}


def check_versions(local: dict, peer: dict | None, expected: str | None) -> list[str]:
    issues: list[str] = []
    if local.get("dirty") is not False:
        issues.append("LOCAL_WORKTREE_NOT_CLEAN")
    sha = local.get("sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        issues.append("LOCAL_HEAD_INVALID")
    if expected is not None and sha != expected.lower():
        issues.append("LOCAL_HEAD_NOT_EXPECTED")
    if peer is not None:
        if peer.get("schema") != 1 or not isinstance(peer.get("sha"), str) or not _SHA.fullmatch(peer["sha"]):
            issues.append("PEER_IDENTITY_INVALID")
        else:
            if peer.get("dirty") is not False:
                issues.append("PEER_WORKTREE_NOT_CLEAN")
            if peer["sha"] != sha:
                issues.append("GPU_PI_HEAD_MISMATCH")
            if expected is not None and peer["sha"] != expected.lower():
                issues.append("PEER_HEAD_NOT_EXPECTED")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--peer", type=Path, help="JSON from another host; no network access")
    parser.add_argument("--expected-sha", help="Approved 40-character release SHA")
    parser.add_argument("--output", type=Path, help="Write local identity JSON for offline comparison")
    args = parser.parse_args(argv)
    if args.expected_sha is not None and not _SHA.fullmatch(args.expected_sha.lower()):
        parser.error("--expected-sha must be 40 hexadecimal characters")
    try:
        local = inspect_checkout(args.root)
        peer = json.loads(args.peer.read_text(encoding="utf-8")) if args.peer else None
        if peer is not None and not isinstance(peer, dict):
            raise ValueError("peer JSON must be an object")
        issues = check_versions(local, peer, args.expected_sha)
        if args.output:
            args.output.write_text(json.dumps(local, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"local": local, "peer": peer, "issues": issues,
                          "safe_to_update_or_drive": False,
                          "compatible": not issues and peer is not None and args.expected_sha is not None},
                         ensure_ascii=False))
        return 2 if issues else 0
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(json.dumps({"compatible": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
