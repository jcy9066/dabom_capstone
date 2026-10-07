import time

import numpy as np

from perception.models.action_policy import (
    OBSERVATION_INFERENCE_ERROR,
    RECALL_TARGET_ACTIONS,
    TemporalActionPolicy,
    is_observation_issue,
)
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


def test_pair_selection_is_nearest_and_non_overlapping():
    analyzer = build_recall_analyzer()
    objs = [
        {"id": 1, "box": [0, 0, 100, 200], "center": (50.0, 100.0)},
        {"id": 2, "box": [60, 0, 160, 200], "center": (110.0, 100.0)},
        {"id": 3, "box": [140, 0, 240, 200], "center": (190.0, 100.0)},
        {"id": 4, "box": [200, 0, 300, 200], "center": (250.0, 100.0)},
    ]

    pairs = analyzer._select_interaction_pairs(objs)
    pair_ids = {tuple(sorted((first["id"], second["id"]))) for first, second in pairs}

    assert pair_ids == {(1, 2), (3, 4)}


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
