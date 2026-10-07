import time

import cv2

from .core.trigger import CascadingTrigger
from .env_config import env_float
from .utils.telegram_notifier import TelegramNotifier
from .utils.event_taxonomy import vision_alert_type
from .models.action_policy import is_observation_issue
from .models.violence_heuristic import ViolenceHeuristic


class FrameProcessor:
    @staticmethod
    def _pair_weight(source):
        score = max(0.0, min(1.0, float(source.get("score") or 0.0)))
        danger = bool(source.get("danger") or source.get("is_danger"))
        stale = bool(source.get("observation_stale"))
        if danger:
            base = 100.0 if not stale else 10.0
        else:
            base = 1.0 if not stale else 0.1
        return base + score

    @classmethod
    def _select_non_overlapping_pairs(cls, pair_actions):
        candidates = [
            (tuple(sorted(pair_key)), dict(source))
            for pair_key, source in (pair_actions or {}).items()
            if isinstance(pair_key, tuple)
            and len(pair_key) == 2
            and isinstance(source, dict)
        ]
        candidates.sort(key=lambda item: item[0])
        cache = {}

        def search(index, used_ids):
            key = (index, tuple(sorted(used_ids)))
            if key in cache:
                return cache[key]
            if index >= len(candidates):
                return 0.0, ()

            best_weight, best_items = search(index + 1, used_ids)
            pair_key, source = candidates[index]
            if pair_key[0] not in used_ids and pair_key[1] not in used_ids:
                include_weight, include_items = search(
                    index + 1,
                    used_ids | {pair_key[0], pair_key[1]},
                )
                include_weight += cls._pair_weight(source)
                include_items = (candidates[index],) + include_items
                if (
                    include_weight > best_weight
                    or (
                        include_weight == best_weight
                        and tuple(item[0] for item in include_items)
                        < tuple(item[0] for item in best_items)
                    )
                ):
                    best_weight, best_items = include_weight, include_items
            cache[key] = (best_weight, best_items)
            return cache[key]

        return list(search(0, set())[1])

    def _resolve_pair_actions_for_display(self, pair_actions, now):
        resolved = {}
        normalized = {
            tuple(sorted(pair_key)): dict(source)
            for pair_key, source in (pair_actions or {}).items()
            if isinstance(pair_key, tuple)
            and len(pair_key) == 2
            and isinstance(source, dict)
        }
        for pair_key, source in normalized.items():
            if source.get("observation_stale"):
                cached = self.pair_action_display_buffer.get(pair_key)
                if cached is None:
                    continue
                if now - float(cached.get("updated_at", 0.0)) > self.action_display_ttl_sec:
                    self.pair_action_display_buffer.pop(pair_key, None)
                    continue
                current = {
                    key: value
                    for key, value in cached.items()
                    if key != "updated_at"
                }
                current["observation_stale"] = True
                resolved[pair_key] = current
                continue

            cached = dict(source)
            cached["updated_at"] = now
            self.pair_action_display_buffer[pair_key] = cached
            resolved[pair_key] = source

        for pair_key in list(self.pair_action_display_buffer):
            if pair_key not in normalized:
                self.pair_action_display_buffer.pop(pair_key, None)
        return resolved

    def __init__(self, detector, action_analyzer, notifier=None):
        self.detector = detector
        self.action_analyzer = action_analyzer
        self.trigger = CascadingTrigger()
        self.notifier = notifier if notifier is not None else TelegramNotifier()
        self.action_display_buffer = {}
        self.action_display_updated_at = {}
        self.pair_action_display_buffer = {}
        self.action_display_ttl_sec = env_float("ACTION_DISPLAY_TTL_SEC", minimum=0.1)
        self.violence_heuristic = ViolenceHeuristic()
        if getattr(self.action_analyzer, "restrict_to_target_actions", False):
            self.violence_heuristic.enabled = False

    def _process_scene_classifier(self, frame, tracked_boxes, obj_states):
        analyzer = self.action_analyzer
        analyzer.observe_frame(frame)
        scene_result = analyzer.classify_scene()

        display_frame = frame.copy()
        detections = []
        danger = False

        for obj in tracked_boxes:
            oid = obj["id"]
            cls_id = obj.get("cls", 0)
            state = obj_states.get(oid, 0)
            detection = {
                "id": oid,
                "cls": cls_id,
                "box": [float(v) for v in obj["box"]],
                "state": state,
                "label": "",
                "score": None,
                "danger": False,
            }

            if cls_id == 0:
                skeleton = obj.get("keypoints")
                if skeleton is not None:
                    analyzer.draw_skeleton(
                        display_frame,
                        skeleton,
                        (120, 220, 120),
                    )
            else:
                detection["label"] = "WEAPON"
                detection["danger"] = True
                danger = True

            detections.append(detection)

        danger = danger or bool(scene_result.get("danger"))
        analyzer.draw_scene_overlay(display_frame, scene_result)
        if (
            scene_result.get("status") == "ok"
            and scene_result.get("fresh")
            and bool(scene_result.get("raw_danger"))
        ):
            self.notifier.send_event_alert_async(
                "폭행 상황 감지: VIOLENCE",
                robot_id="local-video",
                event_type=vision_alert_type("VIOLENCE"),
            )

        return {
            "frame": display_frame,
            "detections": detections,
            "danger": danger,
            "scene": scene_result,
        }

    def process(self, frame):
        tracked_boxes = self.detector.track(frame)
        obj_states = self.trigger.get_object_states(tracked_boxes)
        if getattr(self.action_analyzer, "scene_level_classifier", False):
            return self._process_scene_classifier(
                frame,
                tracked_boxes,
                obj_states,
            )

        display_frame = frame.copy()
        detections = []
        danger = False
        analyze_all_persons = bool(
            getattr(self.action_analyzer, "analyze_all_persons", False)
        )
        manages_action_hysteresis = bool(
            getattr(self.action_analyzer, "manages_action_hysteresis", False)
        )
        batch_action_results = {}
        batch_processor = getattr(self.action_analyzer, "process_many", None)
        if analyze_all_persons and callable(batch_processor):
            person_objs = [obj for obj in tracked_boxes if obj.get("cls", 0) == 0]
            batch_action_results = batch_processor(frame, person_objs)

        pair_now = time.monotonic()
        pair_actions = self._resolve_pair_actions_for_display(
            dict(getattr(self.action_analyzer, "latest_pair_actions", {}) or {}),
            pair_now,
        )

        for obj in tracked_boxes:
            oid = obj["id"]
            cls_id = obj.get("cls", 0)
            state = obj_states.get(oid, 0)

            detection = {
                "id": oid,
                "cls": cls_id,
                "box": [float(v) for v in obj["box"]],
                "state": state,
                "label": "",
                "score": None,
                "danger": False,
            }

            should_analyze_person = cls_id == 0 and (state != 0 or analyze_all_persons)
            if state == 0 and not should_analyze_person:
                detections.append(detection)
                continue

            color = (0, 165, 255)
            label = ""
            skeleton = None

            if cls_id == 0:
                if oid in batch_action_results:
                    skeleton, action = batch_action_results[oid]
                else:
                    skeleton, action = self.action_analyzer.process(frame, obj)
                observation_issue = is_observation_issue(action)
                now = time.monotonic()

                if action and not observation_issue:
                    self.action_display_buffer[oid] = action
                    self.action_display_updated_at[oid] = now
                elif manages_action_hysteresis and not observation_issue:
                    self.action_display_buffer.pop(oid, None)
                    self.action_display_updated_at.pop(oid, None)

                updated_at = self.action_display_updated_at.get(oid)
                if (
                    observation_issue
                    and updated_at is not None
                    and now - updated_at > self.action_display_ttl_sec
                ):
                    self.action_display_buffer.pop(oid, None)
                    self.action_display_updated_at.pop(oid, None)

                if oid in self.action_display_buffer:
                    current_action = self.action_display_buffer[oid]
                    detection["label"] = current_action["label"]
                    detection["score"] = float(current_action["score"])
                    detection["danger"] = bool(current_action["is_danger"])

                    if current_action["is_danger"]:
                        danger = True
                        color = (0, 0, 255)
                        label = f"!!! {current_action['label']} !!! {current_action['score'] * 100:.0f}%"
                        if not observation_issue:
                            self.notifier.send_event_alert_async(
                                f"위험 행동 감지: {current_action['label']}",
                                robot_id="local-video",
                                event_type=vision_alert_type(current_action["label"]),
                            )
                    else:
                        label = f"[{current_action['label']}] {current_action['score'] * 100:.0f}%"
            else:
                danger = True
                detection["label"] = "WEAPON"
                detection["danger"] = True
                label = "WEAPON"

            x1, y1, x2, y2 = map(int, obj["box"])
            # Bounding box visualization disabled. Keep this line for easy rollback.
            # cv2.rectangle(display_frame, (x1, y1), (x2, y2), color, 2)

            if skeleton is not None and (state != 0 or oid in self.action_display_buffer):
                self.action_analyzer.draw_skeleton(display_frame, skeleton, color)

            if label:
                cv2.putText(
                    display_frame,
                    label,
                    (x1, y1 - 8),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    color,
                    2,
                )

            detections.append(detection)

        objects_by_id = {obj["id"]: obj for obj in tracked_boxes}
        for pair_key, source in self._select_non_overlapping_pairs(pair_actions):
            first = objects_by_id.get(pair_key[0])
            second = objects_by_id.get(pair_key[1])
            if first is None or second is None:
                continue
            pair_danger = bool(source.get("danger") or source.get("is_danger"))
            pair_color = (0, 0, 255) if pair_danger else (0, 165, 255)
            danger = danger or pair_danger

            for participant in (first, second):
                skeleton = participant.get("keypoints")
                if skeleton is not None:
                    self.action_analyzer.draw_skeleton(
                        display_frame,
                        skeleton,
                        pair_color,
                    )

            first_center = tuple(map(int, first.get("center") or (0, 0)))
            second_center = tuple(map(int, second.get("center") or (0, 0)))
            cv2.line(
                display_frame,
                first_center,
                second_center,
                pair_color,
                2,
                cv2.LINE_AA,
            )
            label = source.get("label") or "INTERACTION"
            score = source.get("score")
            score_text = (
                f" {float(score) * 100:.0f}%"
                if score is not None
                else ""
            )
            badge_center = (
                int((first_center[0] + second_center[0]) / 2),
                int((first_center[1] + second_center[1]) / 2),
            )
            cv2.putText(
                display_frame,
                f"{label}{score_text}",
                badge_center,
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                pair_color,
                2,
                cv2.LINE_AA,
            )

        active_track_ids = {obj["id"] for obj in tracked_boxes}
        cleanup_now = time.monotonic()
        for buffered_id, updated_at in list(self.action_display_updated_at.items()):
            if (
                buffered_id not in active_track_ids
                and cleanup_now - updated_at > self.action_display_ttl_sec
            ):
                self.action_display_buffer.pop(buffered_id, None)
                self.action_display_updated_at.pop(buffered_id, None)

        return {
            "frame": display_frame,
            "detections": detections,
            "pair_actions": [
                {"pair_ids": list(pair_key), **dict(source)}
                for pair_key, source in self._select_non_overlapping_pairs(pair_actions)
            ],
            "danger": danger,
        }
