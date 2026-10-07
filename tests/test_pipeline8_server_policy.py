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


def test_pipeline8_pair_actions_are_marked_as_interactions():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert '"interaction_pair_ids": None' in source
    assert 'current_action.get("interaction_pair_ids")' in source
    assert "draw_interaction_overlays" in source
    assert 'detection.get("action_source") == "pair"' in source
    assert "should_emit_event" in source


def test_stale_pair_evidence_does_not_refresh_display_ttl():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert 'selected_action.get("observation_stale")' in source
    assert "do not refresh stale evidence" in source
