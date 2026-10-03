import time

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
    analyzer.temporal_policy = TemporalActionPolicy(
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
