from pathlib import Path

import numpy as np
import pytest

from perception.models.detector_rtmo import RTMOPoseDetector


ROOT_DIR = Path(__file__).resolve().parents[1]


class FakeTracker:
    def __init__(self):
        self.reset_called = False

    def reset(self):
        self.reset_called = True


def build_detector():
    detector = RTMOPoseDetector.__new__(RTMOPoseDetector)
    detector.score_threshold = 0.05
    detector.tracker = FakeTracker()
    return detector


def test_rtmo_postprocess_keeps_17_keypoints_and_filters_low_score():
    detector = build_detector()
    det = np.zeros((1, 3, 5), dtype=np.float32)
    det[0, 0] = [10, 20, 110, 220, 0.92]
    det[0, 1] = [200, 30, 300, 230, 0.005]
    det[0, 2] = [400, 40, 500, 240, 0.60]

    pose = np.zeros((1, 3, 17, 3), dtype=np.float32)
    pose[0, :, :, 2] = 0.9

    boxes, scores, keypoints, keypoint_scores = detector._postprocess_outputs(
        det,
        pose,
        ratio=1.0,
    )

    assert boxes.shape == (2, 4)
    assert scores.shape == (2,)
    assert keypoints.shape == (2, 17, 2)
    assert keypoint_scores.shape == (2, 17)
    np.testing.assert_allclose(
        np.sort(scores),
        np.array([0.60, 0.92], dtype=np.float32),
        atol=1e-6,
    )


def test_rtmo_postprocess_preserves_overlapping_end2end_instances():
    detector = build_detector()
    det = np.zeros((1, 3, 5), dtype=np.float32)
    det[0, 0] = [0, 0, 100, 200, 0.95]
    det[0, 1] = [2, 2, 102, 202, 0.80]
    det[0, 2] = [250, 0, 350, 200, 0.75]
    pose = np.zeros((1, 3, 17, 3), dtype=np.float32)
    pose[0, :, :, 2] = 0.9

    boxes, scores, keypoints, _ = detector._postprocess_outputs(
        det,
        pose,
        ratio=1.0,
    )

    # end2end.onnx has already applied model NMS. A second NMS here would
    # incorrectly remove one of the first two heavily-overlapping people.
    assert boxes.shape == (3, 4)
    assert scores.shape == (3,)
    assert keypoints.shape == (3, 17, 2)
    assert np.allclose(boxes[0], [0, 0, 100, 200])
    assert np.allclose(boxes[1], [2, 2, 102, 202])


def test_rtmo_tracks_map_back_to_correct_poses_by_global_iou():
    detector = build_detector()
    pose_boxes = np.array(
        [
            [0, 0, 100, 200],
            [200, 0, 300, 200],
            [400, 0, 500, 200],
        ],
        dtype=np.float32,
    )
    tracked_boxes = np.array(
        [
            [398, 2, 502, 198],
            [198, 2, 302, 198],
            [1, 1, 99, 199],
        ],
        dtype=np.float32,
    )

    matched = detector._associate_pose_indices(
        tracked_boxes,
        pose_boxes,
    )

    assert matched == {0: 2, 1: 1, 2: 0}


def test_rtmo_global_iou_assignment_is_one_to_one_when_people_overlap():
    detector = build_detector()
    pose_boxes = np.array(
        [
            [0, 0, 100, 200],
            [50, 0, 150, 200],
        ],
        dtype=np.float32,
    )
    tracked_boxes = np.array(
        [
            [40, 0, 140, 200],
            [5, 0, 105, 200],
        ],
        dtype=np.float32,
    )

    matched = detector._associate_pose_indices(
        tracked_boxes,
        pose_boxes,
    )

    assert set(matched.keys()) == {0, 1}
    assert set(matched.values()) == {0, 1}


def test_rtmo_low_confidence_keypoints_are_hidden_from_overlay():
    detector = build_detector()
    keypoints = np.ones((17, 2), dtype=np.float32) * 50
    scores = np.ones(17, dtype=np.float32)
    scores[[0, 4, 9]] = 0.1

    visible = detector._visible_keypoints(keypoints, scores)

    assert tuple(visible[0]) == (0.0, 0.0)
    assert tuple(visible[4]) == (0.0, 0.0)
    assert tuple(visible[9]) == (0.0, 0.0)
    assert tuple(visible[1]) == (50.0, 50.0)


def test_rtmo_detector_resets_botsort_state():
    detector = build_detector()

    detector.reset_tracking_state()

    assert detector.tracker.reset_called is True


def test_pipeline8_factory_uses_rtmo_onnx_botsort_and_x3d():
    source = (ROOT_DIR / "perception" / "pipeline_factory.py").read_text(
        encoding="utf-8"
    )
    start = source.index('    if choice == "8":')
    end = source.index('    if choice == "9":', start)
    block = source[start:end]

    assert "RTMOPoseDetector" in block
    assert 'onnx_model=assets["rtmo_onnx"]' in block
    assert 'tracker="perception/config/botsort_recall.yaml"' in block
    assert "SceneViolenceRecognizer" in block
    assert "YOLOPoseDetector" not in block
    assert "mmpose" not in block.lower()


def test_pipeline8_runtime_does_not_require_mmpose():
    requirements = (ROOT_DIR / "requirements.txt").read_text(encoding="utf-8")
    detector = (
        ROOT_DIR / "perception" / "models" / "detector_rtmo.py"
    ).read_text(encoding="utf-8")

    assert "onnxruntime-gpu" in requirements
    assert "mmpose @" not in requirements
    assert "import onnxruntime as ort" in detector
    assert "from mmpose" not in detector


def test_dashboard_labels_rtmo_m_pipeline():
    source = (ROOT_DIR / "frontend" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "실시간 카메라 (RTMO-M + BotSORT + X3D-M)" in source


def test_rtmo_preprocess_preserves_bgr_and_uses_114_letterbox():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:, :, 0] = 10
    frame[:, :, 1] = 20
    frame[:, :, 2] = 30

    tensor, ratio = RTMOPoseDetector._preprocess(frame)

    assert tensor.shape == (1, 3, 640, 640)
    assert tensor.dtype == np.float32
    assert ratio == 1.0
    assert tensor[0, :, 100, 100].tolist() == [10.0, 20.0, 30.0]
    assert tensor[0, :, 600, 100].tolist() == [114.0, 114.0, 114.0]


def test_rtmo_detector_does_not_apply_second_nms_after_end2end_export():
    source = (
        ROOT_DIR / "perception" / "models" / "detector_rtmo.py"
    ).read_text(encoding="utf-8")

    assert "_nms_keep" not in source
    assert "np.flatnonzero(scores >= self.score_threshold)" in source
    assert "end2end graph already applies" in source



def test_rtmo_pose_dedup_collapses_same_person_duplicate_candidates():
    detector = build_detector()

    boxes = np.array(
        [
            [210, 145, 313, 422],
            [180, 146, 377, 424],
            [111, 145, 320, 428],
        ],
        dtype=np.float32,
    )
    scores = np.array([0.95, 0.80, 0.65], dtype=np.float32)

    base_pose = np.stack(
        [
            np.linspace(245, 285, 17),
            np.linspace(170, 395, 17),
        ],
        axis=1,
    ).astype(np.float32)

    keypoints = np.stack(
        [
            base_pose,
            base_pose + 1.0,
            base_pose - 1.0,
        ],
        axis=0,
    )

    keypoint_scores = np.full(
        (3, 17),
        0.9,
        dtype=np.float32,
    )

    kept = detector._deduplicate_pose_candidates(
        boxes,
        scores,
        keypoints,
        keypoint_scores,
    )

    assert kept[0].shape == (1, 4)
    assert kept[1].shape == (1,)
    assert kept[2].shape == (1, 17, 2)
    assert kept[3].shape == (1, 17)
    assert float(kept[1][0]) == pytest.approx(0.95)


def test_rtmo_pose_dedup_preserves_overlapping_distinct_people():
    detector = build_detector()

    boxes = np.array(
        [
            [100, 100, 300, 420],
            [160, 100, 360, 420],
        ],
        dtype=np.float32,
    )
    scores = np.array([0.92, 0.88], dtype=np.float32)

    pose_a = np.stack(
        [
            np.linspace(150, 250, 17),
            np.linspace(140, 390, 17),
        ],
        axis=1,
    ).astype(np.float32)

    pose_b = pose_a.copy()
    pose_b[:, 0] += 60.0

    keypoints = np.stack([pose_a, pose_b], axis=0)
    keypoint_scores = np.full((2, 17), 0.9, dtype=np.float32)

    kept = detector._deduplicate_pose_candidates(
        boxes,
        scores,
        keypoints,
        keypoint_scores,
    )

    assert kept[0].shape == (2, 4)
    assert kept[1].shape == (2,)


def test_rtmo_track_applies_pose_dedup_before_botsort():
    source = (
        ROOT_DIR / "perception" / "models" / "detector_rtmo.py"
    ).read_text(encoding="utf-8")

    pose_call = source.index("self._pose_predictions(frame)")
    dedup_call = source.index(
        "self._deduplicate_pose_candidates(",
        pose_call,
    )
    tracker_call = source.index(
        "self.tracker.update(detections, frame)",
        dedup_call,
    )

    assert pose_call < dedup_call < tracker_call
