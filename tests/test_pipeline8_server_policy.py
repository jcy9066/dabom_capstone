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
    assert 'current_action.get("source") != "pair"' in source


def test_stale_pair_evidence_does_not_refresh_display_ttl():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert 'selected_action.get("observation_stale")' in source
    assert "do not refresh stale evidence" in source


def test_stale_pair_evidence_cannot_emit_new_alerts():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert 'detection["inference_status"] = "stale"' in source
    assert 'source.get("observation_stale")' in source
    assert "observation_stale" in source


def test_pair_overlay_uses_shared_source_metadata_and_one_badge():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert "seen.add(pair_key)" in source
    assert "overlays.append((pair_key, first, second, detection))" in source
    assert 'label = source.get("label") or "INTERACTION"' in source
    assert 'score = source.get("score")' in source
    assert "draw_interaction_badge(" in source


def test_pair_overlay_highlights_both_participants():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert "draw_corner_brackets(frame, first_box, color)" in source
    assert "draw_corner_brackets(frame, second_box, color)" in source
    assert 'draw_skeleton_points(frame, first.get("skeleton"), color)' in source
    assert 'draw_skeleton_points(frame, second.get("skeleton"), color)' in source


def test_pair_overlay_group_does_not_include_unrelated_third_person():
    import server.app as server_app

    detections = [
        {
            "id": 1,
            "box": [10, 10, 50, 100],
            "label": "PUNCHING",
            "score": 0.8,
            "danger": True,
            "action_source": "pair",
            "interaction_pair_ids": [1, 2],
        },
        {
            "id": 2,
            "box": [60, 10, 100, 100],
            "label": "",
            "score": None,
            "danger": False,
            "action_source": None,
            "interaction_pair_ids": None,
        },
        {
            "id": 3,
            "box": [180, 10, 220, 100],
            "label": "",
            "score": None,
            "danger": False,
            "action_source": None,
            "interaction_pair_ids": None,
        },
    ]

    overlays = server_app.interaction_pair_overlays(detections)

    assert len(overlays) == 1
    pair_key, first, second, source = overlays[0]
    assert pair_key == (1, 2)
    assert {first["id"], second["id"]} == {1, 2}
    assert source["id"] == 1
    assert 3 not in pair_key


def test_final_interaction_overlay_is_non_overlapping_and_prefers_danger():
    import server.app as server_app

    detections = [
        {
            "id": 1,
            "box": [10, 10, 50, 100],
            "label": "PUSHING",
            "score": 0.60,
            "danger": False,
            "action_source": "pair",
            "interaction_pair_ids": [1, 2],
        },
        {
            "id": 2,
            "box": [60, 10, 100, 100],
            "label": "PUSHING",
            "score": 0.60,
            "danger": False,
            "action_source": "pair",
            "interaction_pair_ids": [1, 2],
        },
        {
            "id": 3,
            "box": [110, 10, 150, 100],
            "label": "PUNCHING",
            "score": 0.55,
            "danger": True,
            "action_source": "pair",
            "interaction_pair_ids": [1, 3],
        },
    ]

    overlays = server_app.interaction_pair_overlays(detections)

    assert len(overlays) == 1
    pair_key, _first, _second, source = overlays[0]
    assert pair_key == (1, 3)
    assert source["danger"] is True


def test_pair_events_use_pair_scoped_cooldown_key():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert 'cooldown_key=f"pair:{pair_key[0]}:{pair_key[1]}"' in source
