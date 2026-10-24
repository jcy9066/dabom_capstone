from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


# ---------------------------------------------------------------------------
# Codex Stop hook
# ---------------------------------------------------------------------------
#
# 기존 목적:
# - 변경 영역에 맞는 validation이 수행되었는지 확인한다.
# - dashboard 변경 시 Playwright 검증 여부를 확인한다.
# - 마지막 assistant message에 validation 결과가 보고되었는지 확인한다.
# - 누락된 경우 Stop을 block한다.
#
# Windows 호환:
# - fcntl을 무조건 import하지 않는다.
# - Windows에서는 msvcrt, Unix/WSL에서는 fcntl을 조건부 사용한다.
# - /tmp 고정 경로 대신 tempfile.gettempdir()을 사용한다.
#
# 이 hook은 Git 상태를 변경하지 않는다.
# ---------------------------------------------------------------------------


STATE_ROOT = Path(tempfile.gettempdir()) / "dabom_codex_hooks"

VALIDATOR_BY_AREA = {
    "server": "validate-server",
    "dashboard": "validate-dashboard",
    "navigation": "validate-navigation",
    "raspberry": "validate-raspberry",
}

VALIDATION_REPORT_MARKERS = (
    "PASS",
    "FAIL",
    "WARN",
    "validation",
    "검증",
)

PLAYWRIGHT_MARKERS = (
    "playwright",
    "browser_",
)

VALIDATOR_COMMAND_MARKERS = {
    "validate-server": ".agents/skills/validate-server/scripts/validate_server.py",
    "validate-navigation": (
        ".agents/skills/validate-navigation/scripts/validate_navigation.py"
    ),
    "validate-raspberry": (
        ".agents/skills/validate-raspberry/scripts/validate_uart_protocol.py"
    ),
    "review-change": ".agents/skills/review-change/scripts/review_change.py",
}


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
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    return payload if isinstance(payload, dict) else {}


def _first_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _string_value(mapping: dict[str, Any], *keys: str) -> str:
    value = _first_value(mapping, *keys)
    return str(value or "").strip()


def _session_id(payload: dict[str, Any]) -> str:
    value = _string_value(
        payload,
        "session_id",
        "sessionId",
        "conversation_id",
        "conversationId",
    )
    return value or "default-session"


def _turn_id(payload: dict[str, Any]) -> str:
    value = _string_value(
        payload,
        "turn_id",
        "turnId",
        "response_id",
        "responseId",
    )
    return value or "default-turn"


def _last_assistant_message(payload: dict[str, Any]) -> str:
    return _string_value(
        payload,
        "last_assistant_message",
        "lastAssistantMessage",
        "assistant_message",
        "assistantMessage",
    )


def _safe_component(value: str) -> str:
    cleaned = "".join(
        ch if ch.isalnum() or ch in "-_." else "_"
        for ch in value
    )
    return cleaned[:160] or "unknown"


def _state_path(payload: dict[str, Any]) -> Path:
    return (
        STATE_ROOT
        / _safe_component(_session_id(payload))
        / f"{_safe_component(_turn_id(payload))}.json"
    )


@contextmanager
def _locked_file(
    path: Path,
    mode: str,
) -> Iterator[Any]:
    """
    Windows / Unix 공통 advisory lock.

    lock 자체가 불가능한 특수 환경에서는 파일 I/O를 실패시키지 않고
    best-effort로 진행한다. Stop hook crash보다 validation 판단을 우선한다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # a+는 Windows msvcrt.locking을 위해 최소 1 byte를 확보하기 쉽다.
    file_obj = path.open(
        mode,
        encoding="utf-8",
    )

    locked = False

    try:
        if os.name == "nt":
            try:
                import msvcrt

                file_obj.seek(0, os.SEEK_END)

                if file_obj.tell() == 0 and "+" in mode:
                    file_obj.write(" ")
                    file_obj.flush()

                file_obj.seek(0)
                msvcrt.locking(
                    file_obj.fileno(),
                    msvcrt.LK_LOCK,
                    1,
                )
                locked = True
            except (ImportError, OSError):
                locked = False
        else:
            try:
                import fcntl

                fcntl.flock(
                    file_obj.fileno(),
                    fcntl.LOCK_EX,
                )
                locked = True
            except (ImportError, OSError):
                locked = False

        yield file_obj
    finally:
        if locked:
            if os.name == "nt":
                try:
                    import msvcrt

                    file_obj.seek(0)
                    msvcrt.locking(
                        file_obj.fileno(),
                        msvcrt.LK_UNLCK,
                        1,
                    )
                except (ImportError, OSError):
                    pass
            else:
                try:
                    import fcntl

                    fcntl.flock(
                        file_obj.fileno(),
                        fcntl.LOCK_UN,
                    )
                except (ImportError, OSError):
                    pass

        file_obj.close()


def _load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}

    try:
        with _locked_file(path, "r+") as file_obj:
            file_obj.seek(0)
            raw = file_obj.read().strip()
    except OSError:
        return {}

    if not raw:
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    return data if isinstance(data, dict) else {}


def _repo_root(payload: dict[str, Any]) -> Path:
    explicit = _string_value(
        payload,
        "cwd",
        "working_directory",
        "workingDirectory",
    )

    candidates = []

    if explicit:
        candidates.append(Path(explicit))

    candidates.append(Path.cwd())

    try:
        candidates.extend(Path(__file__).resolve().parents)
    except OSError:
        pass

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()

        for current in (resolved, *resolved.parents):
            if (current / ".git").exists() and (current / "AGENTS.md").exists():
                return current

    return Path.cwd()


def _git_changed_files(root: Path) -> list[str]:
    """
    read-only Git 조회만 수행한다.
    Git 상태를 변경하지 않는다.
    """
    try:
        completed = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10.0,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if completed.returncode != 0:
        return []

    changed: list[str] = []

    for raw_line in completed.stdout.splitlines():
        if len(raw_line) < 4:
            continue

        value = raw_line[3:].strip()

        # rename: "old -> new"
        if " -> " in value:
            value = value.split(" -> ", 1)[1].strip()

        value = value.strip('"').replace("\\", "/")

        if value:
            changed.append(value)

    return sorted(set(changed))


def _changed_files(
    payload: dict[str, Any],
    state: dict[str, Any],
) -> list[str]:
    for source in (state, payload):
        value = _first_value(
            source,
            "changed_files",
            "changedFiles",
        )

        if isinstance(value, list):
            normalized = [
                str(item).replace("\\", "/").lstrip("./")
                for item in value
                if str(item).strip()
            ]

            if normalized:
                return sorted(set(normalized))

    return _git_changed_files(_repo_root(payload))


def _required_validators(changed_files: list[str]) -> set[str]:
    required: set[str] = set()

    for raw_path in changed_files:
        path = raw_path.replace("\\", "/").lstrip("./")

        if path.startswith("server/"):
            required.add("validate-server")

        if (
            path.startswith("frontend/")
            or path == "server/app.py"
        ):
            required.add("validate-dashboard")

        if path.startswith("navigation/"):
            required.add("validate-navigation")

        if path.startswith("raspberry/"):
            required.add("validate-raspberry")

    if changed_files:
        required.add("review-change")

    return required


def _report_marks_pass(message: str, label: str) -> bool:
    if not message:
        return False

    escaped = rf"[*_`~]*{re.escape(label)}[*_`~]*"
    separator = r"[ \t]*(?::|=|-)?[ \t]*"
    return bool(
        re.search(
            rf"(?im)(?:{escaped}{separator}PASS\b|PASS{separator}{escaped}\b)",
            message,
        )
    )


def _validation_passes(message: str) -> set[str]:
    return {
        validator
        for validator in (*VALIDATOR_BY_AREA.values(), "review-change")
        if _report_marks_pass(message, validator)
    }


def _transcript_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    transcript_path = _string_value(
        payload,
        "transcript_path",
        "transcriptPath",
    )
    if not transcript_path:
        return []

    path = Path(transcript_path)
    if not path.is_file():
        return []

    expected_turn = _turn_id(payload)
    events: list[dict[str, Any]] = []

    try:
        with path.open("r", encoding="utf-8", errors="replace") as file_obj:
            for line in file_obj:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not isinstance(event, dict):
                    continue

                event_payload = event.get("payload")
                if not isinstance(event_payload, dict):
                    continue

                metadata = event_payload.get(
                    "internal_chat_message_metadata_passthrough"
                )
                metadata_turn = (
                    metadata.get("turn_id")
                    if isinstance(metadata, dict)
                    else None
                )
                event_turn = event_payload.get("turn_id") or metadata_turn

                if event_turn == expected_turn:
                    events.append(event)
    except OSError:
        return []

    return events


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("payload")
    return value if isinstance(value, dict) else {}


def _successful_command_texts(
    transcript_events: list[dict[str, Any]],
) -> list[str]:
    commands: list[str] = []

    for event in transcript_events:
        event_payload = _event_payload(event)
        if event.get("type") != "event_msg":
            continue
        if event_payload.get("type") != "item_completed":
            continue

        item = event_payload.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") != "CommandExecution":
            continue
        if item.get("status") != "completed" or item.get("exit_code") != 0:
            continue

        command = item.get("command")
        if isinstance(command, list):
            commands.append(" ".join(str(part) for part in command))
        elif isinstance(command, str):
            commands.append(command)

        parsed_commands = item.get("parsed_cmd")
        if isinstance(parsed_commands, list):
            commands.extend(
                str(parsed.get("cmd"))
                for parsed in parsed_commands
                if isinstance(parsed, dict) and parsed.get("cmd")
            )

    return commands


def _dashboard_review_completed(
    transcript_events: list[dict[str, Any]],
) -> bool:
    dashboard_paths: set[str] = set()
    completed_paths: set[str] = set()

    for event in transcript_events:
        event_payload = _event_payload(event)

        if (
            event.get("type") == "response_item"
            and event_payload.get("type") == "function_call"
            and event_payload.get("namespace") == "collaboration"
            and event_payload.get("name") == "spawn_agent"
        ):
            arguments = event_payload.get("arguments")
            if not isinstance(arguments, str):
                continue
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError:
                continue
            if parsed.get("agent_type") != "dashboard_reviewer":
                continue
            task_name = str(parsed.get("task_name") or "").strip("/")
            if task_name:
                dashboard_paths.add(f"/root/{task_name}")

        if event.get("type") == "event_msg":
            item = event_payload.get("item")
            if not isinstance(item, dict):
                continue
            if (
                item.get("type") == "SubAgentActivity"
                and item.get("kind") == "completed"
                and item.get("agent_path")
            ):
                completed_paths.add(str(item["agent_path"]))

    return bool(dashboard_paths & completed_paths)


def _transcript_validation_attempts(
    transcript_events: list[dict[str, Any]],
) -> set[str]:
    attempts: set[str] = set()

    for command in _successful_command_texts(transcript_events):
        normalized = command.replace("\\", "/").lower()
        attempts.update(
            validator
            for validator, marker in VALIDATOR_COMMAND_MARKERS.items()
            if marker in normalized
        )

    if _dashboard_review_completed(transcript_events):
        attempts.add("validate-dashboard")

    return attempts


def _validation_attempts(
    payload: dict[str, Any],
    state: dict[str, Any],
    transcript_events: list[dict[str, Any]],
) -> set[str]:
    attempts: set[str] = set()

    for source in (state, payload):
        value = _first_value(
            source,
            "validation_attempts",
            "validationAttempts",
            "validators",
        )

        if isinstance(value, dict):
            attempts.update(
                str(name)
                for name, result in value.items()
                if result
            )
        elif isinstance(value, list):
            attempts.update(
                str(item)
                for item in value
                if str(item).strip()
            )

        tools = _first_value(source, "tools_used", "toolsUsed")
        if isinstance(tools, list):
            tool_text = " ".join(str(item).lower() for item in tools)
            attempts.update(
                validator
                for validator in (*VALIDATOR_BY_AREA.values(), "review-change")
                if validator.lower() in tool_text
            )

    attempts.update(_transcript_validation_attempts(transcript_events))

    return attempts


def _playwright_used(
    payload: dict[str, Any],
    state: dict[str, Any],
    transcript_events: list[dict[str, Any]],
) -> bool:
    for source in (state, payload):
        value = _first_value(
            source,
            "playwright_used",
            "playwrightUsed",
        )

        if isinstance(value, bool):
            return value

        if isinstance(value, (int, float)):
            return bool(value)

        tools = _first_value(
            source,
            "tools_used",
            "toolsUsed",
        )

        if isinstance(tools, list):
            lowered = " ".join(str(item).lower() for item in tools)

            if any(marker in lowered for marker in PLAYWRIGHT_MARKERS):
                return True

    message = _last_assistant_message(payload)
    if any(_report_marks_pass(message, marker) for marker in PLAYWRIGHT_MARKERS):
        return True

    for event in transcript_events:
        event_payload = _event_payload(event)
        if event.get("type") != "response_item":
            continue
        if event_payload.get("type") not in ("function_call", "custom_tool_call"):
            continue

        tool_text = " ".join(
            str(event_payload.get(key) or "")
            for key in ("namespace", "name", "input")
        ).lower()
        if any(marker in tool_text for marker in PLAYWRIGHT_MARKERS):
            return True

    return False


def _has_validation_report(message: str) -> bool:
    if not message:
        return False

    lowered = message.lower()

    return any(
        marker.lower() in lowered
        for marker in VALIDATION_REPORT_MARKERS
    )


def _decision_allow() -> dict[str, Any]:
    return {}


def _decision_block(reason: str) -> dict[str, Any]:
    return {
        "decision": "block",
        "reason": reason,
    }


def _evaluate(payload: dict[str, Any]) -> dict[str, Any]:
    # 사람이 직접 `python validate_on_stop.py`를 실행한 경우.
    # stdin payload가 없다고 hook failure로 처리하지 않는다.
    if not payload:
        return _decision_allow()

    state_path = _state_path(payload)
    state = _load_state(state_path)

    changed_files = _changed_files(payload, state)
    required = _required_validators(changed_files)

    if not required:
        return _decision_allow()

    message = _last_assistant_message(payload)
    transcript_events = _transcript_events(payload)
    attempted = _validation_attempts(payload, state, transcript_events)
    missing = sorted(required - attempted)

    # 실제 Stop payload의 transcript와 명시적으로 전파된 실행 상태에서 근거를 찾는다.
    # 어느 경로에서도 필수 validator를 확인할 수 없으면 fail-open하지 않는다.
    if missing:
        reason = (
            "변경 영역에 필요한 validator가 아직 실행되지 않았습니다: "
            + ", ".join(missing)
        )
        return _decision_block(reason)

    missing_pass_reports = sorted(required - _validation_passes(message))
    if missing_pass_reports:
        reason = (
            "필수 validator별 PASS 보고가 누락되었습니다: "
            + ", ".join(missing_pass_reports)
        )
        return _decision_block(reason)

    dashboard_required = "validate-dashboard" in required

    if (
        dashboard_required
        and "validate-dashboard" in attempted
        and not _playwright_used(payload, state, transcript_events)
    ):
        reason = (
            "dashboard 변경 검증에는 Playwright 사용 기록이 필요합니다."
        )
        return _decision_block(reason)

    if not _has_validation_report(message):
        reason = (
            "validation을 수행했지만 마지막 assistant message에 "
            "PASS / FAIL / WARN 검증 결과가 보고되지 않았습니다."
        )
        return _decision_block(reason)

    return _decision_allow()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    decision = _evaluate(_read_payload())
    print(
        json.dumps(
            decision,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
