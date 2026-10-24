from pathlib import Path

import cv2
import numpy as np
import pytest

from server.privacy import PrivateVideoWriter, PrivacyProcessingError, PrivacyProcessor


class FixedFaceDetector:
    def detectMultiScale(self, *_args, **_kwargs):
        return np.array([[8, 8, 32, 32]])


class FailingDetector:
    def detectMultiScale(self, *_args, **_kwargs):
        raise RuntimeError("detector failed")


def synthetic_face_frame():
    frame = np.zeros((48, 48, 3), dtype=np.uint8)
    for row in range(8, 40):
        for column in range(8, 40):
            frame[row, column] = (row * 5 % 255, column * 5 % 255, (row + column) * 3 % 255)
    return frame


def test_face_bbox_is_mosaicked_without_changing_live_input():
    frame = synthetic_face_frame()
    original = frame.copy()
    private = PrivacyProcessor(FixedFaceDetector(), mosaic_blocks=4).process(frame)
    assert np.array_equal(frame, original)
    assert np.array_equal(private[:8], original[:8])
    assert not np.array_equal(private[8:40, 8:40], original[8:40, 8:40])
    assert len(np.unique(private[8:40, 8:40].reshape(-1, 3), axis=0)) < len(
        np.unique(original[8:40, 8:40].reshape(-1, 3), axis=0)
    )


def test_saved_image_contains_only_privacy_processed_pixels(tmp_path: Path):
    raw = synthetic_face_frame()
    target = tmp_path / "event.jpg"
    PrivacyProcessor(FixedFaceDetector(), mosaic_blocks=4).save_image(target, raw)
    assert target.exists()
    assert list(tmp_path.iterdir()) == [target]
    saved = cv2.imread(str(target))
    assert saved is not None
    assert not np.array_equal(saved[8:40, 8:40], raw[8:40, 8:40])


def test_privacy_failure_does_not_create_raw_file(tmp_path: Path):
    target = tmp_path / "snapshot.jpg"
    target.write_bytes(b"pre-existing-raw-data")
    processor = PrivacyProcessor(FailingDetector())
    with pytest.raises(PrivacyProcessingError):
        processor.save_image(target, synthetic_face_frame())
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


class FakeVideoBackend:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.write_bytes(b"private-video-container")
        self.frames = []
        self.released = False

    def isOpened(self):
        return True

    def write(self, frame):
        self.frames.append(frame.copy())

    def release(self):
        self.released = True


def test_video_writer_persists_only_mosaicked_frames(tmp_path: Path, monkeypatch):
    backends = []

    def create_backend(path, *_args):
        backend = FakeVideoBackend(path)
        backends.append(backend)
        return backend

    monkeypatch.setattr("server.privacy.cv2.VideoWriter", create_backend)
    monkeypatch.setattr("server.privacy.cv2.VideoWriter_fourcc", lambda *_args: 0)
    target = tmp_path / "event.mp4"
    raw = synthetic_face_frame()
    writer = PrivateVideoWriter(
        target,
        fps=10,
        frame_size=(48, 48),
        processor=PrivacyProcessor(FixedFaceDetector(), mosaic_blocks=4),
    )

    writer.write(raw)
    assert writer.close() == target

    assert target.exists()
    assert backends[0].released
    assert len(backends[0].frames) == 1
    assert not np.array_equal(backends[0].frames[0][8:40, 8:40], raw[8:40, 8:40])
    assert list(tmp_path.iterdir()) == [target]


def test_video_privacy_failure_removes_temporary_and_target(tmp_path: Path, monkeypatch):
    backends = []

    def create_backend(path, *_args):
        backend = FakeVideoBackend(path)
        backends.append(backend)
        return backend

    monkeypatch.setattr("server.privacy.cv2.VideoWriter", create_backend)
    monkeypatch.setattr("server.privacy.cv2.VideoWriter_fourcc", lambda *_args: 0)
    target = tmp_path / "event.mp4"
    target.write_bytes(b"pre-existing-raw-video")
    writer = PrivateVideoWriter(
        target,
        fps=10,
        frame_size=(48, 48),
        processor=PrivacyProcessor(FailingDetector()),
    )

    with pytest.raises(PrivacyProcessingError):
        writer.write(synthetic_face_frame())

    assert backends[0].released
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
