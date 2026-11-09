"""Resolve USB ALSA cards by USB VID:PID rather than unstable card numbers."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

SPEAKER_USB_ID = "4c4a:4155"  # Jieli USB Composite Device
MICROPHONE_USB_ID = "0c76:160a"  # JMTek USB Audio Device
CARD_LINE = re.compile(r"^card\s+(\d+):.*?device\s+(\d+):", re.IGNORECASE | re.MULTILINE)


def usb_id_for_card(card_number: int, sound_root: Path = Path("/sys/class/sound")) -> str | None:
    card = sound_root / f"card{card_number}"
    if not card.exists():
        return None
    path = card.resolve()
    for ancestor in (path, *path.parents):
        vendor = ancestor / "idVendor"
        product = ancestor / "idProduct"
        if vendor.is_file() and product.is_file():
            return f"{vendor.read_text().strip().lower()}:{product.read_text().strip().lower()}"
    return None


def parse_device_cards(output: str) -> set[tuple[int, int]]:
    return {(int(card), int(device)) for card, device in CARD_LINE.findall(output)}


def find_device(
    usb_id: str,
    *,
    capture: bool,
    sound_root: Path = Path("/sys/class/sound"),
    list_output: str | None = None,
) -> str:
    if list_output is None:
        binary = "arecord" if capture else "aplay"
        result = subprocess.run([binary, "-l"], capture_output=True, text=True, check=True)
        list_output = result.stdout
    candidates = sorted(parse_device_cards(list_output))
    for card, device in candidates:
        if usb_id_for_card(card, sound_root) == usb_id.lower():
            return f"plughw:{card},{device}"
    direction = "입력" if capture else "출력"
    raise RuntimeError(f"USB 오디오 {direction} 장치 {usb_id}를 찾지 못했습니다. arecord/aplay -l과 USB 연결을 확인하세요.")
