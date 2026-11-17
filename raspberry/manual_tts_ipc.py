"""Local, user-owned Unix socket for fixed manual announcement WAV playback.

Only the existing USB audio-intercom process opens ALSA. The robot command
client sends a fixed kind over this private local socket, never free text.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import wave
from collections.abc import Awaitable, Callable
from pathlib import Path

KINDS = ("warning", "report")
WAV_FILES = {"warning": "alert_warning.wav", "report": "alert_report.wav"}


def socket_path() -> Path:
    return Path(os.environ.get("DABOM_TTS_IPC_PATH") or f"/tmp/dabom-tts-{os.getuid()}.sock")


def wav_path(kind: str, root: Path) -> Path:
    if kind not in KINDS:
        raise ValueError("invalid manual alert kind")
    return root / "raspberry" / "audio" / WAV_FILES[kind]


def verify_wav(kind: str, root: Path) -> Path:
    path = wav_path(kind, root)
    if not path.is_file():
        raise RuntimeError(f"TTS WAV missing: {path}")
    try:
        with wave.open(str(path), "rb") as reader:
            if reader.getnchannels() not in (1, 2) or reader.getframerate() < 8000 or reader.getnframes() == 0:
                raise RuntimeError("TTS WAV is empty or invalid")
    except (wave.Error, EOFError, OSError) as exc:
        raise RuntimeError("invalid TTS WAV") from exc
    return path


async def request_tts(kind: str, *, check_only: bool = False, timeout_sec: float = 30) -> None:
    if kind not in KINDS:
        raise ValueError("invalid manual alert kind")
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(socket_path())), timeout=2)
    try:
        writer.write((json.dumps({"kind": kind, "check_only": check_only}) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=3 if check_only else timeout_sec)
        if not line or len(line) > 1024:
            raise RuntimeError("TTS client did not return a valid completion response")
        result = json.loads(line)
        if result.get("ok") is not True:
            raise RuntimeError(str(result.get("error") or "TTS playback failed")[:200])
    finally:
        writer.close()
        await writer.wait_closed()


class ManualTtsServer:
    """Single-playback TTS service; refuse overlapping playback."""

    def __init__(self, *, root: Path, playback: Callable[[Path], Awaitable[None]], path: Path | None = None):
        self.root = root
        self.playback = playback
        self.path = path or socket_path()
        self.server: asyncio.AbstractServer | None = None
        self._lock = asyncio.Lock()
        self._inode: int | None = None

    async def start(self) -> None:
        if self.path.exists() or self.path.is_symlink():
            meta = self.path.lstat()
            if not stat.S_ISSOCK(meta.st_mode) or meta.st_uid != os.getuid():
                raise RuntimeError("unsafe TTS socket path; refusing to replace it")
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self.path)), timeout=0.3)
            except (OSError, asyncio.TimeoutError, ConnectionError):
                self.path.unlink()  # stale socket owned by this UID
            else:
                writer.close()
                await writer.wait_closed()
                raise RuntimeError("another TTS audio process is already running")
        self.server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        os.chmod(self.path, 0o600)
        self._inode = self.path.stat().st_ino

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        if self._inode is not None:
            try:
                meta = self.path.lstat()
                if stat.S_ISSOCK(meta.st_mode) and meta.st_ino == self._inode and meta.st_uid == os.getuid():
                    self.path.unlink()
            except FileNotFoundError:
                pass
            self._inode = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=3)
            if len(raw) > 256:
                raise RuntimeError("TTS request too long")
            command = json.loads(raw)
            if not isinstance(command, dict) or command.get("kind") not in KINDS or type(command.get("check_only")) is not bool:
                raise RuntimeError("invalid TTS request")
            kind = command["kind"]
            if self._lock.locked():
                raise RuntimeError("TTS playback busy")
            if command["check_only"]:
                verify_wav(kind, self.root)
            else:
                async with self._lock:
                    await self.playback(verify_wav(kind, self.root))
            result = {"ok": True}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)[:180]}
        try:
            writer.write((json.dumps(result, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
