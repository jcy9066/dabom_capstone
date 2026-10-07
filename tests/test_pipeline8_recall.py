import threading
from collections import deque

import numpy as np

from perception.models.scene_x3d import SceneViolenceRecognizer


def build_recognizer(score=0.8):
    recognizer = SceneViolenceRecognizer.__new__(SceneViolenceRecognizer)
    recognizer.device = "cpu"
    recognizer.checkpoint = "unused"
    recognizer.num_frames = 16
    recognizer.frame_size = 224
    recognizer.clip_duration_sec = 4.0
    recognizer.inference_interval_sec = 1.0
    recognizer.threshold = 0.4
    recognizer.normal_clear_hits = 2
    recognizer._danger_latched = False
    recognizer._normal_clear_streak = 0
    recognizer._frames = deque()
    recognizer._buffer_lock = threading.Lock()
    recognizer._inference_lock = threading.Lock()
    recognizer._last_inference_at = None
    recognizer._last_result = recognizer._warming_result(0.0)
    recognizer.model = None
    recognizer._test_score = score
    recognizer._sampled = None

    def predict(frames):
        recognizer._sampled = frames
        return recognizer._test_score

    recognizer._predict_clip = predict
    return recognizer


def feed_four_seconds(recognizer):
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    for index in range(61):
        recognizer.observe_frame(frame, timestamp=index / 15.0)


def test_x3d_scene_uses_16_samples_across_four_second_buffer():
    recognizer = build_recognizer(score=0.81)
    feed_four_seconds(recognizer)

    result = recognizer.classify_scene(now=4.0)

    assert result["status"] == "ok"
    assert result["label"] == "VIOLENCE"
    assert result["danger"] is True
    assert result["score"] == 0.81
    assert result["threshold"] == 0.4
    assert result["fresh"] is True
    assert len(recognizer._sampled) == 16


def test_x3d_scene_inference_is_throttled_to_one_hz():
    recognizer = build_recognizer(score=0.7)
    feed_four_seconds(recognizer)

    first = recognizer.classify_scene(now=4.0)
    second = recognizer.classify_scene(now=4.5)
    third = recognizer.classify_scene(now=5.1)

    assert first["fresh"] is True
    assert second["fresh"] is False
    assert third["fresh"] is True


def test_x3d_scene_danger_overlay_marks_scene_not_people():
    recognizer = build_recognizer(score=0.9)
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    result = {
        "status": "ok",
        "label": "VIOLENCE",
        "score": 0.9,
        "danger": True,
    }

    recognizer.draw_scene_overlay(frame, result)

    assert tuple(frame[2, 2]) == (0, 0, 255)
    assert np.count_nonzero(frame) > 0


def test_x3d_scene_normal_overlay_has_no_red_danger_border():
    recognizer = build_recognizer(score=0.1)
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    result = {
        "status": "ok",
        "label": "NORMAL",
        "score": 0.1,
        "danger": False,
    }

    recognizer.draw_scene_overlay(frame, result)

    assert tuple(frame[2, 2]) != (0, 0, 255)


def test_x3d_danger_clears_only_after_consecutive_normal_results():
    recognizer = build_recognizer(score=0.8)
    feed_four_seconds(recognizer)

    danger = recognizer.classify_scene(now=4.0)
    recognizer._test_score = 0.2
    held = recognizer.classify_scene(now=5.1)
    cleared = recognizer.classify_scene(now=6.2)

    assert danger["danger"] is True
    assert danger["raw_danger"] is True
    assert held["danger"] is True
    assert held["raw_danger"] is False
    assert held["normal_clear_streak"] == 1
    assert cleared["danger"] is False
    assert cleared["label"] == "NORMAL"


def test_x3d_inference_error_is_unavailable_not_normal():
    recognizer = build_recognizer(score=0.8)
    feed_four_seconds(recognizer)
    recognizer.classify_scene(now=4.0)

    def fail(_frames):
        raise RuntimeError("synthetic x3d failure")

    recognizer._predict_clip = fail
    result = recognizer.classify_scene(now=5.1)

    assert result["status"] == "inference_error"
    assert result["available"] is False
    assert result["label"] is None
    assert result["danger"] is True
    assert "synthetic x3d failure" in result["error"]


def test_x3d_warming_overlay_is_visibly_distinct():
    recognizer = build_recognizer(score=0.1)
    frame = np.zeros((120, 240, 3), dtype=np.uint8)

    recognizer.draw_scene_overlay(
        frame,
        recognizer._warming_result(1.5),
    )

    assert np.count_nonzero(frame) > 0
    assert tuple(frame[2, 2]) != (0, 0, 255)
