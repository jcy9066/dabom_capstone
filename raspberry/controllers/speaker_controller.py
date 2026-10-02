from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class SpeakerControllerError(RuntimeError):
    pass


class SpeakerController:
    """Real TTS output for the Seeed Grove - Speaker Plus [101020853].

    The hardware is treated as an amplified speaker connected to the Pi audio
    output. Speech is synthesized to WAV with espeak-ng/espeak and played
    through ALSA. There is deliberately no print-only fallback.
    """

    def __init__(self) -> None:
        self.enabled = os.getenv("SPEAKER_ENABLED", "true").strip().lower() in {
            "1", "true", "yes", "on"
        }
        self.voice = os.getenv("SPEAKER_TTS_VOICE", "ko").strip() or "ko"
        self.rate = int(os.getenv("SPEAKER_TTS_RATE", "155"))
        self.device = os.getenv("SPEAKER_ALSA_DEVICE", "default").strip() or "default"
        self._engine = shutil.which("espeak-ng") or shutil.which("espeak")
        self._aplay = shutil.which("aplay")

        if self.enabled and self._engine is None:
            raise SpeakerControllerError("espeak-ng/espeak is required for TTS output")
        if self.enabled and self._aplay is None:
            raise SpeakerControllerError("aplay is required for Grove Speaker Plus output")

    def speak(self, text: str) -> None:
        message = " ".join(str(text or "").split()).strip()
        if not message:
            return
        if not self.enabled:
            raise SpeakerControllerError("speaker output is disabled")
        if len(message) > 300:
            message = message[:300]

        wav_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="dabom-tts-", suffix=".wav", delete=False) as handle:
                wav_path = Path(handle.name)

            synth = subprocess.run(
                [
                    self._engine,
                    "-v", self.voice,
                    "-s", str(self.rate),
                    "-w", str(wav_path),
                    message,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if synth.returncode != 0:
                raise SpeakerControllerError(
                    f"TTS synthesis failed: {(synth.stderr or '').strip() or synth.returncode}"
                )

            play = subprocess.run(
                [self._aplay, "-q", "-D", self.device, str(wav_path)],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if play.returncode != 0:
                raise SpeakerControllerError(
                    f"speaker playback failed: {(play.stderr or '').strip() or play.returncode}"
                )
        except subprocess.TimeoutExpired as exc:
            raise SpeakerControllerError("speaker TTS timed out") from exc
        finally:
            if wav_path is not None:
                try:
                    wav_path.unlink(missing_ok=True)
                except OSError:
                    pass
