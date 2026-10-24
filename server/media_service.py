"""Privacy-safe image persistence and memory-only frozen frame tokens."""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from server.privacy import PrivacyProcessor


IMAGE_CATEGORIES = frozenset({"events", "actions"})


class UnsafeMediaPathError(ValueError):
    """Raised when a media path escapes the configured private gallery."""


class FrozenFrameTokenError(ValueError):
    """Raised when a frozen frame token cannot be used by the requester."""


@dataclass(frozen=True)
class FrozenFrame:
    frame: np.ndarray
    frame_sequence: int | None


@dataclass(frozen=True)
class _FrozenFrameEntry:
    frame: np.ndarray
    frame_sequence: int | None
    user_id: str
    session_id: str
    expires_at: float


def _contained(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


class PrivateImageStore:
    """Stores only privacy-processed gallery images and returns relative paths."""

    def __init__(
        self,
        *,
        project_root: str | Path = ".",
        gallery_root: str | Path = "received_frames/gallery",
        processor: PrivacyProcessor | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        configured_root = Path(gallery_root)
        self.gallery_root = (
            configured_root.resolve()
            if configured_root.is_absolute()
            else (self.project_root / configured_root).resolve()
        )
        if not _contained(self.gallery_root, self.project_root):
            raise UnsafeMediaPathError("Gallery root must be inside the project root.")
        self.processor = processor or PrivacyProcessor()

    def save_image(self, category: str, frame: np.ndarray) -> str:
        category_root = self._category_root(category)
        filename = f"{secrets.token_hex(16)}.jpg"
        target = category_root / filename
        self.processor.save_image(target, frame)
        return target.relative_to(self.project_root).as_posix()

    def resolve(self, relative_path: str | Path) -> Path:
        supplied = Path(relative_path)
        if supplied.is_absolute():
            raise UnsafeMediaPathError("Absolute media paths are not allowed.")
        candidate = (self.project_root / supplied).resolve()
        if not _contained(candidate, self.gallery_root):
            raise UnsafeMediaPathError("Media path escapes the private gallery.")
        if not any(
            _contained(candidate, self.gallery_root / category)
            for category in IMAGE_CATEGORIES
        ):
            raise UnsafeMediaPathError("Media path has an unsupported category.")
        return candidate

    def delete(self, relative_path: str | Path) -> bool:
        target = self.resolve(relative_path)
        existed = target.is_file()
        target.unlink(missing_ok=True)
        return existed

    remove = delete

    def _category_root(self, category: str) -> Path:
        if category not in IMAGE_CATEGORIES:
            raise ValueError("Unsupported private image category.")
        return self.gallery_root / category


class FrozenFrameCache:
    """Binds immutable, memory-only frame snapshots to a user session and TTL."""

    def __init__(
        self,
        *,
        ttl_sec: float = 30,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_sec <= 0:
            raise ValueError("Frozen frame TTL must be positive.")
        self.ttl_sec = float(ttl_sec)
        self.clock = clock
        self._entries: dict[str, _FrozenFrameEntry] = {}
        self._lock = threading.Lock()

    def store(
        self,
        frame: np.ndarray,
        *,
        user_id: str | int,
        session_id: str,
        frame_sequence: int | None = None,
    ) -> str:
        frozen = self._immutable_copy(frame)
        token = secrets.token_urlsafe(32)
        entry = _FrozenFrameEntry(
            frame=frozen,
            frame_sequence=frame_sequence,
            user_id=str(user_id),
            session_id=str(session_id),
            expires_at=self.clock() + self.ttl_sec,
        )
        with self._lock:
            self._discard_expired_locked()
            while token in self._entries:
                token = secrets.token_urlsafe(32)
            self._entries[token] = entry
        return token

    def get(
        self,
        token: str,
        *,
        user_id: str | int,
        session_id: str,
    ) -> FrozenFrame:
        with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                raise FrozenFrameTokenError("Frozen frame token is invalid or expired.")
            if entry.expires_at <= self.clock():
                self._entries.pop(token, None)
                raise FrozenFrameTokenError("Frozen frame token is invalid or expired.")
            if entry.user_id != str(user_id) or entry.session_id != str(session_id):
                raise FrozenFrameTokenError("Frozen frame token belongs to another session.")
            frame = self._immutable_copy(entry.frame)
            return FrozenFrame(frame=frame, frame_sequence=entry.frame_sequence)

    def invalidate(
        self,
        token: str,
        *,
        user_id: str | int,
        session_id: str,
    ) -> None:
        self.get(token, user_id=user_id, session_id=session_id)
        with self._lock:
            self._entries.pop(token, None)

    def _discard_expired_locked(self) -> None:
        now = self.clock()
        expired = [
            token for token, entry in self._entries.items() if entry.expires_at <= now
        ]
        for token in expired:
            self._entries.pop(token, None)

    @staticmethod
    def _immutable_copy(frame: np.ndarray) -> np.ndarray:
        if not isinstance(frame, np.ndarray) or frame.ndim not in (2, 3) or frame.size == 0:
            raise ValueError("A non-empty image frame is required.")
        frozen = frame.copy()
        frozen.setflags(write=False)
        return frozen
