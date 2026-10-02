import glob
import logging
import os

import cv2
import numpy as np
import torch
from mmaction.apis import inference_recognizer, init_recognizer
from mmengine.registry import DefaultScope

from perception.device import resolve_cuda_device
from perception.models.action_batch import process_keypoint_many
from perception.models.action_policy import (
    RECALL_TARGET_ACTIONS,
    TemporalActionPolicy,
    classify_target_action,
    classify_target_scores,
)


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

        with DefaultScope.overwrite_default_scope("mmaction"):
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

    def process_many(self, frame, objs):
        return process_keypoint_many(self, frame, objs, total_frames=100)

    def process(self, frame, obj):
        kpts = obj.get("keypoints")
        scores = obj.get("keypoints_scores")

        if kpts is None or len(kpts) == 0:
            return None, None

        obj_id = obj["id"]
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
        anno = dict(
            frame_dir="",
            label=-1,
            img_shape=(shape[0], shape[1]),
            original_shape=(shape[0], shape[1]),
            start_index=0,
            modality="Pose",
            total_frames=100,
            keypoint=np.expand_dims(np.array(kpts), axis=0),
            keypoint_score=np.expand_dims(np.array(scores), axis=0),
        )
        with DefaultScope.overwrite_default_scope("mmaction"):
            result = inference_recognizer(self.action_model, anno)
        return result.pred_score

    def _classify(self, kpts, scores, shape):
        try:
            pred_scores = self._predict_scores(kpts, scores, shape)
            max_idx = torch.argmax(pred_scores).item()
            max_score = pred_scores[max_idx].item()
            return classify_target_action(max_idx, max_score, self.target_actions)
        except Exception:
            return None

    def _classify_with_object(self, obj_id, kpts, scores, shape):
        if not self.recall_mode:
            return self._classify(kpts, scores, shape)

        try:
            pred_scores = self._predict_scores(kpts, scores, shape)
            candidate = classify_target_scores(pred_scores, self.target_actions)
            return self.temporal_policy.update(obj_id, candidate)
        except Exception:
            return None

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
