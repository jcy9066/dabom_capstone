"""Manual warning/report hardware-free contract and local IPC regression tests."""
import asyncio
import sys
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "raspberry") not in sys.path:
    sys.path.insert(0, str(ROOT / "raspberry"))

from manual_alert_engine import ManualAlertEngine, AlertBusyError
from manual_tts_ipc import ManualTtsServer, request_tts, verify_wav


class ManualAlertBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_warning_three_beeps_with_led_aligned_through_tts(self):
        events = []
        release = asyncio.Event()
        entered = asyncio.Event()

        async def beep(duration): events.append(("beep", duration))
        async def led(enabled): events.append(("led", enabled))
        async def tts(kind):
            events.append(("tts", kind))
            entered.set()
            await release.wait()

        eng = ManualAlertEngine(beep=beep, led=led, tts=tts, half_period_sec=0.002)
        t = eng.start("warning")
        with self.assertRaises(AlertBusyError):
            eng.start("report")
        await asyncio.wait_for(entered.wait(), timeout=2)
        self.assertEqual([e for e in events if e[0] == "beep"], [("beep", 2)] * 3)
        self.assertTrue(any(e == ("led", True) for e in events))
        release.set()
        await t
        self.assertEqual(events[-1], ("led", False))

    async def test_report_no_led_and_tts_after_two_beeps(self):
        events = []
        async def beep(duration): events.append(("beep", duration))
        async def led(enabled): events.append(("led", enabled))
        async def tts(kind): events.append(("tts", kind))
        eng = ManualAlertEngine(beep=beep, led=led, tts=tts, half_period_sec=0.001)
        await eng.start("report")
        self.assertEqual(events, [("beep", 1), ("beep", 1), ("tts", "report")])

    async def test_led_off_on_tts_failure_and_cancel(self):
        states = []
        async def beep(_): pass
        async def led(enabled): states.append(enabled)
        async def broken(_): raise RuntimeError("audio failed")
        eng = ManualAlertEngine(beep=beep, led=led, tts=broken, half_period_sec=0.001)
        with self.assertRaisesRegex(RuntimeError, "audio failed"):
            await eng.start("warning")
        self.assertFalse(states[-1])


class IpcTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dir = self.root / "raspberry" / "audio"
        self.dir.mkdir(parents=True)
        self.wav = self.dir / "alert_warning.wav"
        with wave.open(str(self.wav), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x00\x00" * 160)
        self.socket = self.root / "test.sock"

    async def test_ipc_check_and_playback_wait_for_completion(self):
        import manual_tts_ipc as ipc
        original = ipc.socket_path
        ipc.socket_path = lambda: self.socket
        self.addCleanup(lambda: setattr(ipc, "socket_path", original))
        entered = asyncio.Event()
        finish = asyncio.Event()
        played = []
        async def playback(path):
            played.append(path)
            entered.set()
            await finish.wait()
        server = ManualTtsServer(root=self.root, playback=playback, path=self.socket)
        await server.start()
        try:
            await request_tts('warning', check_only=True)
            task = asyncio.create_task(request_tts('warning', timeout_sec=2))
            await entered.wait()
            self.assertFalse(task.done())
            finish.set()
            await task
            self.assertEqual(played, [self.wav])
            with self.assertRaisesRegex(RuntimeError, 'missing'):
                await request_tts('report', check_only=True)
        finally:
            await server.close()
        self.assertFalse(self.socket.exists())

    async def test_preflight_rejects_missing_or_invalid_audio(self):
        with self.assertRaisesRegex(RuntimeError, 'missing'):
            verify_wav('report', self.root)
        self.wav.write_bytes(b'not-wave-data')
        with self.assertRaises(RuntimeError):
            verify_wav('warning', self.root)


if __name__ == '__main__':
    unittest.main()
