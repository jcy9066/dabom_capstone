import time

import numpy as np

from perception.models.action_policy import (
    OBSERVATION_INFERENCE_ERROR,
    RECALL_TARGET_ACTIONS,
    TemporalActionPolicy,
    is_observation_issue,
)
import perception.models.action_yolopose_stgcnpp as action_module
from perception.models.action_yolopose_stgcnpp import ActionRecognizer


def build_recall_analyzer():
    analyzer = ActionRecognizer.__new__(ActionRecognizer)
    analyzer.recall_mode = True
    analyzer.manages_action_hysteresis = True
    analyzer.restrict_to_target_actions = True
    analyzer.target_actions = RECALL_TARGET_ACTIONS
    analyzer.action_buffer = {}
    analyzer.single_person_actions = {
        idx: info for idx, info in RECALL_TARGET_ACTIONS.items() if idx in {41, 42}
    }
    analyzer.interaction_actions = {
        idx: info for idx, info in RECALL_TARGET_ACTIONS.items() if idx in {49, 50, 51}
    }
    analyzer.pair_action_buffer = {}
    analyzer.interaction_pair_distance_ratio = 1.5
    analyzer.min_history_frames = 12
    analyzer.use_native_history_length = True
    analyzer.interaction_infer_every_n = 2
    analyzer.interaction_max_pairs = 4
    analyzer.pose_gap_reset_sec = 1.5
    analyzer.last_valid_pose_at = {}
    analyzer._interaction_frame_counter = 0
    analyzer.temporal_policy = TemporalActionPolicy(
        window=5,
        suspicious_min_hits=1,
        danger_min_hits=1,
        normal_clear_hits=2,
        state_ttl_sec=1.0,
    )
    analyzer.pair_temporal_policy = TemporalActionPolicy(
        window=5,
        suspicious_min_hits=1,
        danger_min_hits=1,
        normal_clear_hits=2,
        state_ttl_sec=1.0,
    )
    return analyzer


def test_inference_failure_does_not_advance_normal_clear(monkeypatch):
    analyzer = build_recall_analyzer()
    now = time.monotonic()
    danger = {
        "label": "FALLING",
        "score": 0.8,
        "is_danger": True,
        "confidence_level": "danger",
    }
    analyzer.temporal_policy.update(7, danger, now=now)

    def fail_predict(*_args, **_kwargs):
        raise RuntimeError("synthetic inference failure")

    monkeypatch.setattr(analyzer, "_predict_scores", fail_predict)

    result = analyzer._classify_with_object(7, [], [], (480, 640, 3))

    assert is_observation_issue(result)
    assert result["observation_status"] == OBSERVATION_INFERENCE_ERROR
    assert analyzer.temporal_policy.current[7]["label"] == "FALLING"
    assert analyzer.temporal_policy.normal_streak[7] == 0


def test_unavailable_pose_does_not_advance_normal_clear():
    analyzer = build_recall_analyzer()
    now = time.monotonic()
    danger = {
        "label": "PUSHING",
        "score": 0.7,
        "is_danger": True,
        "confidence_level": "danger",
    }
    analyzer.temporal_policy.update(9, danger, now=now)

    issue = analyzer.observation_unavailable(9, "keypoints missing")

    assert is_observation_issue(issue)
    assert analyzer.temporal_policy.current[9]["label"] == "PUSHING"
    assert analyzer.temporal_policy.normal_streak[9] == 0


def test_expired_track_removes_pose_and_policy_state():
    analyzer = build_recall_analyzer()
    analyzer.action_buffer[3] = {"kpts": [1], "scores": [1]}
    analyzer.temporal_policy.update(3, None, now=0.0)

    expired = analyzer.expire_tracking_state(now=2.0)

    assert expired == [3]
    assert 3 not in analyzer.action_buffer
    assert 3 not in analyzer.temporal_policy.history
    assert 3 not in analyzer.temporal_policy.last_seen_at


def test_tracker_reset_clears_all_action_state():
    analyzer = build_recall_analyzer()
    analyzer.action_buffer[5] = {"kpts": [1], "scores": [1]}
    analyzer.temporal_policy.update(5, None, now=time.monotonic())

    analyzer.reset_tracking_state()

    assert analyzer.action_buffer == {}
    assert analyzer.temporal_policy.history == {}
    assert analyzer.temporal_policy.current == {}
    assert analyzer.temporal_policy.normal_streak == {}
    assert analyzer.temporal_policy.last_seen_at == {}


def test_interaction_pair_uses_two_person_skeleton_tensor(monkeypatch):
    analyzer = build_recall_analyzer()
    captured = {}

    def fake_predict(keypoints, scores, _shape):
        captured["keypoints_shape"] = keypoints.shape
        captured["scores_shape"] = scores.shape
        prediction = np.zeros(60, dtype=np.float32)
        prediction[49] = 0.8
        return prediction

    monkeypatch.setattr(analyzer, "_predict_scores_array", fake_predict)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    keypoints_a = np.ones((17, 2), dtype=np.float32)
    keypoints_b = np.ones((17, 2), dtype=np.float32) * 2
    scores = np.ones(17, dtype=np.float32)
    first = {
        "id": 10,
        "box": np.array([100, 100, 200, 300], dtype=np.float32),
        "center": (150.0, 200.0),
        "keypoints": keypoints_a,
        "keypoints_scores": scores,
    }
    second = {
        "id": 11,
        "box": np.array([180, 100, 280, 300], dtype=np.float32),
        "center": (230.0, 200.0),
        "keypoints": keypoints_b,
        "keypoints_scores": scores,
    }

    action = None
    for _ in range(analyzer.min_history_frames):
        action = analyzer._process_interaction_pair(frame, first, second)

    assert captured["keypoints_shape"] == (2, analyzer.min_history_frames, 17, 2)
    assert captured["scores_shape"] == (2, analyzer.min_history_frames, 17)
    assert action["label"] == "PUNCHING"


def test_interaction_pair_distance_gate_rejects_far_people():
    analyzer = build_recall_analyzer()
    first = {"id": 1, "box": [0, 0, 100, 200], "center": (50.0, 100.0)}
    second = {"id": 2, "box": [1000, 0, 1100, 200], "center": (1050.0, 100.0)}

    assert analyzer._is_interaction_pair(first, second) is False


def test_interaction_pair_warmup_does_not_repeat_last_frame(monkeypatch):
    analyzer = build_recall_analyzer()
    called = False

    def fake_predict(*_args, **_kwargs):
        nonlocal called
        called = True
        return np.zeros(60, dtype=np.float32)

    monkeypatch.setattr(analyzer, "_predict_scores_array", fake_predict)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    scores = np.ones(17, dtype=np.float32)
    first = {
        "id": 1,
        "box": [0, 0, 100, 200],
        "center": (50.0, 100.0),
        "keypoints": np.ones((17, 2), dtype=np.float32),
        "keypoints_scores": scores,
    }
    second = {
        "id": 2,
        "box": [80, 0, 180, 200],
        "center": (130.0, 100.0),
        "keypoints": np.ones((17, 2), dtype=np.float32) * 2,
        "keypoints_scores": scores,
    }

    result = analyzer._process_interaction_pair(frame, first, second)

    assert is_observation_issue(result)
    assert called is False
    assert len(analyzer.pair_action_buffer[(1, 2)]["kpts"]) == 1


def test_pair_selection_keeps_top_candidates_even_when_people_overlap():
    analyzer = build_recall_analyzer()
    analyzer.interaction_max_pairs = 3
    objs = [
        {"id": 1, "box": [0, 0, 100, 200], "center": (50.0, 100.0)},
        {"id": 2, "box": [60, 0, 160, 200], "center": (110.0, 100.0)},
        {"id": 3, "box": [140, 0, 240, 200], "center": (190.0, 100.0)},
        {"id": 4, "box": [200, 0, 300, 200], "center": (250.0, 100.0)},
    ]

    pairs = analyzer._select_interaction_pairs(objs)
    pair_ids = [tuple(sorted((first["id"], second["id"]))) for first, second in pairs]

    assert pair_ids == [(1, 2), (3, 4), (2, 3)]
    assert any(2 in pair for pair in pair_ids[1:])


def test_pair_selection_keeps_existing_pair_bias_without_excluding_others():
    analyzer = build_recall_analyzer()
    analyzer.interaction_max_pairs = 2
    analyzer.pair_action_buffer[(2, 3)] = {"kpts": [1], "scores": [1]}
    objs = [
        {"id": 1, "box": [0, 0, 100, 200], "center": (50.0, 100.0)},
        {"id": 2, "box": [70, 0, 170, 200], "center": (120.0, 100.0)},
        {"id": 3, "box": [145, 0, 245, 200], "center": (195.0, 100.0)},
    ]

    pairs = analyzer._select_interaction_pairs(objs)
    pair_ids = [tuple(sorted((first["id"], second["id"]))) for first, second in pairs]

    assert pair_ids[0] == (2, 3)
    assert (1, 2) in pair_ids


def test_single_person_interaction_is_suspicious_fallback(monkeypatch):
    analyzer = build_recall_analyzer()

    def fake_predict(*_args, **_kwargs):
        prediction = np.zeros(60, dtype=np.float32)
        prediction[49] = 0.9
        return prediction

    monkeypatch.setattr(analyzer, "_predict_scores", fake_predict)
    action = analyzer._classify_with_object(
        5,
        [np.ones((17, 2), dtype=np.float32)] * analyzer.min_history_frames,
        [np.ones(17, dtype=np.float32)] * analyzer.min_history_frames,
        (480, 640, 3),
    )

    assert action["label"] == "PUNCHING"
    assert action["is_danger"] is False
    assert action["confidence_level"] == "suspicious"
    assert action["source"] == "single_fallback"


def test_interaction_pose_gap_is_reported_as_unavailable():
    analyzer = build_recall_analyzer()
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    first = {
        "id": 10,
        "box": [0, 0, 100, 200],
        "center": (50.0, 100.0),
        "keypoints": None,
        "keypoints_scores": None,
    }
    second = {
        "id": 11,
        "box": [80, 0, 180, 200],
        "center": (130.0, 100.0),
        "keypoints": np.ones((17, 2), dtype=np.float32),
        "keypoints_scores": np.ones(17, dtype=np.float32),
    }

    result = analyzer._process_interaction_pair(frame, first, second)

    assert is_observation_issue(result)
    assert result["observation_status"] == "unavailable"


def test_cached_pair_result_is_marked_stale(monkeypatch):
    analyzer = build_recall_analyzer()
    analyzer.interaction_infer_every_n = 2
    pair_id = (1, 2)
    cached = {
        "label": "PUNCHING",
        "score": 0.8,
        "is_danger": True,
        "confidence_level": "danger",
    }
    analyzer.pair_temporal_policy.current[pair_id] = cached
    analyzer.pair_temporal_policy.mark_observed(pair_id)

    keypoints = np.ones((17, 2), dtype=np.float32)
    scores = np.ones(17, dtype=np.float32)
    objs = [
        {
            "id": 1,
            "cls": 0,
            "box": [0, 0, 100, 200],
            "center": (50.0, 100.0),
            "keypoints": keypoints,
            "keypoints_scores": scores,
        },
        {
            "id": 2,
            "cls": 0,
            "box": [80, 0, 180, 200],
            "center": (130.0, 100.0),
            "keypoints": keypoints,
            "keypoints_scores": scores,
        },
    ]

    monkeypatch.setattr(
        action_module,
        "process_keypoint_many",
        lambda _analyzer, _frame, people, total_frames: {
            obj["id"]: (obj["keypoints"], None) for obj in people
        },
    )

    results = analyzer.process_many(
        np.zeros((480, 640, 3), dtype=np.uint8),
        objs,
    )

    assert results[1][1]["observation_stale"] is True
    assert results[2][1]["observation_stale"] is True
    assert results[1][1]["source"] == "pair"


def test_single_person_inference_uses_real_history_length(monkeypatch):
    analyzer = build_recall_analyzer()
    captured_lengths = []

    def fake_predict(kpts, _scores, _shape):
        captured_lengths.append(len(kpts))
        prediction = np.zeros(60, dtype=np.float32)
        prediction[42] = 0.3
        return prediction

    monkeypatch.setattr(analyzer, "_predict_scores", fake_predict)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    obj = {
        "id": 31,
        "cls": 0,
        "box": [100, 100, 200, 300],
        "center": (150.0, 200.0),
        "keypoints": np.ones((17, 2), dtype=np.float32),
        "keypoints_scores": np.ones(17, dtype=np.float32),
    }

    for _ in range(analyzer.min_history_frames):
        analyzer.process_many(frame, [obj])

    assert captured_lengths == [analyzer.min_history_frames]
    assert len(analyzer.action_buffer[31]["kpts"]) == analyzer.min_history_frames


def test_confirmed_dangerous_pair_overrides_stronger_single_action(monkeypatch):
    analyzer = build_recall_analyzer()
    analyzer.interaction_infer_every_n = 2
    analyzer._interaction_frame_counter = 1

    keypoints = np.ones((17, 2), dtype=np.float32)
    scores = np.ones(17, dtype=np.float32)
    objs = [
        {
            "id": 1,
            "cls": 0,
            "box": [0, 0, 100, 200],
            "center": (50.0, 100.0),
            "keypoints": keypoints,
            "keypoints_scores": scores,
        },
        {
            "id": 2,
            "cls": 0,
            "box": [80, 0, 180, 200],
            "center": (130.0, 100.0),
            "keypoints": keypoints,
            "keypoints_scores": scores,
        },
    ]
    single = {
        "label": "FALLING",
        "score": 0.95,
        "is_danger": True,
        "confidence_level": "danger",
    }
    pair = {
        "label": "PUNCHING",
        "score": 0.70,
        "is_danger": True,
        "confidence_level": "danger",
    }

    monkeypatch.setattr(
        action_module,
        "process_keypoint_many",
        lambda _analyzer, _frame, people, total_frames: {
            obj["id"]: (obj["keypoints"], dict(single)) for obj in people
        },
    )
    monkeypatch.setattr(
        analyzer,
        "_process_interaction_pair",
        lambda _frame, _first, _second: dict(pair),
    )

    results = analyzer.process_many(
        np.zeros((480, 640, 3), dtype=np.uint8),
        objs,
    )

    assert results[1][1]["label"] == "PUNCHING"
    assert results[2][1]["label"] == "PUNCHING"
    assert results[1][1]["source"] == "pair"
    assert results[2][1]["interaction_pair_ids"] == [1, 2]


def test_pair_history_updates_even_when_pair_inference_is_skipped(monkeypatch):
    analyzer = build_recall_analyzer()
    analyzer.interaction_infer_every_n = 2
    analyzer._interaction_frame_counter = 0

    keypoints = np.ones((17, 2), dtype=np.float32)
    scores = np.ones(17, dtype=np.float32)
    objs = [
        {
            "id": 1,
            "cls": 0,
            "box": [0, 0, 100, 200],
            "center": (50.0, 100.0),
            "keypoints": keypoints,
            "keypoints_scores": scores,
        },
        {
            "id": 2,
            "cls": 0,
            "box": [80, 0, 180, 200],
            "center": (130.0, 100.0),
            "keypoints": keypoints * 2,
            "keypoints_scores": scores,
        },
    ]

    monkeypatch.setattr(
        action_module,
        "process_keypoint_many",
        lambda _analyzer, _frame, people, total_frames: {
            obj["id"]: (obj["keypoints"], None) for obj in people
        },
    )

    analyzer.process_many(
        np.zeros((480, 640, 3), dtype=np.uint8),
        objs,
    )

    assert len(analyzer.pair_action_buffer[(1, 2)]["kpts"]) == 1
    assert analyzer._interaction_frame_counter == 1


def test_geometrically_ineligible_visible_pair_state_is_cleared():
    analyzer = build_recall_analyzer()
    stale_pair = (1, 3)
    analyzer.pair_action_buffer[stale_pair] = {"kpts": [1], "scores": [1]}
    analyzer.pair_temporal_policy.update(
        stale_pair,
        {
            "label": "PUSHING",
            "score": 0.7,
            "is_danger": True,
            "confidence_level": "danger",
        },
        now=time.monotonic(),
    )
    objs = [
        {"id": 1, "box": [0, 0, 100, 200], "center": (50.0, 100.0)},
        {"id": 2, "box": [60, 0, 160, 200], "center": (110.0, 100.0)},
        {"id": 3, "box": [500, 0, 600, 200], "center": (550.0, 100.0)},
    ]
    selected = analyzer._select_interaction_pairs(objs)

    analyzer._clear_unselected_visible_pair_state(objs, selected)

    assert stale_pair not in analyzer.pair_action_buffer
    assert stale_pair not in analyzer.pair_temporal_policy.history
    assert stale_pair not in analyzer.pair_temporal_policy.current
    assert stale_pair not in analyzer.pair_temporal_policy.last_seen_at


def test_pose_gap_resets_single_and_pair_history():
    analyzer = build_recall_analyzer()
    analyzer.pose_gap_reset_sec = 1.0
    analyzer.note_valid_pose(7, now=10.0)
    analyzer.action_buffer[7] = {"kpts": [1], "scores": [1]}
    analyzer.temporal_policy.update(
        7,
        {
            "label": "FALLING",
            "score": 0.8,
            "is_danger": True,
            "confidence_level": "danger",
        },
        now=10.0,
    )
    pair_id = (7, 8)
    analyzer.pair_action_buffer[pair_id] = {"kpts": [1], "scores": [1]}
    analyzer.pair_temporal_policy.update(
        pair_id,
        {
            "label": "PUNCHING",
            "score": 0.8,
            "is_danger": True,
            "confidence_level": "danger",
        },
        now=10.0,
    )

    analyzer.note_valid_pose(7, now=11.1)

    assert 7 not in analyzer.action_buffer
    assert 7 not in analyzer.temporal_policy.history
    assert pair_id not in analyzer.pair_action_buffer
    assert pair_id not in analyzer.pair_temporal_policy.history
    assert analyzer.last_valid_pose_at[7] == 11.1


def test_competing_dangerous_pairs_keep_higher_score_for_shared_person():
    analyzer = build_recall_analyzer()
    weaker = {
        "label": "PUSHING",
        "score": 0.61,
        "is_danger": True,
        "confidence_level": "danger",
        "source": "pair",
    }
    stronger = {
        "label": "PUNCHING",
        "score": 0.82,
        "is_danger": True,
        "confidence_level": "danger",
        "source": "pair",
    }

    assert analyzer._should_replace_with_pair(weaker, stronger) is True
    assert analyzer._should_replace_with_pair(stronger, weaker) is False
