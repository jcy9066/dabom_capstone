from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / ".codex" / "hooks" / "pre_tool_policy.py"
SPEC = importlib.util.spec_from_file_location("dabom_pre_tool_policy", POLICY_PATH)
assert SPEC is not None and SPEC.loader is not None
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (r"C:\Users\tester\repo", r"C:\Users\tester\repo"),
        ("C:/Users/tester/repo", "C:/Users/tester/repo"),
        ("/c/Users/tester/repo", "C:/Users/tester/repo"),
        ("/C/Users/tester/repo", "C:/Users/tester/repo"),
        ("/c", "C:/"),
        (r"\\server\share\repo", r"\\server\share\repo"),
    ],
)
def test_normalize_native_windows_path_forms(value: str, expected: str) -> None:
    assert POLICY._normalize_native_path_text(value, os_name="nt") == expected


def test_non_windows_path_normalization_is_unchanged() -> None:
    assert (
        POLICY._normalize_native_path_text("/c/Users/tester/repo", os_name="posix")
        == "/c/Users/tester/repo"
    )


def _run_git(*args: str) -> None:
    completed = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def _msys_path(path: Path) -> str:
    forward = path.resolve().as_posix()
    drive, suffix = forward.split(":", 1)
    return f"/{drive.lower()}{suffix}"


@pytest.fixture
def linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    if os.name != "nt":
        pytest.skip("Windows linked-worktree path regression")

    repository = tmp_path / "repository"
    worktree = tmp_path / "feature-worktree"
    _run_git("init", "-b", "dev", str(repository))
    _run_git("-C", str(repository), "config", "user.name", "Codex Test")
    _run_git("-C", str(repository), "config", "user.email", "codex@example.invalid")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    _run_git("-C", str(repository), "add", "--", "tracked.txt")
    _run_git("-C", str(repository), "commit", "-m", "test: Create fixture")
    _run_git(
        "-C",
        str(repository),
        "worktree",
        "add",
        "-b",
        "feat/path-policy-test",
        str(worktree),
    )
    return repository, worktree


@pytest.mark.parametrize("key", ["cwd", "workdir"])
def test_msys_payload_cwd_resolves_to_native_linked_worktree(
    linked_worktree: tuple[Path, Path],
    key: str,
) -> None:
    _, worktree = linked_worktree
    resolved = POLICY._extract_tool_cwd({}, {key: _msys_path(worktree)})

    assert resolved == worktree.resolve()
    blocked, reason = POLICY._git_command_is_blocked(
        "git add -- tracked.txt",
        resolved,
    )
    assert blocked is False, reason


def test_git_dash_c_accepts_msys_linked_worktree_path(
    linked_worktree: tuple[Path, Path],
) -> None:
    repository, worktree = linked_worktree
    blocked, reason = POLICY._git_command_is_blocked(
        f'git -C "{_msys_path(worktree)}" commit -m "test: Validate path"',
        repository.resolve(),
    )

    assert blocked is False, reason


def test_protected_branch_remains_read_only(
    linked_worktree: tuple[Path, Path],
) -> None:
    repository, _ = linked_worktree
    blocked, reason = POLICY._git_command_is_blocked(
        "git add -- tracked.txt",
        repository.resolve(),
    )

    assert blocked is True
    assert "protected branch" in reason


@pytest.mark.parametrize(
    "command",
    [
        "git push origin dev",
        "git merge feat/example",
        "git rebase dev",
        "git cherry-pick deadbeef",
    ],
)
def test_remote_and_integration_operations_remain_blocked(command: str) -> None:
    blocked, _ = POLICY._git_command_is_blocked(command, ROOT)
    assert blocked is True
