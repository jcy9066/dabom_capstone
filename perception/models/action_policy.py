import time
from collections import deque

try:
    from perception.env_config import env_float, env_int
except ModuleNotFoundError:  # Direct perception script execution.
    from env_config import env_float, env_int

SUSPICIOUS_ACTION_THRESHOLD = env_float(
    "ACTION_SUSPICIOUS_THRESHOLD", minimum=0.0, maximum=1.0
)
DANGER_ACTION_THRESHOLD = env_float(
    "ACTION_DANGER_THRESHOLD", minimum=0.0, maximum=1.0
)

RECALL_TARGET_ACTIONS = {
    41: {"name": "STAGGERING", "danger": True},
    42: {"name": "FALLING", "danger": True},
    49: {"name": "PUNCHING", "danger": True},
    50: {"name": "KICKING", "danger": True},
    51: {"name": "PUSHING", "danger": True},
}

OBSERVATION_UNAVAILABLE = "unavailable"
OBSERVATION_INFERENCE_ERROR = "inference_error"


def observation_issue(status, message):
    return {
        "observation_status": status,
        "error": str(message),
    }


def is_observation_issue(action):
    return isinstance(action, dict) and action.get("observation_status") in {
        OBSERVATION_UNAVAILABLE,
        OBSERVATION_INFERENCE_ERROR,
    }


def classify_target_action(action_idx, score, target_actions):
    action_info = target_actions.get(action_idx)
    action = None

    if action_info is not None:
        is_configured_danger = bool(action_info.get("danger"))
        if is_configured_danger and score >= DANGER_ACTION_THRESHOLD:
            action = {
                "label": action_info["name"],
                "score": float(score),
                "is_danger": True,
                "confidence_level": "danger",
            }
        elif score >= SUSPICIOUS_ACTION_THRESHOLD:
            action = {
                "label": action_info["name"],
                "score": float(score),
                "is_danger": False,
                "confidence_level": "suspicious",
            }

    return action


def classify_target_scores(pred_scores, target_actions):
    """Select the strongest configured target without requiring global argmax."""
    best_idx = None
    best_score = float("-inf")

    for action_idx in target_actions:
        if action_idx < 0 or action_idx >= len(pred_scores):
            continue
        raw_score = pred_scores[action_idx]
        score = float(raw_score.item() if hasattr(raw_score, "item") else raw_score)
        if score > best_score:
            best_idx = action_idx
            best_score = score

    if best_idx is None:
        return None
    return classify_target_action(best_idx, best_score, target_actions)


class TemporalActionPolicy:
    """Recall-oriented voting and normal-state hysteresis for tracked people."""

    def __init__(
        self,
        *,
        window,
        suspicious_min_hits,
        danger_min_hits,
        normal_clear_hits,
        state_ttl_sec=10.0,
    ):
        self.window = int(window)
        self.suspicious_min_hits = int(suspicious_min_hits)
        self.danger_min_hits = int(danger_min_hits)
        self.normal_clear_hits = int(normal_clear_hits)
        self.state_ttl_sec = float(state_ttl_sec)
        if self.window < 1:
            raise ValueError("ACTION_TEMPORAL_WINDOW must be >= 1")
        if not 1 <= self.suspicious_min_hits <= self.window:
            raise ValueError("ACTION_SUSPICIOUS_MIN_HITS must be within temporal window")
        if not 1 <= self.danger_min_hits <= self.window:
            raise ValueError("ACTION_DANGER_MIN_HITS must be within temporal window")
        if self.normal_clear_hits < 1:
            raise ValueError("ACTION_NORMAL_CLEAR_HITS must be >= 1")
        if self.state_ttl_sec <= 0:
            raise ValueError("ACTION_TRACK_STATE_TTL_SEC must be > 0")

        self.history = {}
        self.current = {}
        self.normal_streak = {}
        self.last_seen_at = {}

    @classmethod
    def from_env(cls):
        return cls(
            window=env_int("ACTION_TEMPORAL_WINDOW", minimum=1),
            suspicious_min_hits=env_int("ACTION_SUSPICIOUS_MIN_HITS", minimum=1),
            danger_min_hits=env_int("ACTION_DANGER_MIN_HITS", minimum=1),
            normal_clear_hits=env_int("ACTION_NORMAL_CLEAR_HITS", minimum=1),
            state_ttl_sec=env_float(
                "ACTION_TRACK_STATE_TTL_SEC",
                default=10.0,
                minimum=0.1,
            ),
        )

    def clear(self, object_id):
        self.history.pop(object_id, None)
        self.current.pop(object_id, None)
        self.normal_streak.pop(object_id, None)
        self.last_seen_at.pop(object_id, None)

    def reset(self):
        self.history.clear()
        self.current.clear()
        self.normal_streak.clear()
        self.last_seen_at.clear()

    def expire_stale(self, now=None):
        now = time.monotonic() if now is None else float(now)
        expired = [
            object_id
            for object_id, last_seen in self.last_seen_at.items()
            if now - last_seen > self.state_ttl_sec
        ]
        for object_id in expired:
            self.clear(object_id)
        return expired

    def mark_observed(self, object_id, now=None):
        now = time.monotonic() if now is None else float(now)
        self.last_seen_at[object_id] = now

    def update(self, object_id, candidate, now=None):
        self.mark_observed(object_id, now=now)
        history = self.history.setdefault(object_id, deque(maxlen=self.window))
        history.append(candidate)

        if candidate is None:
            self.normal_streak[object_id] = self.normal_streak.get(object_id, 0) + 1
        else:
            self.normal_streak[object_id] = 0

        if self.normal_streak.get(object_id, 0) >= self.normal_clear_hits:
            history.clear()
            self.current.pop(object_id, None)
            return None

        danger_hits = [
            action
            for action in history
            if action is not None and action.get("confidence_level") == "danger"
        ]
        target_hits = [action for action in history if action is not None]

        if len(danger_hits) >= self.danger_min_hits:
            selected = max(danger_hits, key=lambda action: action["score"])
            resolved = dict(selected)
            resolved["is_danger"] = True
            resolved["confidence_level"] = "danger"
            self.current[object_id] = resolved
            return resolved

        if len(target_hits) >= self.suspicious_min_hits:
            selected = max(target_hits, key=lambda action: action["score"])
            resolved = dict(selected)
            resolved["is_danger"] = False
            resolved["confidence_level"] = "suspicious"
            self.current[object_id] = resolved
            return resolved

        current = self.current.get(object_id)
        if current is not None and self.normal_streak.get(object_id, 0) < self.normal_clear_hits:
            return current

        self.current.pop(object_id, None)
        return None
