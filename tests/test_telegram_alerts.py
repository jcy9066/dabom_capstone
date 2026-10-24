import threading

import numpy as np

from perception.frame_processor import FrameProcessor
from perception.utils.event_taxonomy import vision_alert_type, vision_event_type
from perception.utils.telegram_notifier import TelegramNotifier


class Response:
    status_code = 200


def test_automatic_telegram_cooldown_is_per_robot_and_event_type():
    now = [100.0]
    calls = []
    call_event = threading.Event()

    def sender(url, **kwargs):
        calls.append((url, kwargs))
        call_event.set()
        return Response()

    notifier = TelegramNotifier(
        token="test-token",
        chat_id="test-chat",
        cooldown_sec=10,
        sender=sender,
        clock=lambda: now[0],
    )

    assert notifier.send_event_alert_async(
        "first", robot_id="robot-1", event_type="ASSAULT"
    )
    assert call_event.wait(1)
    call_event.clear()
    assert not notifier.send_event_alert_async(
        "duplicate", robot_id="robot-1", event_type="ASSAULT"
    )
    assert notifier.send_event_alert_async(
        "different", robot_id="robot-1", event_type="NETWORK_LOSS"
    )
    assert call_event.wait(1)
    call_event.clear()
    now[0] += 10
    assert notifier.send_event_alert_async(
        "after cooldown", robot_id="robot-1", event_type="ASSAULT"
    )
    assert call_event.wait(1)

    assert len(calls) == 3
    assert all(call[0].endswith("/sendMessage") for call in calls)
    assert all("files" not in call[1] for call in calls)


class FallingDetector:
    def track(self, _frame):
        return [{"id": 1, "cls": 0, "box": [0, 0, 16, 16]}]


class FallingAnalyzer:
    def process(self, _frame, _obj):
        return None, {"label": "FALLING", "score": 0.91, "is_danger": True}

    def draw_skeleton(self, *_args):
        pass


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def send_event_alert_async(self, message, *, robot_id, event_type):
        self.calls.append((message, robot_id, event_type))
        return True


def test_unmapped_danger_keeps_alert_without_forcing_database_enum():
    assert vision_event_type("FALLING") is None
    assert vision_alert_type("FALLING") == "FALLING"
    notifier = RecordingNotifier()
    processor = FrameProcessor(FallingDetector(), FallingAnalyzer(), notifier=notifier)
    processor.trigger.get_object_states = lambda _objects: {1: 1}

    result = processor.process(np.zeros((24, 24, 3), dtype=np.uint8))

    assert result["danger"] is True
    assert len(notifier.calls) == 1
    assert notifier.calls[0][1:] == ("local-video", "FALLING")
