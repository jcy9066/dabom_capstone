from ultralytics import YOLO
import os
from perception.device import resolve_cuda_device


PERSON_CLASS_ID = 0


class YOLODetector:
    def __init__(self, weight, tracker, device=None):
        if not os.path.exists(weight):
            raise FileNotFoundError(f"가중치 파일 없음: {weight}")
        self.model = YOLO(weight)
        self.tracker = str(tracker) if str(tracker).endswith(".yaml") else f"{tracker}.yaml"
        self.target_classes = [PERSON_CLASS_ID]
        self.device = resolve_cuda_device(device)

    def track(self, frame):
        results = self.model.track(
            frame, persist=True, tracker=self.tracker, half=True, verbose=False, classes=self.target_classes, conf=0.25, imgsz=640, device=self.device
        )
        boxes = []
        if results[0].boxes.id is not None:
            xyxy = results[0].boxes.xyxy.cpu().numpy()
            ids = results[0].boxes.id.cpu().numpy()
            clss = results[0].boxes.cls.cpu().numpy()
            for b, i, c in zip(xyxy, ids, clss):
                boxes.append({'id': int(i), 'box': b, 'cls': int(c), 'center': ((b[0]+b[2])/2, (b[1]+b[3])/2)})
        return boxes

class YOLOPoseDetector(YOLODetector):
    def track(self, frame):
        results = self.model.track(
            frame, persist=True, tracker=self.tracker, half=True, verbose=False, classes=self.target_classes, conf=0.25, imgsz=640, device=self.device
        )
        boxes = []
        if results[0].boxes.id is not None:
            xyxy = results[0].boxes.xyxy.cpu().numpy()
            ids = results[0].boxes.id.cpu().numpy()
            clss = results[0].boxes.cls.cpu().numpy()
            
            # YOLO-Pose 관절 데이터 (17개)
            kpts = results[0].keypoints.xy.cpu().numpy()
            kpts_conf = results[0].keypoints.conf.cpu().numpy()
            
            for b, i, c, k, kc in zip(xyxy, ids, clss, kpts, kpts_conf):
                boxes.append({
                    'id': int(i), 'box': b, 'cls': int(c), 'center': ((b[0]+b[2])/2, (b[1]+b[3])/2),
                    'keypoints': k, 'keypoints_scores': kc
                })
        return boxes
