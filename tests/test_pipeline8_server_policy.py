from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def test_pipeline8_server_disables_auxiliary_violence_output():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert 'getattr(frame_processor.action_analyzer, "restrict_to_target_actions", False)' in source
    assert "if restrict_to_target_actions" in source
    assert "None if restrict_to_target_actions else violence_results.get(oid)" in source
    assert "not restrict_to_target_actions" in source


def test_pipeline8_observation_issue_does_not_clear_display_state():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert "observation_issue = is_observation_issue(action)" in source
    assert "not observation_issue" in source
    assert "ACTION_DISPLAY_TTL_SEC" in source
