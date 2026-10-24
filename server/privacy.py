"""Fail-closed privacy processing for camera-derived files."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

import cv2
import numpy as np


class PrivacyProcessingError(RuntimeError):
    """Raised when visual data cannot be made safe for persistence."""


class PrivacyProcessor:
    """Detect faces on persistence frames and mosaic only those regions.

    OpenCV's lightweight Haar detector is intentionally independent from the
    patrol inference pipeline and runs on CPU only when a file is being saved.
    """

    def __init__(self, detector: Any | None = None, mosaic_blocks: int = 10) -> None:
        self.mosaic_blocks = max(2, int(mosaic_blocks))
        self._process_lock = threading.Lock()
        if detector is None:
            cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
            detector = cv2.CascadeClassifier(str(cascade_path))
            if detector.empty():
                raise PrivacyProcessingError("Face detector could not be loaded.")
        self.detector = detector

    def process(self, frame: np.ndarray) -> np.ndarray:
        if not isinstance(frame, np.ndarray) or frame.ndim not in (2, 3) or frame.size == 0:
            raise PrivacyProcessingError("A non-empty image frame is required.")
        try:
            with self._process_lock:
                output = frame.copy()
                gray = (
                    output
                    if output.ndim == 2
                    else cv2.cvtColor(output, cv2.COLOR_BGR2GRAY)
                )
                faces = self.detector.detectMultiScale(
                    gray,
                    scaleFactor=1.1,
                    minNeighbors=5,
                    minSize=(20, 20),
                )
            height, width = output.shape[:2]
            for face in faces:
                x, y, face_width, face_height = (int(value) for value in face)
                x1, y1 = max(0, x), max(0, y)
                x2, y2 = min(width, x + face_width), min(height, y + face_height)
                if x2 <= x1 or y2 <= y1:
                    continue
                region = output[y1:y2, x1:x2]
                small_width = max(1, min(self.mosaic_blocks, x2 - x1))
                small_height = max(1, min(self.mosaic_blocks, y2 - y1))
                reduced = cv2.resize(
                    region,
                    (small_width, small_height),
                    interpolation=cv2.INTER_LINEAR,
                )
                output[y1:y2, x1:x2] = cv2.resize(
                    reduced,
                    (x2 - x1, y2 - y1),
                    interpolation=cv2.INTER_NEAREST,
                )
            return output
        except PrivacyProcessingError:
            raise
        except Exception as exc:
            raise PrivacyProcessingError("Face privacy processing failed.") from exc

    def save_image(self, path: str | Path, frame: np.ndarray) -> Path:
        target = Path(path)
        temporary: Path | None = None
        try:
            private_frame = self.process(frame)
            suffix = target.suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                raise PrivacyProcessingError("Unsupported private image format.")
            ok, encoded = cv2.imencode(suffix, private_frame)
            if not ok:
                raise PrivacyProcessingError("Private image encoding failed.")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.stem}.{uuid4().hex}.tmp{suffix}")
            temporary.write_bytes(encoded.tobytes())
            os.replace(temporary, target)
            return target
        except PrivacyProcessingError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        except Exception as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise PrivacyProcessingError("Private image persistence failed.") from exc


class PrivateVideoWriter:
    """Writes only mosaicked frames and publishes the file atomically."""

    def __init__(
        self,
        path: str | Path,
        *,
        fps: float,
        frame_size: tuple[int, int],
        processor: PrivacyProcessor | None = None,
        fourcc: str = "mp4v",
    ) -> None:
        self.target = Path(path)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.temporary = self.target.with_name(
            f".{self.target.stem}.{uuid4().hex}.privacy{self.target.suffix}"
        )
        self.closed = False
        try:
            self.processor = processor or PrivacyProcessor()
            self.writer = cv2.VideoWriter(
                str(self.temporary),
                cv2.VideoWriter_fourcc(*fourcc),
                float(fps),
                frame_size,
            )
            if not self.writer.isOpened():
                self.writer.release()
                raise PrivacyProcessingError("Private video writer could not be opened.")
        except PrivacyProcessingError:
            self.temporary.unlink(missing_ok=True)
            self.target.unlink(missing_ok=True)
            raise
        except Exception as exc:
            self.temporary.unlink(missing_ok=True)
            self.target.unlink(missing_ok=True)
            raise PrivacyProcessingError("Private video writer could not be opened.") from exc

    def write(self, frame: np.ndarray) -> None:
        if self.closed:
            raise PrivacyProcessingError("Private video writer is closed.")
        try:
            self.writer.write(self.processor.process(frame))
        except Exception as exc:
            self.abort()
            if isinstance(exc, PrivacyProcessingError):
                raise
            raise PrivacyProcessingError("Private video frame persistence failed.") from exc

    def close(self) -> Path:
        if self.closed:
            raise PrivacyProcessingError("Private video writer is already closed.")
        self.writer.release()
        self.closed = True
        try:
            os.replace(self.temporary, self.target)
        except Exception as exc:
            self.temporary.unlink(missing_ok=True)
            self.target.unlink(missing_ok=True)
            raise PrivacyProcessingError("Private video could not be published.") from exc
        return self.target

    def abort(self) -> None:
        if not self.closed:
            self.writer.release()
            self.closed = True
        self.temporary.unlink(missing_ok=True)
        self.target.unlink(missing_ok=True)
