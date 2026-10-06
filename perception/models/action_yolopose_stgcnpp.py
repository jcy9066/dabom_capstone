import glob
import logging

import cv2
import numpy as np
import torch

from perception.device import resolve_cuda_device
from perception.env_config import env_float
from perception.models.action_batch import process_keypoint_many
from perception.models.action_policy import (
    OBSERVATION_INFERENCE_ERROR,
    OBSERVATION_UNAVAILABLE,
    RECALL_TARGET_ACTIONS,
    TemporalActionPolicy,
    classify_target_action,
    classify_target_scores,
    is_observation_issue,
    observation_issue,
)


LOGGER = logging.getLogger(__name__)


def find_weight(pattern):
    files = glob.glob(pattern)
    if not files:
        raise FileNotFoundError(f"에러: 가중치 파일 없음 -> {pattern}")
    return files[0]


class ActionRecognizer:
    def __init__(
        self,
        device=None,
        *,
        recall_mode=False,
        action_config=None,
        action_checkpoint=None,
    ):
        device = resolve_cuda_device(device)
        logging.getLogger("mmengine").setLevel(logging.ERROR)

        print("⏳ 로컬 환경에서 ST-GCN++ 모델을 적재합니다... (MMPose 생략)")
        action_config = action_config or find_weight("weights/stgcnpp_8xb16-joint-u100*.py")
        action_checkpoint = action_checkpoint or find_weight("weights/stgcnpp_8xb16-joint-u100*.pth")

        from mmaction.apis import inference_recognizer, init_recognizer
        from mmengine.registry import DefaultScope

        self._inference_recognizer = inference_recognizer
        self._default_scope_cls = DefaultScope
        with self._default_scope_cls.overwrite_default_scope("mmaction"):
            self.action_model = init_recognizer(action_config, action_checkpoint, device=device)

        self.action_buffer = {}
        self.skeleton_links = [
            (15, 13),
            (13, 11),
            (16, 14),
            (14, 12),
            (11, 12),
            (5, 11),
            (6, 12),
            (5, 6),
            (5, 7),
            (6, 8),
            (7, 9),
            (8, 10),
            (1, 2),
            (0, 1),
            (0, 2),
            (1, 3),
            (2, 4),
        ]
        self.recall_mode = bool(recall_mode)
        self.analyze_all_persons = self.recall_mode
        self.manages_action_hysteresis = self.recall_mode
        self.restrict_to_target_actions = self.recall_mode
        self.target_actions = (
            RECALL_TARGET_ACTIONS
            if self.recall_mode
            else {
                42: {"name": "FALLING", "danger": True},
                49: {"name": "PUNCHING", "danger": True},
                50: {"name": "KICKING", "danger": True},
                51: {"name": "PUSHING", "danger": True},
            }
        )
        self.temporal_policy = TemporalActionPolicy.from_env() if self.recall_mode else None
        self.single_person_actions = {
            idx: info for idx, info in self.target_actions.items() if idx in {41, 42}
        }
        self.interaction_actions = {
            idx: info for idx, info in self.target_actions.items() if idx in {49, 50, 51}
        }
        self.pair_action_buffer = {}
        self.pair_temporal_policy = TemporalActionPolicy.from_env() if self.recall_mode else None
        self.interaction_pair_distance_ratio = (
            env_float("ACTION_INTERACTION_PAIR_DISTANCE_RATIO", default=1.5, minimum=0.1)
            if self.recall_mode
            else 1.5
        )

    def expire_tracking_state(self, now=None):
        if self.temporal_policy is None:
            return []
        expired = self.temporal_policy.expire_stale(now=now)
        for object_id in expired:
            self.action_buffer.pop(object_id, None)

        if self.pair_temporal_policy is not None:
            expired_pairs = self.pair_temporal_policy.expire_stale(now=now)
            for pair_id in expired_pairs:
                self.pair_action_buffer.pop(pair_id, None)
        return expired

    def reset_tracking_state(self):
        self.action_buffer.clear()
        self.pair_action_buffer.clear()
        if self.temporal_policy is not None:
            self.temporal_policy.reset()
        if self.pair_temporal_policy is not None:
            self.pair_temporal_policy.reset()

    def _mark_observed(self, object_id):
        if self.temporal_policy is None:
            return
        self.temporal_policy.mark_observed(object_id)

    def observation_unavailable(self, object_id, reason):
        self._mark_observed(object_id)
        return observation_issue(OBSERVATION_UNAVAILABLE, reason)

    def process_many(self, frame, objs):
        self.expire_tracking_state()
        for obj in objs:
            self._mark_observed(obj["id"])

        results = process_keypoint_many(self, frame, objs, total_frames=100)
        if not self.recall_mode or len(objs) < 2:
            return results

        for first_index, first in enumerate(objs):
            for second in objs[first_index + 1 :]:
                if not self._is_interaction_pair(first, second):
                    continue
                pair_action = self._process_interaction_pair(frame, first, second)
                if not pair_action or is_observation_issue(pair_action):
                    continue
                for obj in (first, second):
                    skeleton, current = results.get(obj["id"], (obj.get("keypoints"), None))
                    if (
                        current is None
                        or is_observation_issue(current)
                        or pair_action["score"] >= current.get("score", 0.0)
                    ):
                        results[obj["id"]] = (skeleton, pair_action)
        return results

    def _is_interaction_pair(self, first, second):
        first_box = first.get("box")
        second_box = second.get("box")
        if first_box is None or second_box is None:
            return False

        first_center = first.get("center") or (
            (first_box[0] + first_box[2]) / 2.0,
            (first_box[1] + first_box[3]) / 2.0,
        )
        second_center = second.get("center") or (
            (second_box[0] + second_box[2]) / 2.0,
            (second_box[1] + second_box[3]) / 2.0,
        )
        distance = float(np.linalg.norm(np.asarray(first_center) - np.asarray(second_center)))
        first_height = max(float(first_box[3] - first_box[1]), 1.0)
        second_height = max(float(second_box[3] - second_box[1]), 1.0)
        return distance <= self.interaction_pair_distance_ratio * max(first_height, second_height)

    def _process_interaction_pair(self, frame, first, second):
        ordered = sorted((first, second), key=lambda obj: obj["id"])
        pair_id = tuple(obj["id"] for obj in ordered)
        keypoints = [obj.get("keypoints") for obj in ordered]
        scores = [obj.get("keypoints_scores") for obj in ordered]
        if any(value is None or len(value) == 0 for value in keypoints):
            return None

        buffer = self.pair_action_buffer.setdefault(pair_id, {"kpts": [], "scores": []})
        buffer["kpts"].append(np.stack(keypoints, axis=0))
        buffer["scores"].append(np.stack(scores, axis=0))
        if len(buffer["kpts"]) > 100:
            buffer["kpts"].pop(0)
            buffer["scores"].pop(0)

        pad_len = 100 - len(buffer["kpts"])
        padded_kpts = buffer["kpts"] + [buffer["kpts"][-1]] * pad_len
        padded_scores = buffer["scores"] + [buffer["scores"][-1]] * pad_len

        # MMAction2 skeleton annotations use (M, T, V, C) and (M, T, V).
        pair_kpts = np.transpose(np.asarray(padded_kpts), (1, 0, 2, 3))
        pair_scores = np.transpose(np.asarray(padded_scores), (1, 0, 2))
        self.pair_temporal_policy.mark_observed(pair_id)
        try:
            pred_scores = self._predict_scores_array(pair_kpts, pair_scores, frame.shape)
            candidate = classify_target_scores(pred_scores, self.interaction_actions)
            return self.pair_temporal_policy.update(pair_id, candidate)
        except Exception as exc:
            LOGGER.exception("Interaction inference failed for pair=%s", pair_id)
            return observation_issue(
                OBSERVATION_INFERENCE_ERROR,
                str(exc) or "interaction inference failed",
            )

    def process(self, frame, obj):
        obj_id = obj["id"]
        self.expire_tracking_state()
        self._mark_observed(obj_id)

        kpts = obj.get("keypoints")
        scores = obj.get("keypoints_scores")
        if kpts is None or len(kpts) == 0:
            return None, self.observation_unavailable(
                obj_id,
                "YOLO pose keypoints unavailable",
            )

        if obj_id not in self.action_buffer:
            self.action_buffer[obj_id] = {"kpts": [], "scores": []}

        self.action_buffer[obj_id]["kpts"].append(kpts)
        self.action_buffer[obj_id]["scores"].append(scores)

        cur_kpts = self.action_buffer[obj_id]["kpts"]
        cur_scores = self.action_buffer[obj_id]["scores"]
        pad_len = 100 - len(cur_kpts)

        pad_kpts = cur_kpts + [cur_kpts[-1]] * pad_len if pad_len > 0 else cur_kpts
        pad_scores = cur_scores + [cur_scores[-1]] * pad_len if pad_len > 0 else cur_scores

        action_res = self._classify_with_object(obj_id, pad_kpts, pad_scores, frame.shape)

        if len(self.action_buffer[obj_id]["kpts"]) >= 100:
            self.action_buffer[obj_id]["kpts"].pop(0)
            self.action_buffer[obj_id]["scores"].pop(0)

        return kpts, action_res

    def _predict_scores(self, kpts, scores, shape):
        return self._predict_scores_array(
            np.expand_dims(np.asarray(kpts), axis=0),
            np.expand_dims(np.asarray(scores), axis=0),
            shape,
        )

    def _predict_scores_array(self, keypoints, keypoint_scores, shape):
        anno = dict(
            frame_dir="",
            label=-1,
            img_shape=(shape[0], shape[1]),
            original_shape=(shape[0], shape[1]),
            start_index=0,
            modality="Pose",
            total_frames=100,
            keypoint=keypoints,
            keypoint_score=keypoint_scores,
        )
        with self._default_scope_cls.overwrite_default_scope("mmaction"):
            result = self._inference_recognizer(self.action_model, anno)
        return result.pred_score

    def _classify(self, kpts, scores, shape):
        try:
            pred_scores = self._predict_scores(kpts, scores, shape)
            max_idx = torch.argmax(pred_scores).item()
            max_score = pred_scores[max_idx].item()
            return classify_target_action(max_idx, max_score, self.target_actions)
        except Exception:
            LOGGER.exception("Action inference failed")
            return observation_issue(
                OBSERVATION_INFERENCE_ERROR,
                "action inference failed",
            )

    def _classify_with_object(self, obj_id, kpts, scores, shape):
        if not self.recall_mode:
            return self._classify(kpts, scores, shape)

        try:
            pred_scores = self._predict_scores(kpts, scores, shape)
            target_actions = self.single_person_actions or self.target_actions
            candidate = classify_target_scores(pred_scores, target_actions)
            return self.temporal_policy.update(obj_id, candidate)
        except Exception as exc:
            LOGGER.exception("Action inference failed for track_id=%s", obj_id)
            self._mark_observed(obj_id)
            return observation_issue(
                OBSERVATION_INFERENCE_ERROR,
                str(exc) or "action inference failed",
            )

    def draw_skeleton(self, frame, kpts, color):
        if kpts is None:
            return
        for x, y in kpts:
            cv2.circle(frame, (int(x), int(y)), 2, color, -1)
        for start, end in self.skeleton_links:
            if tuple(kpts[start]) != (0, 0) and tuple(kpts[end]) != (0, 0):
                cv2.line(
                    frame,
                    tuple(map(int, kpts[start])),
                    tuple(map(int, kpts[end])),
                    color,
                    1,
                )
