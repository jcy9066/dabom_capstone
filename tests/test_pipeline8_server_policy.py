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

    assert "candidate_by_pair" in source
    assert "_maximum_weight_pair_matching" in source
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


def test_pair_events_use_incident_scoped_cooldown_key():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert "interaction_incident_key(" in source
    assert "cooldown_key=interaction_incident_key(" in source


def test_maximum_weight_matching_beats_greedy_pair_choice():
    import server.app as server_app

    detections = [
        {"id": 1, "box": [0, 0, 40, 100]},
        {"id": 2, "box": [50, 0, 90, 100]},
        {"id": 3, "box": [100, 0, 140, 100]},
        {"id": 4, "box": [150, 0, 190, 100]},
    ]
    pair_actions = {
        (1, 2): {
            "label": "PUNCHING",
            "score": 0.90,
            "is_danger": True,
            "source": "pair",
        },
        (1, 3): {
            "label": "PUNCHING",
            "score": 0.80,
            "is_danger": True,
            "source": "pair",
        },
        (2, 4): {
            "label": "PUNCHING",
            "score": 0.80,
            "is_danger": True,
            "source": "pair",
        },
    }

    overlays = server_app.interaction_pair_overlays(detections, pair_actions)
    pair_keys = {item[0] for item in overlays}

    assert pair_keys == {(1, 3), (2, 4)}


def test_pair_actions_serialize_without_person_result_compression():
    import server.app as server_app

    pair_actions = {
        (1, 2): {
            "label": "PUSHING",
            "score": 0.61,
            "is_danger": True,
            "source": "pair",
        },
        (1, 3): {
            "label": "PUNCHING",
            "score": 0.82,
            "is_danger": True,
            "source": "pair",
        },
    }

    serialized = server_app.serialize_pair_actions(pair_actions)

    assert len(serialized) == 2
    assert {tuple(item["pair_ids"]) for item in serialized} == {(1, 2), (1, 3)}


def test_interaction_incident_survives_track_id_switch():
    import server.app as server_app

    original_next_id = server_app.interaction_incident_state["next_id"]
    original_incidents = dict(server_app.interaction_incident_state["incidents"])
    try:
        server_app.interaction_incident_state["incidents"].clear()
        first_a = {"box": [10, 10, 60, 120]}
        first_b = {"box": [70, 10, 120, 120]}
        second_a = {"box": [13, 12, 63, 122]}
        second_b = {"box": [73, 12, 123, 122]}

        first_key = server_app.interaction_incident_key(
            (11, 12),
            first_a,
            first_b,
            "PUNCHING",
            frame_token=1,
            now=100.0,
        )
        second_key = server_app.interaction_incident_key(
            (21, 22),
            second_a,
            second_b,
            "PUSHING",
            frame_token=2,
            now=100.2,
        )

        assert first_key == second_key
    finally:
        server_app.interaction_incident_state["next_id"] = original_next_id
        server_app.interaction_incident_state["incidents"].clear()
        server_app.interaction_incident_state["incidents"].update(original_incidents)


def test_distinct_same_frame_interactions_get_distinct_incident_ids():
    import server.app as server_app

    original_next_id = server_app.interaction_incident_state["next_id"]
    original_incidents = dict(server_app.interaction_incident_state["incidents"])
    try:
        server_app.interaction_incident_state["incidents"].clear()
        first_key = server_app.interaction_incident_key(
            (1, 2),
            {"box": [10, 10, 50, 100]},
            {"box": [55, 10, 95, 100]},
            "PUNCHING",
            frame_token=77,
            now=200.0,
        )
        second_key = server_app.interaction_incident_key(
            (3, 4),
            {"box": [180, 10, 220, 100]},
            {"box": [225, 10, 265, 100]},
            "PUNCHING",
            frame_token=77,
            now=200.0,
        )

        assert first_key != second_key
    finally:
        server_app.interaction_incident_state["next_id"] = original_next_id
        server_app.interaction_incident_state["incidents"].clear()
        server_app.interaction_incident_state["incidents"].update(original_incidents)


def test_empty_scene_clears_preserved_pair_actions():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert 'latest_pairs = getattr(analyzer, "latest_pair_actions", None)' in source
    assert "latest_pairs.clear()" in source


def test_stale_pair_display_expires_after_ttl(monkeypatch):
    import server.app as server_app

    monkeypatch.setattr(server_app, "ACTION_DISPLAY_TTL_SEC", 1.0)
    buffer = {}
    fresh = {
        (1, 2): {
            "label": "PUNCHING",
            "score": 0.8,
            "is_danger": True,
            "source": "pair",
            "observation_stale": False,
        }
    }
    stale = {
        (1, 2): {
            "label": "PUNCHING",
            "score": 0.8,
            "is_danger": True,
            "source": "pair",
            "observation_stale": True,
        }
    }

    first = server_app.resolve_pair_actions_for_display(fresh, buffer, now=10.0)
    within_ttl = server_app.resolve_pair_actions_for_display(
        stale,
        buffer,
        now=10.5,
    )
    expired = server_app.resolve_pair_actions_for_display(
        stale,
        buffer,
        now=11.1,
    )

    assert (1, 2) in first
    assert within_ttl[(1, 2)]["observation_stale"] is True
    assert expired == {}
    assert buffer == {}


def test_local_frame_processor_uses_maximum_weight_pair_matching():
    import server.app as server_app

    pair_actions = {
        (1, 2): {
            "label": "PUNCHING",
            "score": 0.90,
            "is_danger": True,
        },
        (1, 3): {
            "label": "PUNCHING",
            "score": 0.80,
            "is_danger": True,
        },
        (2, 4): {
            "label": "PUNCHING",
            "score": 0.80,
            "is_danger": True,
        },
    }

    selected = server_app.FrameProcessor._select_non_overlapping_pairs(
        pair_actions
    )

    assert {pair_key for pair_key, _source in selected} == {(1, 3), (2, 4)}


def test_pair_results_are_cleared_when_model_is_disabled():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert 'latest_result["pair_actions"] = []' in source
