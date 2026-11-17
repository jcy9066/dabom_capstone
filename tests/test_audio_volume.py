"""Check ALSA reading and writes without touching a real USB device."""
import sys
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry"))
from audio_intercom_client import AudioClient

ALSA_OUTPUT = """Simple mixer control 'PCM',0
  Front Left: Playback 74 [50%] [-12.3dB] [on]
  Front Right: Playback 74 [50%] [-12.3dB] [on]
"""


def test_usb_volume_reads_reported_hardware_value():
    client = AudioClient("plughw:1,0", "plughw:2,0")
    with patch("audio_intercom_client.subprocess.run", return_value=CompletedProcess([], 0, ALSA_OUTPUT)) as run:
        assert client._speaker_volume() == 50
        run.assert_called_once_with(["amixer", "-c", "2", "sget", "PCM"], text=True,
                                    capture_output=True, timeout=3, check=True)


def test_usb_volume_sets_and_reports_actual_rounded_value():
    client = AudioClient("plughw:1,0", "plughw:2,0")
    with patch("audio_intercom_client.subprocess.run", return_value=CompletedProcess([], 0, ALSA_OUTPUT)) as run:
        assert client._speaker_volume(51) == 50
        run.assert_called_once_with(["amixer", "-c", "2", "sset", "PCM", "51%"], text=True,
                                    capture_output=True, timeout=3, check=True)
