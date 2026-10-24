from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any


# Local Git policy:
# - Approved read-only Git commands are allowed.
# - git switch --no-guess to an existing branch is allowed.
# - New branch creation is allowed without delete/rename/force options.
# - git worktree add -b is allowed; destructive worktree operations are blocked.
# - git add/new commit are allowed only on non-protected linked worktrees.
# - main/dev are protected from add/commit.
# - Remote operations, merges/rebases, history rewriting, checkout/restore/reset,
#   amend, branch deletion/rename/force, and worktree removal/prune remain blocked.
PROTECTED_BRANCHES = frozenset({"main", "dev"})

MUTATING_GIT_SUBCOMMANDS = frozenset(
    {
        "am",
        "apply",
        "bisect",
        "checkout",
        "cherry-pick",
        "clean",
        "clone",
        "fetch",
        "init",
        "merge",
        "mv",
        "pull",
        "push",
        "rebase",
        "reset",
        "restore",
        "revert",
        "rm",
        "stash",
        "tag",
    }
)

READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {
        "blame",
        "cat-file",
        "cherry",
        "count-objects",
        "describe",
        "diff",
        "diff-files",
        "diff-index",
        "diff-tree",
        "for-each-ref",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "merge-base",
        "name-rev",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "show-ref",
        "status",
        "verify-commit",
        "verify-tag",
    }
)

MUTATING_GITHUB_TOOL_SUFFIXES = frozenset(
    {
        "create_blob",
        "create_branch",
        "create_commit",
        "create_file",
        "create_tree",
        "delete_file",
        "merge_pull_request",
        "update_file",
        "update_ref",
    }
)

ALLOWED_COMMIT_TYPES = frozenset(
    {
        "feat",
        "fix",
        "docs",
        "style",
        "design",
        "test",
        "refactor",
        "build",
        "ci",
        "perf",
        "chore",
        "rename",
        "remove",
    }
)

# Accept raw/path-qualified Git and RTK-wrapped Git at shell-command boundaries.
_GIT_INVOCATION_RE = re.compile(
    r"(?ix)(?:^|[\r\n;&|`!]|\$\()\s*"
    r"(?:(?:env|command|cmd(?:\.exe)?|sudo|powershell(?:\.exe)?|pwsh(?:\.exe)?|"
    r"bash|sh)\b[^\r\n;&|()`]*?\s+[\"']?)*"
    r"((?:rtk(?:\.exe)?\s+)?(?:"
    r"\"[^\"\r\n]*[\\/]git(?:\.exe)?\""
    r"|'[^'\r\n]*[\\/]git(?:\.exe)?'"
    r"|(?:[^\s\r\n;&|()`]*[\\/])?git(?:\.exe)?"
    r")(?=\s|$)[^\r\n;&|)`]*)"
)

_GIT_GLOBAL_FLAGS = frozenset(
    {
        "--bare",
        "--glob-pathspecs",
        "--icase-pathspecs",
        "--literal-pathspecs",
        "--no-optional-locks",
        "--no-pager",
        "--no-replace-objects",
        "--noglob-pathspecs",
        "--paginate",
    }
)

_GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {
        "-C",
        "-c",
        "--config-env",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
    }
)

_SUBJECT_RE = re.compile(r"^(?P<type>[a-z]+): (?P<summary>.+)$")


def _read_payload() -> dict[str, Any]:
    try:
        if sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
    except Exception:
        return {}

    if not raw.strip():
        return {}

    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    return value if isinstance(value, dict) else {}


def _first_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _tool_name(payload: dict[str, Any]) -> str:
    return str(
        _first_value(payload, "tool_name", "toolName", "tool", "name") or ""
    ).strip()


def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    value = _first_value(
        payload,
        "tool_input",
        "toolInput",
        "input",
        "arguments",
        "args",
    )
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return {"command": value}
    return {}


def _extract_command(tool_input: dict[str, Any]) -> str:
    for key in ("command", "cmd", "script", "code", "input"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            parts = [str(part) for part in value if part is not None]
            if parts:
                return " ".join(parts)

    argv = tool_input.get("argv")
    if isinstance(argv, list):
        return " ".join(str(part) for part in argv)

    return ""


_MSYS_DRIVE_PATH_RE = re.compile(r"^/([A-Za-z])(?:/(.*))?$")


def _normalize_native_path_text(value: str, *, os_name: str | None = None) -> str:
    platform = os.name if os_name is None else os_name
    if platform != "nt":
        return value

    match = _MSYS_DRIVE_PATH_RE.fullmatch(value)
    if match is None:
        return value

    drive = match.group(1).upper()
    suffix = match.group(2)
    return f"{drive}:/{suffix}" if suffix else f"{drive}:/"


def _extract_tool_cwd(payload: dict[str, Any], tool_input: dict[str, Any]) -> Path:
    for mapping in (tool_input, payload):
        for key in ("cwd", "workdir", "working_directory", "workingDirectory"):
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                return Path(_normalize_native_path_text(value)).expanduser().resolve()
    return Path.cwd().resolve()


def _github_tool_is_mutating(tool_name: str) -> tuple[bool, str]:
    normalized = tool_name.strip().lower()
    for suffix in MUTATING_GITHUB_TOOL_SUFFIXES:
        if (
            normalized.endswith("." + suffix)
            or normalized.endswith("__" + suffix)
            or normalized == suffix
        ):
            return True, suffix
    return False, ""


def _extract_commit_message(segment: str) -> tuple[str | None, str | None]:
    try:
        argv = shlex.split(segment, posix=True)
    except ValueError:
        return None, "commit command quoting is invalid"

    messages: list[str] = []
    index = 0

    while index < len(argv):
        token = argv[index]
        if token in ("-m", "--message"):
            if index + 1 >= len(argv):
                return None, "git commit message is missing"
            messages.append(argv[index + 1])
            index += 2
            continue
        if token.startswith("--message="):
            messages.append(token.split("=", 1)[1])
        elif token.startswith("-m") and token != "-m":
            messages.append(token[2:])
        index += 1

    if len(messages) != 1:
        return None, 'use one-line git commit -m "type: Summary"'

    return messages[0], None


def _commit_args_are_blocked(args: list[str]) -> tuple[bool, str]:
    message_count = 0
    index = 0

    while index < len(args):
        token = args[index]
        if token in {"-m", "--message"}:
            if index + 1 >= len(args):
                return True, "git commit message is missing"
            message_count += 1
            index += 2
            continue
        if token.startswith("--message="):
            message_count += 1
            index += 1
            continue
        if token.startswith("-m") and token != "-m":
            message_count += 1
            index += 1
            continue
        return True, f"git commit argument is not approved: {token}"

    if message_count != 1:
        return True, 'use one-line git commit -m "type: Summary"'

    return False, ""


def _validate_commit_message(message: str) -> str | None:
    if "\n" in message or "\r" in message:
        return "commit message must be one line"

    subject = message.strip()
    if not subject:
        return "commit message is empty"
    if len(subject) > 50:
        return f"commit message exceeds 50 chars: {len(subject)}"
    if subject.endswith("."):
        return "commit message must not end with a period"

    match = _SUBJECT_RE.fullmatch(subject)
    if match is None:
        return "commit message must match 'type: Summary'"

    commit_type = match.group("type")
    if commit_type not in ALLOWED_COMMIT_TYPES:
        return f"unsupported commit type: {commit_type}"

    if not match.group("summary").strip():
        return "commit summary is empty"

    return None


def _resolve_git_cwd(base_cwd: Path, value: str) -> Path:
    candidate = Path(_normalize_native_path_text(value)).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (base_cwd / candidate).resolve()


def _parse_git_invocation(
    segment: str,
    default_cwd: Path,
) -> tuple[str | None, list[str], Path, str | None]:
    try:
        argv = shlex.split(segment, posix=True)
    except ValueError:
        return None, [], default_cwd, "git command quoting is invalid"

    index = 0
    if index < len(argv) and argv[index].lower() in {"rtk", "rtk.exe"}:
        index += 1
    if index >= len(argv):
        return None, [], default_cwd, "git command parsing failed"

    executable = argv[index].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if executable not in {"git", "git.exe"}:
        return None, [], default_cwd, "git command parsing failed"
    index += 1

    git_cwd = default_cwd

    while index < len(argv) and argv[index].startswith("-"):
        token = argv[index]
        if token == "--":
            index += 1
            break
        if token in _GIT_GLOBAL_FLAGS:
            index += 1
            continue
        if token in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            if index + 1 >= len(argv):
                return None, [], git_cwd, f"git global option value is missing: {token}"
            value = argv[index + 1]
            if token == "-C":
                git_cwd = _resolve_git_cwd(git_cwd, value)
            index += 2
            continue
        if token.startswith("-C") and token != "-C":
            git_cwd = _resolve_git_cwd(git_cwd, token[2:])
            index += 1
            continue
        if token.startswith("-c") and token != "-c":
            index += 1
            continue
        if any(
            token.startswith(option + "=")
            for option in _GIT_GLOBAL_OPTIONS_WITH_VALUE
            if option.startswith("--")
        ):
            index += 1
            continue
        return None, [], git_cwd, f"unsupported git global option: {token}"

    if index >= len(argv):
        return None, [], git_cwd, "git subcommand is missing"

    return argv[index].lower(), argv[index + 1 :], git_cwd, None


def _switch_is_blocked(args: list[str]) -> tuple[bool, str]:
    if args.count("--no-guess") != 1:
        return True, "git switch requires --no-guess"

    branch_args = [token for token in args if token != "--no-guess"]
    if len(branch_args) != 1 or branch_args[0].startswith("-"):
        return True, "git switch only permits one existing branch"

    return False, ""


def _valid_new_branch_name(branch: str) -> tuple[bool, str]:
    normalized = branch.strip()
    if not normalized or normalized.startswith("-"):
        return False, "new branch name is invalid"
    if normalized in PROTECTED_BRANCHES:
        return False, f"protected branch cannot be created or replaced: {normalized}"
    if normalized in {"HEAD", "@"}:
        return False, "new branch name is invalid"
    if any(part in normalized for part in ("..", "~", "^", ":", "?", "*", "[", "\\")):
        return False, "new branch name contains unsafe ref syntax"
    if normalized.startswith("/") or normalized.endswith("/") or normalized.endswith("."):
        return False, "new branch name is invalid"
    if "//" in normalized or "@{" in normalized:
        return False, "new branch name is invalid"
    return True, ""


def _branch_is_blocked(args: list[str]) -> tuple[bool, str]:
    if not args:
        return False, ""

    read_only_flags = {
        "-a",
        "-l",
        "-r",
        "-v",
        "-vv",
        "--all",
        "--contains",
        "--list",
        "--merged",
        "--no-merged",
        "--remotes",
        "--show-current",
    }
    has_read_only_flag = False

    for token in args:
        if token in read_only_flags or token.startswith(
            ("--contains=", "--merged=", "--no-merged=")
        ):
            has_read_only_flag = True
            continue
        if token.startswith("-"):
            return True, f"git branch option is not approved: {token}"

    if has_read_only_flag:
        return False, ""

    # Creation only: git branch <new-branch> [<start-point>]
    if len(args) not in {1, 2}:
        return True, "git branch only permits read-only listing or simple branch creation"
    valid, reason = _valid_new_branch_name(args[0])
    if not valid:
        return True, reason
    if len(args) == 2 and args[1].startswith("-"):
        return True, "git branch start point is invalid"
    return False, ""


def _worktree_is_blocked(args: list[str]) -> tuple[bool, str]:
    if not args:
        return True, "git worktree requires an approved subcommand"

    subcommand = args[0].lower()
    rest = args[1:]

    if subcommand == "list":
        allowed = {"--porcelain", "-v", "--verbose", "-z"}
        for token in rest:
            if token not in allowed:
                return True, f"git worktree list option is not approved: {token}"
        return False, ""

    if subcommand != "add":
        return True, f"git worktree {subcommand} is blocked"

    # Creation only:
    #   git worktree add -b <new-branch> <path> [<start-point>]
    if len(rest) not in {3, 4} or rest[0] != "-b":
        return True, "git worktree only permits: add -b <branch> <path> [<start-point>]"

    branch = rest[1]
    path = rest[2]
    start_point = rest[3] if len(rest) == 4 else None

    valid, reason = _valid_new_branch_name(branch)
    if not valid:
        return True, reason
    if not path or path in {".", ".."} or path.startswith("-"):
        return True, "git worktree target path is not approved"
    if start_point is not None and start_point.startswith("-"):
        return True, "git worktree start point is invalid"

    return False, ""


def _run_git(cwd: Path, *args: str) -> tuple[str | None, str | None]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
    except Exception as exc:
        return None, f"git context check failed: {exc}"

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown git error"
        return None, f"git context check failed: {detail}"
    return completed.stdout.strip(), None


def _write_context_is_blocked(cwd: Path) -> tuple[bool, str]:
    branch, error = _run_git(cwd, "branch", "--show-current")
    if error:
        return True, error
    if not branch:
        return True, "git add/commit are blocked on detached HEAD"
    if branch in PROTECTED_BRANCHES:
        return True, f"git add/commit are blocked on protected branch: {branch}"

    git_dir, error = _run_git(cwd, "rev-parse", "--git-dir")
    if error:
        return True, error
    common_dir, error = _run_git(cwd, "rev-parse", "--git-common-dir")
    if error:
        return True, error

    git_dir_path = _resolve_git_cwd(cwd, git_dir or ".git")
    common_dir_path = _resolve_git_cwd(cwd, common_dir or ".git")
    if git_dir_path == common_dir_path:
        return True, "git add/commit are allowed only inside a linked Git worktree"

    return False, ""


def _git_command_is_blocked(
    command: str,
    default_cwd: Path,
) -> tuple[bool, str]:
    if not command:
        return False, ""

    for match in _GIT_INVOCATION_RE.finditer(command):
        segment = match.group(1)
        subcommand, args, git_cwd, error = _parse_git_invocation(segment, default_cwd)
        if error:
            return True, error

        if subcommand in MUTATING_GIT_SUBCOMMANDS:
            return True, f"git {subcommand}"

        if subcommand == "switch":
            blocked, reason = _switch_is_blocked(args)
            if blocked:
                return True, reason
            continue

        if subcommand == "branch":
            blocked, reason = _branch_is_blocked(args)
            if blocked:
                return True, reason
            continue

        if subcommand == "worktree":
            blocked, reason = _worktree_is_blocked(args)
            if blocked:
                return True, reason
            continue

        if subcommand == "commit":
            blocked, reason = _write_context_is_blocked(git_cwd)
            if blocked:
                return True, reason
            blocked, reason = _commit_args_are_blocked(args)
            if blocked:
                return True, reason
            message, error = _extract_commit_message(segment)
            if error:
                return True, error
            error = _validate_commit_message(message or "")
            if error:
                return True, error
            continue

        if subcommand == "add":
            blocked, reason = _write_context_is_blocked(git_cwd)
            if blocked:
                return True, reason
            continue

        if subcommand not in READ_ONLY_GIT_SUBCOMMANDS:
            return True, f"git subcommand is not approved: {subcommand}"

    return False, ""


def _deny_decision(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
            "additionalContext": reason,
        }
    }


def main() -> int:
    payload = _read_payload()
    tool_name = _tool_name(payload)

    github_blocked, github_action = _github_tool_is_mutating(tool_name)
    if github_blocked:
        reason = (
            "GitHub MCP repository writes are blocked: "
            f"{github_action}. Local branch/worktree creation and linked-worktree "
            "git add/new commit only."
        )
        print(json.dumps(_deny_decision(reason), ensure_ascii=False))
        return 0

    tool_input = _tool_input(payload)
    command = _extract_command(tool_input)
    default_cwd = _extract_tool_cwd(payload, tool_input)
    git_blocked, reason = _git_command_is_blocked(command, default_cwd)
    if git_blocked:
        print(
            json.dumps(
                _deny_decision(
                    f"Git policy blocked this operation: {reason}. "
                    "main/dev are read-only bases; automated writes require a "
                    "non-protected linked worktree. Branch/worktree creation is "
                    "limited to simple creation; remote operations, integration, "
                    "destructive worktree operations, and history rewriting remain "
                    "user-managed."
                ),
                ensure_ascii=False,
            )
        )
        return 0

    # No explicit "allow" decision: current Codex treats permissionDecision=allow
    # without updatedInput as an unsupported PreToolUse result.
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
