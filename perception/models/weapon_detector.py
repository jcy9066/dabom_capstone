"""Verified two-category knife/firearm detector for an explicitly supplied YOLO checkpoint.

A generic COCO person detector never implies a weapon. When enabled, both
knife and firearm categories must be present in the checkpoint label vocabulary.
"""
from __future__ import annotations

import math
from pathlib import Path

KNIFE_LABELS = frozenset({"knife", "knives"})
GUN_LABELS = frozenset({"gun", "guns", "pistol", "handgun", "firearm", "rifle", "shotgun"})


def weapon_class_ids(names: dict[int, str] | list[str]) -> dict[int, str]:
    items = names.items() if isinstance(names, dict) else enumerate(names)
    selected: dict[int, str] = {}
    for index, raw in items:
        label = str(raw).strip().lower().replace("-", " ").replace("_", " ")
        if label in KNIFE_LABELS:
            selected[int(index)] = "KNIFE"
        elif label in GUN_LABELS:
            selected[int(index)] = "GUN"
    if not {"KNIFE", "GUN"}.issubset(set(selected.values())):
        raise ValueError("Weapon checkpoint must explicitly include knife AND gun classes")
    return selected


class WeaponDetector:
    def __init__(self, checkpoint: str, device=None, confidence: float = 0.5):
        if not (math.isfinite(float(confidence)) and 0.0 < confidence <= 1.0):
            raise ValueError("WEAPON_CONFIDENCE must be in (0, 1]")
        path = Path(checkpoint)
        if not path.is_file():
            raise FileNotFoundError(f"Weapon checkpoint not found: {path}")
        from ultralytics import YOLO
        from perception.device import resolve_cuda_device
        self.model = YOLO(str(path))
        self.categories = weapon_class_ids(self.model.names)
        self.device = resolve_cuda_device(device)
        self.confidence = float(confidence)

    def detect(self, frame) -> list[dict]:
        height, width = frame.shape[:2]
        results = self.model.predict(
            frame, conf=self.confidence, classes=list(self.categories),
            imgsz=640, device=self.device, verbose=False,
        )
        weapons: list[dict] = []
        for result in results[:1]:
            boxes = result.boxes
            if boxes is None:
                continue
            for klass, score, xyxy in zip(
                boxes.cls.cpu().tolist(),
                boxes.conf.cpu().tolist(),
                boxes.xyxy.cpu().tolist(),
            ):
                category = self.categories.get(int(klass))
                if category is None or not math.isfinite(float(score)):
                    continue
                x1, y1, x2, y2 = [float(x) for x in xyxy]
                if not all(math.isfinite(x) for x in (x1, y1, x2, y2)):
                    continue
                clipped = [max(0., min(float(width), x1)),
                           max(0., min(float(height), y1)),
                           max(0., min(float(width), x2)),
                           max(0., min(float(height), y2))]
                if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
                    continue
                weapons.append({
                    "id": None, "cls": -1, "box": clipped,
                    "label": category, "score": float(score),
                    "danger": True, "visual_state": "danger",
                    "inference_status": "ok", "action_source": "weapon",
                })
        return weapons
