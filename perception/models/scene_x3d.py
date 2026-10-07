from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np
import torch

from perception.device import resolve_cuda_device


LOGGER = logging.getLogger(__name__)

X3D_NUM_FRAMES = 16
X3D_FRAME_SIZE = 224
X3D_CLIP_DURATION_SEC = 4.0
X3D_INFERENCE_INTERVAL_SEC = 1.0
X3D_VIOLENCE_THRESHOLD = 0.4
X3D_NORMAL_CLEAR_HITS = 2
X3D_BUFFER_EXTRA_SEC = 1.0

DEFAULT_SKELETON_LINKS = [
    (15, 13), (13, 11), (16, 14), (14, 12), (11, 12),
    (5, 11), (6, 12), (5, 6), (5, 7), (6, 8), (7, 9),
    (8, 10), (1, 2), (0, 1), (0, 2), (1, 3), (2, 4),
]


class SceneViolenceRecognizer:
    """Scene-level X3D-M violence classifier over a rolling 4-second window."""

    scene_level_classifier = True
    restrict_to_target_actions = True
    analyze_all_persons = True
    manages_action_hysteresis = False
    skeleton_links = DEFAULT_SKELETON_LINKS

    def __init__(self, device=None, *, checkpoint: str | Path):
        self.device = resolve_cuda_device(device)
        self.checkpoint = str(checkpoint)
        self.num_frames = X3D_NUM_FRAMES
        self.frame_size = X3D_FRAME_SIZE
        self.clip_duration_sec = X3D_CLIP_DURATION_SEC
        self.inference_interval_sec = X3D_INFERENCE_INTERVAL_SEC
        self.threshold = X3D_VIOLENCE_THRESHOLD
        self.normal_clear_hits = X3D_NORMAL_CLEAR_HITS
        self._danger_latched = False
        self._normal_clear_streak = 0
        self._frames = deque()
        self._buffer_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._last_inference_at = None
        self._last_result = self._warming_result(0.0)
        self.model = self._load_model()

    @staticmethod
    def _normalize_checkpoint_state_dict(state_dict):
        """Normalize the released violence checkpoint to PyTorchVideo X3D keys."""
        normalized = {}

        for key, value in state_dict.items():
            normalized_key = str(key)

            if normalized_key.startswith("module."):
                normalized_key = normalized_key[len("module."):]

            if normalized_key.startswith("backbone."):
                normalized_key = normalized_key[len("backbone."):]

            if normalized_key == "blocks.5.proj.1.weight":
                normalized_key = "blocks.5.proj.weight"
            elif normalized_key == "blocks.5.proj.1.bias":
                normalized_key = "blocks.5.proj.bias"

            if normalized_key in normalized:
                raise RuntimeError(
                    "Duplicate X3D checkpoint key after normalization: "
                    f"{normalized_key}"
                )

            normalized[normalized_key] = value

        return normalized

    def _load_model(self):
        from pytorchvideo.models.hub import x3d_m

        model = x3d_m(pretrained=False)
        model.blocks[5].proj = torch.nn.Linear(2048, 2)
        checkpoint = torch.load(
            self.checkpoint,
            map_location="cpu",
            weights_only=False,
        )
        if isinstance(checkpoint, dict):
            state_dict = checkpoint.get(
                "model",
                checkpoint.get(
                    "model_state_dict",
                    checkpoint.get("state_dict", checkpoint),
                ),
            )
        else:
            state_dict = checkpoint
        if not isinstance(state_dict, dict):
            raise RuntimeError("X3D checkpoint does not contain a state dict")

        state_dict = self._normalize_checkpoint_state_dict(state_dict)
        model.load_state_dict(state_dict, strict=True)
        model.to(self.device)
        model.eval()
        return model

    def observe_frame(self, frame, timestamp=None):
        if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.size == 0:
            return
        observed_at = time.time() if timestamp is None else float(timestamp)
        resized = cv2.resize(
            frame,
            (self.frame_size, self.frame_size),
            interpolation=cv2.INTER_AREA,
        )
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        cutoff = observed_at - (
            self.clip_duration_sec + X3D_BUFFER_EXTRA_SEC
        )
        with self._buffer_lock:
            self._frames.append((observed_at, rgb))
            while self._frames and self._frames[0][0] < cutoff:
                self._frames.popleft()

    def _sample_clip(self):
        with self._buffer_lock:
            frames = list(self._frames)
        if len(frames) < self.num_frames:
            span = 0.0 if len(frames) < 2 else frames[-1][0] - frames[0][0]
            return None, max(0.0, span)

        end_at = frames[-1][0]
        start_at = end_at - self.clip_duration_sec
        if frames[0][0] > start_at:
            return None, max(0.0, end_at - frames[0][0])

        timestamps = np.asarray([item[0] for item in frames], dtype=np.float64)
        targets = np.linspace(
            start_at,
            end_at,
            self.num_frames,
            dtype=np.float64,
        )
        selected = []
        for target in targets:
            right = int(np.searchsorted(timestamps, target, side="left"))
            candidates = []
            if right < len(frames):
                candidates.append(right)
            if right > 0:
                candidates.append(right - 1)
            index = min(
                candidates,
                key=lambda idx: abs(timestamps[idx] - target),
            )
            selected.append(frames[index][1])
        return selected, self.clip_duration_sec

    def _build_tensor(self, frames):
        array = np.stack(frames, axis=0)
        tensor = torch.from_numpy(array).to(dtype=torch.float32).div_(255.0)
        tensor = tensor.permute(3, 0, 1, 2).unsqueeze(0)
        mean = torch.tensor(
            [0.45, 0.45, 0.45],
            dtype=tensor.dtype,
        ).view(1, 3, 1, 1, 1)
        std = torch.tensor(
            [0.225, 0.225, 0.225],
            dtype=tensor.dtype,
        ).view(1, 3, 1, 1, 1)
        tensor = (tensor - mean) / std
        return tensor.to(self.device, non_blocking=True)

    def _predict_clip(self, frames):
        video = self._build_tensor(frames)
        with torch.inference_mode():
            logits = self.model(video)
            probabilities = torch.softmax(logits, dim=1)
        return float(probabilities[0, 1].detach().cpu().item())

    def _warming_result(self, span_sec):
        return {
            "status": "warming_up",
            "label": None,
            "score": None,
            "danger": False,
            "threshold": self.threshold,
            "available": False,
            "fresh": False,
            "clip_span_sec": round(float(span_sec), 3),
            "error": None,
        }

    def _apply_temporal_state(self, violence_score):
        raw_danger = float(violence_score) >= self.threshold
        if raw_danger:
            self._danger_latched = True
            self._normal_clear_streak = 0
        elif self._danger_latched:
            self._normal_clear_streak += 1
            if self._normal_clear_streak >= self.normal_clear_hits:
                self._danger_latched = False
                self._normal_clear_streak = 0
        else:
            self._normal_clear_streak = 0
        return self._danger_latched, raw_danger

    def classify_scene(self, now=None):
        current_at = time.time() if now is None else float(now)
        with self._inference_lock:
            if (
                self._last_inference_at is not None
                and current_at - self._last_inference_at
                < self.inference_interval_sec
            ):
                cached = dict(self._last_result)
                cached["fresh"] = False
                return cached

            frames, span_sec = self._sample_clip()
            if frames is None:
                result = self._warming_result(span_sec)
                result["danger"] = self._danger_latched
                self._last_result = result
                return dict(result)

            self._last_inference_at = current_at
            try:
                violence_score = self._predict_clip(frames)
                danger, raw_danger = self._apply_temporal_state(violence_score)
                result = {
                    "status": "ok",
                    "label": "VIOLENCE" if danger else "NORMAL",
                    "score": violence_score,
                    "danger": danger,
                    "raw_danger": raw_danger,
                    "normal_clear_streak": self._normal_clear_streak,
                    "normal_clear_hits": self.normal_clear_hits,
                    "threshold": self.threshold,
                    "available": True,
                    "fresh": True,
                    "clip_span_sec": round(float(span_sec), 3),
                    "updated_at": time.time(),
                    "error": None,
                }
            except Exception as exc:
                LOGGER.exception("X3D scene inference failed")
                result = {
                    "status": "inference_error",
                    "label": None,
                    "score": None,
                    "danger": self._danger_latched,
                    "raw_danger": None,
                    "normal_clear_streak": self._normal_clear_streak,
                    "normal_clear_hits": self.normal_clear_hits,
                    "threshold": self.threshold,
                    "available": False,
                    "fresh": True,
                    "clip_span_sec": round(float(span_sec), 3),
                    "updated_at": time.time(),
                    "error": str(exc) or "X3D scene inference failed",
                }
            self._last_result = result
            return dict(result)

    def reset_tracking_state(self):
        with self._buffer_lock:
            self._frames.clear()
        with self._inference_lock:
            self._last_inference_at = None
            self._danger_latched = False
            self._normal_clear_streak = 0
            self._last_result = self._warming_result(0.0)

    def draw_skeleton(self, frame, keypoints, color):
        if keypoints is None:
            return
        points = [(int(x), int(y)) for x, y in keypoints]
        height, width = frame.shape[:2]
        for x, y in points:
            if (x, y) != (0, 0) and 0 <= x < width and 0 <= y < height:
                cv2.circle(frame, (x, y), 3, color, -1, cv2.LINE_AA)
        for start, end in self.skeleton_links:
            if start >= len(points) or end >= len(points):
                continue
            x1, y1 = points[start]
            x2, y2 = points[end]
            if (x1, y1) == (0, 0) or (x2, y2) == (0, 0):
                continue
            if (
                0 <= x1 < width
                and 0 <= y1 < height
                and 0 <= x2 < width
                and 0 <= y2 < height
            ):
                cv2.line(frame, (x1, y1), (x2, y2), color, 2, cv2.LINE_AA)

    def draw_scene_overlay(self, frame, result):
        if not result:
            return

        status = str(result.get("status") or "")
        danger = bool(result.get("danger"))
        score = result.get("score")

        if status == "ok" and danger:
            accent = (0, 0, 255)
            title = "SCENE AI  |  VIOLENCE"
            detail = f"{float(score or 0.0) * 100:.0f}%"
        elif status == "ok":
            accent = (70, 190, 90)
            title = "SCENE AI  |  NORMAL"
            detail = f"violence {float(score or 0.0) * 100:.0f}%"
        elif status == "warming_up":
            accent = (150, 150, 150)
            title = "SCENE AI  |  WARMING UP"
            detail = f"{float(result.get('clip_span_sec') or 0.0):.1f}s / {self.clip_duration_sec:.1f}s"
        else:
            accent = (0, 165, 255)
            title = "SCENE AI  |  UNAVAILABLE"
            detail = "inference error"

        height, width = frame.shape[:2]
        if danger:
            # Full-frame danger border with corner accents for a clear,
            # presentation-ready scene-level warning.
            cv2.rectangle(
                frame,
                (3, 3),
                (max(3, width - 4), max(3, height - 4)),
                (0, 0, 255),
                4,
            )
            corner = max(24, min(width, height) // 12)
            for x1, y1, x2, y2 in (
                (3, 3, 3 + corner, 3),
                (3, 3, 3, 3 + corner),
                (width - 4, 3, width - 4 - corner, 3),
                (width - 4, 3, width - 4, 3 + corner),
                (3, height - 4, 3 + corner, height - 4),
                (3, height - 4, 3, height - 4 - corner),
                (width - 4, height - 4, width - 4 - corner, height - 4),
                (width - 4, height - 4, width - 4, height - 4 - corner),
            ):
                cv2.line(frame, (x1, y1), (x2, y2), (0, 0, 255), 6)

        font = cv2.FONT_HERSHEY_SIMPLEX
        title_scale = 0.62
        detail_scale = 0.50
        title_thickness = 2
        detail_thickness = 1
        (title_w, title_h), _ = cv2.getTextSize(
            title, font, title_scale, title_thickness
        )
        (detail_w, detail_h), _ = cv2.getTextSize(
            detail, font, detail_scale, detail_thickness
        )

        panel_x = 14
        panel_y = 14
        panel_w = min(width - 28, max(title_w, detail_w) + 32)
        panel_h = title_h + detail_h + 32
        if panel_w <= 0 or panel_h <= 0:
            return

        overlay = frame.copy()
        cv2.rectangle(
            overlay,
            (panel_x, panel_y),
            (panel_x + panel_w, panel_y + panel_h),
            (20, 20, 20),
            -1,
        )
        cv2.addWeighted(overlay, 0.78, frame, 0.22, 0, frame)
        cv2.rectangle(
            frame,
            (panel_x, panel_y),
            (panel_x + 5, panel_y + panel_h),
            accent,
            -1,
        )
        cv2.putText(
            frame,
            title,
            (panel_x + 16, panel_y + title_h + 9),
            font,
            title_scale,
            (245, 245, 245),
            title_thickness,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            detail,
            (panel_x + 16, panel_y + title_h + detail_h + 20),
            font,
            detail_scale,
            accent,
            detail_thickness,
            cv2.LINE_AA,
        )

