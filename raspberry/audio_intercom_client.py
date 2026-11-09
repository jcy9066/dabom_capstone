"""Opt-in, independently launched DABOM Pi voice intercom client.

Does not interact with ROS, motor control, Pico W, or the existing Pi stack.
Use --check first; then run it separately inside a tmux session.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import signal
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
import websockets

from audio_devices import MICROPHONE_USB_ID, SPEAKER_USB_ID, find_device

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
FRAME_BYTES = 640  # 20 ms at 16 kHz, s16le, mono
logger = logging.getLogger("dabom-audio")


def audio_ws_url(server_url: str, robot_id: str) -> str:
    parts = urlsplit(server_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("SERVER_BASE_URL must be http(s)://hostname[:port]")
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, f"/ws/audio/robot/{robot_id}", "", ""))


class AudioClient:
    def __init__(self, microphone: str, speaker: str):
        self.microphone = microphone
        self.speaker = speaker
        self.capture_task: asyncio.Task | None = None
        self.capture_proc = None
        self.playback_proc = None
        self.listen_enabled = False
        self.talk_enabled = False

    async def _capture(self, ws) -> None:
        args = ["arecord", "-q", "-D", self.microphone, "-t", "raw",
                "-f", "S16_LE", "-r", "16000", "-c", "1",
                "-B", "80000", "-F", "20000"]
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        self.capture_proc = proc
        logger.info("현장 마이크 송출 시작: %s", self.microphone)
        try:
            while self.listen_enabled:
                try:
                    pcm = await proc.stdout.readexactly(FRAME_BYTES)
                except asyncio.IncompleteReadError:
                    logger.warning("마이크 캡처가 종료되었습니다 (returncode=%s)", proc.returncode)
                    break
                await ws.send(pcm)
        finally:
            self.capture_proc = None
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                await asyncio.wait_for(proc.wait(), timeout=2)
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            logger.info("현장 마이크 송출 종료")

    @staticmethod
    def _capture_completed(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.error("마이크 캡처 작업 실패: %s", exception)

    async def set_listening(self, enabled: bool, ws) -> None:
        if enabled == self.listen_enabled:
            return
        self.listen_enabled = enabled
        if enabled:
            self.capture_task = asyncio.create_task(self._capture(ws), name="audio-microphone")
            self.capture_task.add_done_callback(self._capture_completed)
        else:
            if self.capture_task:
                self.capture_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.capture_task
                self.capture_task = None

    async def _open_playback(self):
        if self.playback_proc and self.playback_proc.returncode is None:
            return
        args = ["aplay", "-q", "-D", self.speaker, "-t", "raw",
                "-f", "S16_LE", "-r", "16000", "-c", "1",
                "-B", "80000", "-F", "20000"]
        self.playback_proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        logger.info("로봇 스피커 출력 시작: %s", self.speaker)

    async def stop_playback(self):
        proc, self.playback_proc = self.playback_proc, None
        if proc is None:
            return
        if proc.stdin:
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        logger.info("로봇 스피커 출력 종료")

    async def on_audio(self, frame: bytes):
        if not self.talk_enabled or not 0 < len(frame) <= 4096:
            return
        await self._open_playback()
        try:
            self.playback_proc.stdin.write(frame)
            await asyncio.wait_for(self.playback_proc.stdin.drain(), timeout=0.3)
        except (BrokenPipeError, ConnectionError, asyncio.TimeoutError, AttributeError):
            logger.warning("스피커 출력 오류; 재생 프로세스를 초기화합니다")
            await self.stop_playback()

    def _speaker_card(self) -> int:
        match = re.fullmatch(r"plughw:(\d+),(\d+)", self.speaker)
        if not match:
            raise ValueError("USB 스피커 장치 번호가 유효하지 않습니다.")
        return int(match.group(1))

    def _speaker_volume(self, target: int | None = None) -> int:
        args = ["amixer", "-c", str(self._speaker_card())]
        if target is None:
            args += ["sget", "PCM"]
        else:
            args += ["sset", "PCM", f"{target}%"]
        result = subprocess.run(args, text=True, capture_output=True, timeout=3, check=True)
        # Use ALSA's actual rounded value, not the requested percentage.
        for line in result.stdout.splitlines():
            if "Front Left:" in line and "Playback" in line:
                match = re.search(r"\[(\d{1,3})%\]", line)
                if match:
                    return int(match.group(1))
        raise RuntimeError("USB 스피커의 PCM 음량 값을 확인할 수 없습니다.")

    async def _report_speaker_volume(self, ws, target: int | None = None) -> None:
        try:
            actual = await asyncio.to_thread(self._speaker_volume, target)
            await ws.send(json.dumps({"type": "speaker_volume", "percent": actual}))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            logger.warning("USB 스피커 음량 조회/설정 실패: %s", exc)
            await ws.send(json.dumps({"type": "audio_error", "message": "USB 스피커 음량 제어 실패. Pi의 amixer 설정을 확인하세요."}))

    async def on_message(self, raw, ws):
        if isinstance(raw, bytes):
            await self.on_audio(raw)
            return
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            return
        kind, enabled = message.get("type"), message.get("enabled")
        if kind == "listen" and type(enabled) is bool:
            await self.set_listening(enabled, ws)
        elif kind == "talk" and type(enabled) is bool:
            self.talk_enabled = enabled
            if not enabled:
                await self.stop_playback()
        elif kind == "get_speaker_volume":
            await self._report_speaker_volume(ws)
        elif kind == "set_speaker_volume":
            percent = message.get("percent")
            if type(percent) is int and 0 <= percent <= 100:
                await self._report_speaker_volume(ws, percent)
        elif kind == "state":
            logger.info("오디오 상태: %s", message)
        elif kind == "error":
            logger.warning("오디오 중계 서버: %s", message.get("message"))

    async def cleanup(self):
        self.listen_enabled = False
        self.talk_enabled = False
        if self.capture_task:
            self.capture_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.capture_task
            self.capture_task = None
        await self.stop_playback()

    async def serve(self, url: str, token: str):
        while True:
            try:
                async with websockets.connect(
                    url, extra_headers={"X-Robot-Control-Token": token},
                    max_size=8192, ping_interval=20, ping_timeout=20,
                ) as ws:
                    logger.info("GPU 오디오 중계 서버 연결: %s", url)
                    async for raw in ws:
                        await self.on_message(raw, ws)
            except asyncio.CancelledError:
                raise
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                logger.warning("오디오 연결 실패/종료: %s", exc)
            finally:
                await self.cleanup()
            await asyncio.sleep(2)


def main():
    parser = argparse.ArgumentParser(description="DABOM independent USB voice intercom client")
    parser.add_argument("--check", action="store_true", help="resolve devices without opening them")
    parser.add_argument("--microphone", default=os.getenv("DABOM_AUDIO_MIC_DEVICE"),
                        help="explicit ALSA capture device, e.g. plughw:2,0")
    parser.add_argument("--speaker", default=os.getenv("DABOM_AUDIO_SPEAKER_DEVICE"),
                        help="explicit ALSA playback device, e.g. plughw:1,0")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[dabom-audio] %(message)s")
    try:
        microphone = args.microphone or find_device(MICROPHONE_USB_ID, capture=True)
        speaker = args.speaker or find_device(SPEAKER_USB_ID, capture=False)
    except (RuntimeError, OSError) as exc:
        parser.error(str(exc))
    print(f"[dabom-audio] microphone={microphone} speaker={speaker}", flush=True)
    if args.check:
        return
    server = os.getenv("SERVER_BASE_URL", "")
    robot_id = os.getenv("ROBOT_ID", "")
    token = os.getenv("ROBOT_CONTROL_TOKEN", "")
    if not server or not robot_id or not token:
        parser.error("SERVER_BASE_URL, ROBOT_ID, ROBOT_CONTROL_TOKEN must be set in .env")
    client = AudioClient(microphone, speaker)
    try:
        asyncio.run(client.serve(audio_ws_url(server, robot_id), token))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
