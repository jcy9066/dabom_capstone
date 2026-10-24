from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / ".codex" / "hooks" / "validate_on_stop.py"
HOOK_SPEC = importlib.util.spec_from_file_location("validate_on_stop", HOOK_PATH)
assert HOOK_SPEC and HOOK_SPEC.loader
HOOK = importlib.util.module_from_spec(HOOK_SPEC)
HOOK_SPEC.loader.exec_module(HOOK)

TURN_ID = "turn-hook-test"


def _command_event(command: list[str], *, exit_code: int = 0) -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "turn_id": TURN_ID,
            "item": {
                "type": "CommandExecution",
                "status": "completed",
                "exit_code": exit_code,
                "command": command,
            },
        },
    }


def _dashboard_events() -> list[dict]:
    return [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "namespace": "collaboration",
                "name": "spawn_agent",
                "arguments": json.dumps(
                    {
                        "agent_type": "dashboard_reviewer",
                        "task_name": "dashboard_hook_test",
                    }
                ),
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": TURN_ID
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": TURN_ID,
                "item": {
                    "type": "SubAgentActivity",
                    "kind": "completed",
                    "agent_path": "/root/dashboard_hook_test",
                },
            },
        },
    ]


def _write_transcript(tmp_path: Path, events: list[dict]) -> Path:
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    return path


def _payload(
    transcript: Path,
    *,
    changed_files: list[str],
    report: str,
) -> dict:
    return {
        "hook_event_name": "Stop",
        "session_id": "session-hook-test",
        "turn_id": TURN_ID,
        "cwd": str(ROOT),
        "transcript_path": str(transcript),
        "last_assistant_message": report,
        "changed_files": changed_files,
    }


def test_review_change_pass_is_recognized_from_windows_command(tmp_path: Path) -> None:
    transcript = _write_transcript(
        tmp_path,
        [
            _command_event(
                [
                    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                    "-Command",
                    (
                        r"python -X utf8 C:\repo\.agents\skills\review-change\scripts"
                        r"\review_change.py"
                    ),
                ]
            )
        ],
    )
    payload = _payload(
        transcript,
        changed_files=[".codex/hooks/validate_on_stop.py"],
        report="`review-change`: PASS",
    )

    assert HOOK._evaluate(payload) == {}


def test_missing_required_validator_still_blocks(tmp_path: Path) -> None:
    transcript = _write_transcript(
        tmp_path,
        [
            _command_event(
                ["python", ".agents/skills/review-change/scripts/review_change.py"]
            )
        ],
    )
    payload = _payload(
        transcript,
        changed_files=["server/database.py"],
        report="review-change: PASS\nvalidate-server: PASS",
    )

    decision = HOOK._evaluate(payload)

    assert decision["decision"] == "block"
    assert "validate-server" in decision["reason"]
    assert "실행" in decision["reason"]


def test_each_required_validator_needs_its_own_pass_report(tmp_path: Path) -> None:
    transcript = _write_transcript(
        tmp_path,
        [
            _command_event(
                ["python", ".agents/skills/validate-server/scripts/validate_server.py"]
            ),
            _command_event(
                ["python", ".agents/skills/review-change/scripts/review_change.py"]
            ),
        ],
    )
    payload = _payload(
        transcript,
        changed_files=["server/database.py"],
        report="review-change: PASS\nvalidation complete",
    )

    decision = HOOK._evaluate(payload)

    assert decision["decision"] == "block"
    assert "validate-server" in decision["reason"]
    assert "PASS" in decision["reason"]


def test_dashboard_still_requires_playwright_evidence(tmp_path: Path) -> None:
    transcript = _write_transcript(
        tmp_path,
        [
            *_dashboard_events(),
            _command_event(
                ["python", ".agents/skills/review-change/scripts/review_change.py"]
            ),
        ],
    )
    payload = _payload(
        transcript,
        changed_files=["frontend/templates/index.html"],
        report="validate-dashboard: PASS\nreview-change: PASS",
    )

    decision = HOOK._evaluate(payload)

    assert decision["decision"] == "block"
    assert "Playwright" in decision["reason"]

    payload["last_assistant_message"] += "\nplaywright: PASS"
    assert HOOK._evaluate(payload) == {}


def test_failed_validator_command_does_not_count_as_execution(tmp_path: Path) -> None:
    transcript = _write_transcript(
        tmp_path,
        [
            _command_event(
                ["python", ".agents/skills/review-change/scripts/review_change.py"],
                exit_code=1,
            )
        ],
    )
    payload = _payload(
        transcript,
        changed_files=["tests/test_validate_on_stop.py"],
        report="review-change: PASS",
    )

    assert HOOK._evaluate(payload)["decision"] == "block"


def test_manual_empty_invocation_is_allowed_and_feedback_is_utf8() -> None:
    assert HOOK._evaluate({}) == {}

    payload = {
        "hook_event_name": "Stop",
        "session_id": "encoding-test",
        "turn_id": TURN_ID,
        "cwd": str(ROOT),
        "changed_files": ["tests/test_validate_on_stop.py"],
        "last_assistant_message": "PASS",
    }
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "cp949"
    completed = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )

    assert completed.returncode == 0
    decoded = completed.stdout.decode("utf-8")
    assert "실행" in decoded

    manual = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=b"",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert manual.returncode == 0
    assert json.loads(manual.stdout.decode("utf-8")) == {}


def test_windows_hook_command_explicitly_enables_utf8() -> None:
    config = json.loads((ROOT / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    command = config["hooks"]["Stop"][0]["hooks"][0]["commandWindows"]

    assert command.startswith("python -X utf8 ")
