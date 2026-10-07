from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from perception.device import resolve_cuda_device


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PERSON_CLASS_ID = 0
RTMO_INPUT_SIZE = (640, 640)
RTMO_SCORE_THRESHOLD = 0.05
RTMO_KEYPOINT_SCORE_THRESHOLD = 0.30
RTMO_TRACK_MATCH_IOU_THRESHOLD = 0.10


class RTMOPoseDetector:
    """Official RTMO-M ONNX inference with persistent BotSORT IDs."""

    def __init__(
        self,
        *,
        onnx_model,
        tracker,
        device=None,
        score_threshold=RTMO_SCORE_THRESHOLD,
    ):
        self.device = resolve_cuda_device(device)
        self.onnx_model_path = self._required_file(
            onnx_model,
            "RTMO ONNX model",
        )
        self.score_threshold = float(score_threshold)
        self.session = self._build_session()
        self.input_name = self.session.get_inputs()[0].name
        self.output_names = [output.name for output in self.session.get_outputs()]
        self.tracker = self._build_tracker(tracker)

    @staticmethod
    def _required_file(path, label):
        resolved = Path(path)
        if not resolved.is_file():
            raise FileNotFoundError(f"{label} not found: {resolved}")
        return resolved

    def _build_session(self):
        # Import/initialize PyTorch CUDA first so CUDA/cuDNN shared libraries
        # are loaded before ONNX Runtime creates its CUDA provider.
        import torch

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable to PyTorch")
        torch.cuda.init()

        import onnxruntime as ort

        providers = ort.get_available_providers()
        if "CUDAExecutionProvider" not in providers:
            raise RuntimeError(
                "ONNX Runtime CUDAExecutionProvider is unavailable; "
                "install onnxruntime-gpu and verify CUDA/cuDNN runtime"
            )

        try:
            device_index = int(self.device.split(":", 1)[1])
        except (IndexError, ValueError) as exc:
            raise RuntimeError(
                f"RTMO requires an explicit CUDA device, got {self.device!r}"
            ) from exc

        session = ort.InferenceSession(
            str(self.onnx_model_path),
            providers=[
                (
                    "CUDAExecutionProvider",
                    {"device_id": device_index},
                )
            ],
        )
        active = session.get_providers()
        if not active or active[0] != "CUDAExecutionProvider":
            raise RuntimeError(
                "RTMO ONNX session did not activate CUDAExecutionProvider: "
                f"{active}"
            )
        return session

    def _build_tracker(self, tracker_config):
        from ultralytics.trackers.bot_sort import BOTSORT
        from ultralytics.utils import IterableSimpleNamespace, YAML

        tracker_path = Path(tracker_config)
        if not tracker_path.is_absolute():
            tracker_path = PROJECT_ROOT / tracker_path
        tracker_path = self._required_file(tracker_path, "BotSORT config")

        args = IterableSimpleNamespace(**YAML.load(str(tracker_path)))
        args.device = self.device
        if getattr(args, "tracker_type", None) != "botsort":
            raise ValueError(
                f"RTMO pipeline requires BotSORT tracker config: {tracker_path}"
            )
        return BOTSORT(args=args)

    @staticmethod
    def _box_iou(box, boxes):
        if len(boxes) == 0:
            return np.empty((0,), dtype=np.float32)
        box = np.asarray(box, dtype=np.float32)
        boxes = np.asarray(boxes, dtype=np.float32)
        x1 = np.maximum(box[0], boxes[:, 0])
        y1 = np.maximum(box[1], boxes[:, 1])
        x2 = np.minimum(box[2], boxes[:, 2])
        y2 = np.minimum(box[3], boxes[:, 3])
        inter = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
        box_area = max(0.0, float(box[2] - box[0])) * max(
            0.0, float(box[3] - box[1])
        )
        boxes_area = np.maximum(0.0, boxes[:, 2] - boxes[:, 0]) * np.maximum(
            0.0, boxes[:, 3] - boxes[:, 1]
        )
        union = box_area + boxes_area - inter
        return np.divide(
            inter,
            union,
            out=np.zeros_like(inter, dtype=np.float32),
            where=union > 0,
        )

    @staticmethod
    def _preprocess(frame):
        input_h, input_w = RTMO_INPUT_SIZE
        height, width = frame.shape[:2]
        ratio = min(input_h / height, input_w / width)
        resized_w = max(1, int(width * ratio))
        resized_h = max(1, int(height * ratio))
        resized = cv2.resize(
            frame,
            (resized_w, resized_h),
            interpolation=cv2.INTER_LINEAR,
        )
        padded = np.full(
            (input_h, input_w, 3),
            114,
            dtype=np.uint8,
        )
        padded[:resized_h, :resized_w] = resized
        tensor = np.ascontiguousarray(
            padded.transpose(2, 0, 1)[None],
            dtype=np.float32,
        )
        return tensor, float(ratio)

    def _infer_outputs(self, frame):
        tensor, ratio = self._preprocess(frame)
        outputs = self.session.run(
            self.output_names,
            {self.input_name: tensor},
        )
        if len(outputs) != 2:
            raise RuntimeError(
                f"Unexpected RTMO ONNX output count: {len(outputs)}"
            )
        return outputs[0], outputs[1], ratio

    def _postprocess_outputs(self, det_outputs, pose_outputs, ratio):
        det_outputs = np.asarray(det_outputs)
        pose_outputs = np.asarray(pose_outputs)
        if (
            det_outputs.ndim != 3
            or det_outputs.shape[0] != 1
            or det_outputs.shape[-1] < 5
        ):
            raise RuntimeError(
                f"Unexpected RTMO detection output shape: {det_outputs.shape}"
            )
        if (
            pose_outputs.ndim != 4
            or pose_outputs.shape[0] != 1
            or pose_outputs.shape[2] != 17
            or pose_outputs.shape[-1] < 3
        ):
            raise RuntimeError(
                f"Unexpected RTMO pose output shape: {pose_outputs.shape}"
            )

        count = min(det_outputs.shape[1], pose_outputs.shape[1])
        if count == 0:
            return (
                np.empty((0, 4), dtype=np.float32),
                np.empty((0,), dtype=np.float32),
                np.empty((0, 17, 2), dtype=np.float32),
                np.empty((0, 17), dtype=np.float32),
            )

        boxes = np.asarray(
            det_outputs[0, :count, :4],
            dtype=np.float32,
        ) / max(float(ratio), 1e-8)
        scores = np.asarray(
            det_outputs[0, :count, 4],
            dtype=np.float32,
        )
        keypoints = np.asarray(
            pose_outputs[0, :count, :, :2],
            dtype=np.float32,
        ) / max(float(ratio), 1e-8)
        keypoint_scores = np.asarray(
            pose_outputs[0, :count, :, 2],
            dtype=np.float32,
        )

        # This official MMDeploy end2end graph already applies the RTMO
        # model test_cfg score threshold and NMS. Applying a second IoU NMS
        # here would preferentially remove overlapping people, which is
        # especially harmful for assault-scene recall. Keep every exported
        # instance above the lightweight padding/garbage floor and let
        # BotSORT handle temporal association.
        keep = np.flatnonzero(scores >= self.score_threshold)
        return (
            boxes[keep],
            scores[keep],
            keypoints[keep],
            keypoint_scores[keep],
        )

    def _pose_predictions(self, frame):
        det_outputs, pose_outputs, ratio = self._infer_outputs(frame)
        return self._postprocess_outputs(
            det_outputs,
            pose_outputs,
            ratio,
        )

    def _associate_pose_indices(self, tracked_boxes, pose_boxes):
        """Globally associate current BotSORT boxes back to RTMO poses by IoU."""
        tracked_boxes = np.asarray(tracked_boxes, dtype=np.float32).reshape(-1, 4)
        pose_boxes = np.asarray(pose_boxes, dtype=np.float32).reshape(-1, 4)
        if len(tracked_boxes) == 0 or len(pose_boxes) == 0:
            return {}

        from scipy.optimize import linear_sum_assignment

        iou_matrix = np.stack(
            [self._box_iou(box, pose_boxes) for box in tracked_boxes],
            axis=0,
        )
        row_indices, pose_indices = linear_sum_assignment(
            1.0 - iou_matrix
        )

        associations = {}
        for row_index, pose_index in zip(row_indices, pose_indices):
            if (
                float(iou_matrix[row_index, pose_index])
                >= RTMO_TRACK_MATCH_IOU_THRESHOLD
            ):
                associations[int(row_index)] = int(pose_index)
        return associations

    @staticmethod
    def _visible_keypoints(keypoints, scores):
        visible = np.asarray(keypoints, dtype=np.float32).copy()
        scores = np.asarray(scores, dtype=np.float32)
        if visible.ndim == 2 and scores.ndim == 1:
            visible[scores < RTMO_KEYPOINT_SCORE_THRESHOLD] = 0.0
        return visible

    def track(self, frame):
        from ultralytics.engine.results import Boxes

        bboxes, bbox_scores, keypoints, keypoint_scores = (
            self._pose_predictions(frame)
        )
        if len(bboxes) == 0:
            detection_data = np.empty((0, 6), dtype=np.float32)
        else:
            classes = np.full(
                (len(bboxes), 1),
                PERSON_CLASS_ID,
                dtype=np.float32,
            )
            detection_data = np.concatenate(
                [
                    bboxes,
                    bbox_scores[:, None],
                    classes,
                ],
                axis=1,
            )

        detections = Boxes(
            detection_data,
            orig_shape=frame.shape[:2],
        ).numpy()
        tracks = self.tracker.update(detections, frame)

        track_rows = [
            np.asarray(row)
            for row in np.asarray(tracks)
            if len(row) >= 8
        ]
        tracked_boxes_for_match = np.asarray(
            [row[:4] for row in track_rows],
            dtype=np.float32,
        ).reshape(-1, 4)
        pose_associations = self._associate_pose_indices(
            tracked_boxes_for_match,
            bboxes,
        )

        tracked = []
        for row_index, row in enumerate(track_rows):
            box = np.asarray(row[:4], dtype=np.float32)
            track_id = int(row[4])
            score = float(row[5])
            cls_id = int(row[6])

            # Ultralytics may re-index detections after confidence filtering.
            # Associate the complete set of current tracks to RTMO poses in
            # one Hungarian assignment rather than trusting tracker indices or
            # using order-dependent greedy matching.
            source_index = pose_associations.get(row_index)
            if source_index is None:
                pose = None
                pose_scores = None
            else:
                pose_scores = np.asarray(
                    keypoint_scores[source_index],
                    dtype=np.float32,
                )
                pose = self._visible_keypoints(
                    keypoints[source_index],
                    pose_scores,
                )

            tracked.append(
                {
                    "id": track_id,
                    "box": box,
                    "cls": cls_id,
                    "score": score,
                    "center": (
                        float((box[0] + box[2]) / 2.0),
                        float((box[1] + box[3]) / 2.0),
                    ),
                    "keypoints": pose,
                    "keypoints_scores": pose_scores,
                }
            )
        return tracked

    def reset_tracking_state(self):
        reset = getattr(self.tracker, "reset", None)
        if callable(reset):
            reset()
