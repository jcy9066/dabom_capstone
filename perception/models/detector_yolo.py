import os

from ultralytics import YOLO

from perception.device import resolve_cuda_device


PERSON_CLASS_ID = 0


class YOLODetector:
    def __init__(self, weight, tracker, device=None, conf=0.25):
        if not os.path.exists(weight):
            raise FileNotFoundError(f"가중치 파일 없음: {weight}")

        confidence = float(conf)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("YOLO confidence threshold must be within [0, 1]")

        self.model = YOLO(weight)
        self.tracker = (
            str(tracker)
            if str(tracker).endswith(".yaml")
            else f"{tracker}.yaml"
        )
        self.target_classes = [PERSON_CLASS_ID]
        self.device = resolve_cuda_device(device)
        self.conf = confidence

    def reset_tracking_state(self):
        predictor = getattr(self.model, "predictor", None)
        if predictor is None:
            return

        for tracker in getattr(predictor, "trackers", None) or []:
            reset = getattr(tracker, "reset", None)
            if callable(reset):
                reset()

        if hasattr(predictor, "vid_path"):
            vid_path = getattr(predictor, "vid_path", None) or []
            predictor.vid_path = [None] * len(vid_path)

    def _track_results(self, frame):
        return self.model.track(
            frame,
            persist=True,
            tracker=self.tracker,
            verbose=False,
            classes=self.target_classes,
            conf=self.conf,
            imgsz=640,
            device=self.device,
        )

    def track(self, frame):
        results = self._track_results(frame)
        boxes = []
        if results[0].boxes.id is not None:
            xyxy = results[0].boxes.xyxy.cpu().numpy()
            ids = results[0].boxes.id.cpu().numpy()
            clss = results[0].boxes.cls.cpu().numpy()
            for box, track_id, cls_id in zip(xyxy, ids, clss):
                boxes.append(
                    {
                        "id": int(track_id),
                        "box": box,
                        "cls": int(cls_id),
                        "center": (
                            (box[0] + box[2]) / 2,
                            (box[1] + box[3]) / 2,
                        ),
                    }
                )
        return boxes


class YOLOPoseDetector(YOLODetector):
    def track(self, frame):
        results = self._track_results(frame)
        boxes = []
        if results[0].boxes.id is not None:
            xyxy = results[0].boxes.xyxy.cpu().numpy()
            ids = results[0].boxes.id.cpu().numpy()
            clss = results[0].boxes.cls.cpu().numpy()
            keypoints = results[0].keypoints.xy.cpu().numpy()
            keypoint_scores = results[0].keypoints.conf.cpu().numpy()

            for box, track_id, cls_id, kpts, scores in zip(
                xyxy,
                ids,
                clss,
                keypoints,
                keypoint_scores,
            ):
                boxes.append(
                    {
                        "id": int(track_id),
                        "box": box,
                        "cls": int(cls_id),
                        "center": (
                            (box[0] + box[2]) / 2,
                            (box[1] + box[3]) / 2,
                        ),
                        "keypoints": kpts,
                        "keypoints_scores": scores,
                    }
                )
        return boxes
