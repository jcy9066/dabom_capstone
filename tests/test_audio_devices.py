from pathlib import Path
from tempfile import TemporaryDirectory
import importlib.util


SPEC = importlib.util.spec_from_file_location(
    "audio_devices_test", Path(__file__).resolve().parents[1] / "raspberry" / "audio_devices.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def test_device_lookup_usb_ids_and_capture_output():
    with TemporaryDirectory() as d:
        base = Path(d)
        usb_speaker = base / "usb_jieli"
        usb_microphone = base / "usb_jmtek"
        for name, root, vid, pid in [("card1", usb_speaker, "4c4a", "4155"),
                                      ("card2", usb_microphone, "0c76", "160a")]:
            root.mkdir()
            (root / "idVendor").write_text(vid)
            (root / "idProduct").write_text(pid)
            (root / "sound").mkdir()
            (root / "sound" / name).mkdir()
            (base / name).symlink_to(root / "sound" / name, target_is_directory=True)
        playback = 'card 1: Device [USB Composite Device], device 0: USB Audio [USB Audio]'
        capture = playback + '\ncard 2: Device_1 [USB Audio Device], device 0: USB Audio [USB Audio]'
        assert mod.find_device("4c4a:4155", capture=False, sound_root=base,
                               list_output=playback) == "plughw:1,0"
        assert mod.find_device("0c76:160a", capture=True, sound_root=base,
                               list_output=capture) == "plughw:2,0"
