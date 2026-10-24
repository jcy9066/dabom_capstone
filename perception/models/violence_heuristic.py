from collections import defaultdict, deque

import numpy as np

try:
    from perception.env_config import env_bool, env_float, env_int
except ModuleNotFoundError:  # Direct perception script execution.
    from env_config import env_bool, env_float, env_int


def _point_valid(point):
    return point is not None and np.isfinite(point).all() and not np.allclose(point, 0.0)


def _box_scale(box):
    x1, y1, x2, y2 = map(float, box)
    width = max(1.0, x2 - x1)
    height = max(1.0, y2 - y1)
    return max(width, height)


def _box_center(box):
    x1, y1, x2, y2 = map(float, box)
    return np.array([(x1 + x2) * 0.5, (y1 + y2) * 0.5], dtype=np.float32)


def _safe_point(kpts, idx):
    if kpts is None or idx >= len(kpts):
        return None
    point = np.asarray(kpts[idx], dtype=np.float32)
    return point if _point_valid(point) else None


def _mean_valid(points):
    valid = [point for point in points if _point_valid(point)]
    if not valid:
        return None
    return np.mean(valid, axis=0)


class ViolenceHeuristic:
    ARM_STRIKE_JOINTS = (7, 8, 9, 10)
    LEG_STRIKE_JOINTS = (15, 16)
    TARGET_JOINTS = (0, 5, 6, 11, 12)

    def __init__(self):
        self.enabled = env_bool("VIOLENCE_HEURISTIC_ENABLED")
        self.use_leg_strikes = env_bool("VIOLENCE_HEURISTIC_USE_LEG_STRIKES")
        self.use_box_target_points = env_bool("VIOLENCE_HEURISTIC_USE_BOX_TARGET_POINTS")
        self.history_frames = env_int("VIOLENCE_HEURISTIC_HISTORY_FRAMES", minimum=1)
        self.suspicious_frames = env_int("VIOLENCE_HEURISTIC_SUSPICIOUS_FRAMES", minimum=1)
        self.danger_frames = env_int("VIOLENCE_HEURISTIC_DANGER_FRAMES", minimum=1)
        self.motion_threshold = env_float("VIOLENCE_HEURISTIC_MOTION_THRESHOLD", minimum=0.01)
        self.approach_threshold = env_float("VIOLENCE_HEURISTIC_APPROACH_THRESHOLD", minimum=0.0)
        self.proximity_threshold = env_float("VIOLENCE_HEURISTIC_PROXIMITY_THRESHOLD", minimum=0.05)
        self.pair_distance_threshold = env_float("VIOLENCE_HEURISTIC_PAIR_DISTANCE_THRESHOLD", minimum=0.5)
        self.suspicious_score = env_float("VIOLENCE_HEURISTIC_SUSPICIOUS_SCORE", minimum=0.01)
        self.danger_score = env_float("VIOLENCE_HEURISTIC_DANGER_SCORE", minimum=self.suspicious_score)
        self.prev_states = {}
        self.score_history = defaultdict(lambda: deque(maxlen=self.history_frames))
        self.missing_counts = defaultdict(int)

    def reset(self):
        self.prev_states.clear()
        self.score_history.clear()
        self.missing_counts.clear()

    def update(self, objects, skeletons_by_id):
        if not self.enabled:
            return {}

        persons = []
        for obj in objects:
            if obj.get("cls", 0) != 0:
                continue
            oid = obj.get("id")
            skeleton = skeletons_by_id.get(oid)
            if oid is None or skeleton is None:
                continue

            kpts = np.asarray(skeleton, dtype=np.float32)
            if kpts.ndim != 2 or kpts.shape[0] < 17 or kpts.shape[1] < 2:
                continue

            box = obj.get("box")
            center = _box_center(box)
            persons.append({
                "id": oid,
                "box": box,
                "center": center,
                "scale": _box_scale(box),
                "kpts": kpts[:, :2],
                "prev": self.prev_states.get(oid),
            })

        raw_scores = {person["id"]: 0.0 for person in persons}
        for i, person_a in enumerate(persons):
            for person_b in persons[i + 1:]:
                if not self._pair_close_enough(person_a, person_b):
                    continue
                score_ab = self._directed_score(person_a, person_b)
                score_ba = self._directed_score(person_b, person_a)
                pair_score = max(score_ab, score_ba)
                if pair_score <= 0.0:
                    continue
                raw_scores[person_a["id"]] = max(raw_scores[person_a["id"]], pair_score)
                raw_scores[person_b["id"]] = max(raw_scores[person_b["id"]], pair_score)

        results = {}
        for person in persons:
            oid = person["id"]
            history = self.score_history[oid]
            history.append(raw_scores.get(oid, 0.0))
            danger_hits = sum(1 for value in history if value >= self.danger_score)
            suspicious_hits = sum(1 for value in history if value >= self.suspicious_score)
            peak_score = max(history) if history else 0.0
            recent_avg = sum(history) / len(history) if history else 0.0
            display_score = max(peak_score, recent_avg)

            if danger_hits >= self.danger_frames:
                results[oid] = self._action(display_score, True)
            elif suspicious_hits >= self.suspicious_frames:
                results[oid] = self._action(display_score, False)

        self._cleanup_state(persons)
        return results

    def _strike_joints(self):
        if self.use_leg_strikes:
            return self.ARM_STRIKE_JOINTS + self.LEG_STRIKE_JOINTS
        return self.ARM_STRIKE_JOINTS

    def _pair_close_enough(self, person_a, person_b):
        distance = float(np.linalg.norm(person_a["center"] - person_b["center"]))
        avg_scale = max(1.0, (person_a["scale"] + person_b["scale"]) * 0.5)
        return distance / avg_scale <= self.pair_distance_threshold

    def _directed_score(self, attacker, target):
        prev = attacker.get("prev")
        if prev is None:
            return 0.0

        target_points = self._target_points(target["kpts"], target["box"])
        if not target_points:
            return 0.0

        best = 0.0
        norm = max(1.0, attacker["scale"])
        target_norm = max(1.0, target["scale"])
        curr_center = attacker["center"]
        prev_center = prev["center"]

        for joint_idx in self._strike_joints():
            curr_point = _safe_point(attacker["kpts"], joint_idx)
            prev_point = _safe_point(prev["kpts"], joint_idx)
            if curr_point is None or prev_point is None:
                continue

            # Remove whole-person translation so ordinary walking and camera drift do not look like strikes.
            curr_relative = curr_point - curr_center
            prev_relative = prev_point - prev_center
            movement = curr_relative - prev_relative
            velocity = float(np.linalg.norm(movement)) / norm
            if velocity < self.motion_threshold:
                continue

            nearest_target = min(target_points, key=lambda point: float(np.linalg.norm(curr_point - point)))
            curr_distance = float(np.linalg.norm(curr_point - nearest_target)) / target_norm
            prev_distance = float(np.linalg.norm(prev_point - nearest_target)) / target_norm
            approach = prev_distance - curr_distance
            if curr_distance > self.proximity_threshold or approach < self.approach_threshold:
                continue

            target_vector = nearest_target - prev_point
            target_norm_value = float(np.linalg.norm(target_vector))
            movement_norm = float(np.linalg.norm(movement))
            if target_norm_value < 1e-3 or movement_norm < 1e-3:
                continue

            cosine = float(np.dot(movement, target_vector) / (movement_norm * target_norm_value))
            if cosine < 0.35:
                continue

            motion_score = min(1.0, velocity / (self.motion_threshold * 2.2))
            proximity_score = max(0.0, 1.0 - (curr_distance / self.proximity_threshold))
            approach_score = min(1.0, approach / max(self.approach_threshold * 3.0, 1e-3))
            direction_score = min(1.0, max(0.0, cosine))
            joint_score = (motion_score * 0.40) + (proximity_score * 0.25) + (approach_score * 0.25) + (direction_score * 0.10)
            best = max(best, joint_score)

        return best

    def _target_points(self, kpts, box):
        points = []
        for idx in self.TARGET_JOINTS:
            point = _safe_point(kpts, idx)
            if point is not None:
                points.append(point)

        torso = _mean_valid([_safe_point(kpts, idx) for idx in (5, 6, 11, 12)])
        if torso is not None:
            points.append(torso)
        upper_body = _mean_valid([_safe_point(kpts, idx) for idx in (0, 5, 6)])
        if upper_body is not None:
            points.append(upper_body)

        if self.use_box_target_points:
            x1, y1, x2, y2 = map(float, box)
            box_width = max(1.0, x2 - x1)
            box_height = max(1.0, y2 - y1)
            points.extend([
                np.array([x1 + box_width * 0.50, y1 + box_height * 0.32], dtype=np.float32),
                np.array([x1 + box_width * 0.50, y1 + box_height * 0.50], dtype=np.float32),
            ])
        return points

    def _action(self, score, is_danger):
        return {
            "label": "VIOLENCE",
            "score": float(min(1.0, max(0.0, score))),
            "is_danger": bool(is_danger),
            "confidence_level": "danger" if is_danger else "suspicious",
            "source": "violence_heuristic",
        }

    def _cleanup_state(self, persons):
        active_ids = {person["id"] for person in persons}
        for person in persons:
            self.prev_states[person["id"]] = {
                "kpts": person["kpts"].copy(),
                "center": person["center"].copy(),
            }
            self.missing_counts[person["id"]] = 0

        known_ids = set(self.prev_states) | set(self.score_history) | set(self.missing_counts)
        for oid in known_ids - active_ids:
            self.missing_counts[oid] += 1
            if self.missing_counts[oid] > self.history_frames:
                self.prev_states.pop(oid, None)
                self.score_history.pop(oid, None)
                self.missing_counts.pop(oid, None)
