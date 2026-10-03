from perception.models.action_policy import (
    RECALL_TARGET_ACTIONS,
    TemporalActionPolicy,
    classify_target_scores,
)


def test_recall_targets_are_exactly_the_five_selected_ntu60_classes():
    assert RECALL_TARGET_ACTIONS == {
        41: {"name": "STAGGERING", "danger": True},
        42: {"name": "FALLING", "danger": True},
        49: {"name": "PUNCHING", "danger": True},
        50: {"name": "KICKING", "danger": True},
        51: {"name": "PUSHING", "danger": True},
    }


def test_target_score_can_trigger_even_when_non_target_is_global_argmax():
    scores = [0.0] * 60
    scores[0] = 0.90
    scores[42] = 0.30

    action = classify_target_scores(scores, RECALL_TARGET_ACTIONS)

    assert action["label"] == "FALLING"
    assert action["confidence_level"] == "suspicious"
    assert action["is_danger"] is False


def test_temporal_policy_promotes_repeated_danger_hits():
    policy = TemporalActionPolicy(
        window=5,
        suspicious_min_hits=2,
        danger_min_hits=3,
        normal_clear_hits=2,
    )
    danger_candidate = {
        "label": "FALLING",
        "score": 0.60,
        "is_danger": True,
        "confidence_level": "danger",
    }

    assert policy.update(7, danger_candidate) is None
    assert policy.update(7, danger_candidate)["confidence_level"] == "suspicious"
    assert policy.update(7, danger_candidate)["confidence_level"] == "danger"


def test_temporal_policy_requires_consecutive_normal_hits_to_clear():
    policy = TemporalActionPolicy(
        window=5,
        suspicious_min_hits=1,
        danger_min_hits=1,
        normal_clear_hits=2,
    )
    danger_candidate = {
        "label": "PUSHING",
        "score": 0.70,
        "is_danger": True,
        "confidence_level": "danger",
    }

    assert policy.update(11, danger_candidate)["confidence_level"] == "danger"
    assert policy.update(11, None)["confidence_level"] == "danger"
    assert policy.update(11, None) is None


def test_observation_issue_is_not_a_normal_action():
    from perception.models.action_policy import (
        OBSERVATION_INFERENCE_ERROR,
        is_observation_issue,
        observation_issue,
    )

    issue = observation_issue(OBSERVATION_INFERENCE_ERROR, "boom")
    assert is_observation_issue(issue)
    assert issue["observation_status"] == OBSERVATION_INFERENCE_ERROR


def test_stale_tracking_state_is_fully_expired():
    policy = TemporalActionPolicy(
        window=5,
        suspicious_min_hits=1,
        danger_min_hits=1,
        normal_clear_hits=2,
        state_ttl_sec=1.0,
    )

    for object_id in range(1000):
        policy.update(object_id, None, now=0.0)

    assert len(policy.history) == 1000
    assert len(policy.normal_streak) == 1000
    assert len(policy.last_seen_at) == 1000

    expired = policy.expire_stale(now=2.0)

    assert len(expired) == 1000
    assert policy.history == {}
    assert policy.current == {}
    assert policy.normal_streak == {}
    assert policy.last_seen_at == {}
