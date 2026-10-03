import threading

import server.app as app


class _FakeActionAnalyzer:
    def __init__(self):
        self.reset_calls = 0

    def reset_tracking_state(self):
        self.reset_calls += 1


class _FakeViolenceHeuristic:
    def __init__(self):
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1


class _FakeProcessor:
    def __init__(self):
        self.action_analyzer = _FakeActionAnalyzer()
        self.action_display_buffer = {"old": {"label": "FALLING"}}
        self.violence_heuristic = _FakeViolenceHeuristic()


def _isolate_stream_globals(monkeypatch):
    processor = _FakeProcessor()
    monkeypatch.setattr(app, "processing_lock", threading.Lock())
    monkeypatch.setattr(app, "state_lock", threading.Lock())
    monkeypatch.setattr(app, "inference_condition", threading.Condition())
    monkeypatch.setattr(
        app,
        "inference_slot",
        {
            "frame": "old-frame",
            "robot_id": "robot",
            "frame_seq": 1,
            "captured_at": 1.0,
            "stream_id": 41,
        },
    )
    monkeypatch.setattr(app, "frame_stats", {})
    monkeypatch.setattr(app, "decode_stats", {})
    monkeypatch.setattr(app, "publish_stats", {})
    monkeypatch.setattr(app, "inference_rate_stats", {})
    monkeypatch.setattr(app, "inference_stats", {"dropped": 0})
    monkeypatch.setattr(app, "stream_stats", {})
    monkeypatch.setattr(app, "active_stream_id", 41)
    monkeypatch.setattr(app, "frame_processor", processor)
    return processor


def test_reconnect_rejects_old_frame_waiting_before_processing(monkeypatch):
    processor = _isolate_stream_globals(monkeypatch)
    old_generation = app.active_stream_id
    old_frame_ready = threading.Event()
    allow_old_frame_to_process = threading.Event()
    processed_frames = []
    worker_result = []

    def fake_process(frame):
        processed_frames.append(frame)
        return {"frame": frame, "detections": [], "danger": False}

    monkeypatch.setattr(app, "process_frame_for_dashboard", fake_process)

    def old_inference():
        # This models an old frame that already left the inference queue and is
        # waiting to enter the processing critical section.
        old_frame_ready.set()
        assert allow_old_frame_to_process.wait(timeout=2)
        worker_result.append(
            app.process_stream_frame_if_current("old-frame", old_generation)
        )

    worker = threading.Thread(target=old_inference)
    worker.start()
    assert old_frame_ready.wait(timeout=2)

    new_generation = app.activate_stream_generation("robot", True)
    allow_old_frame_to_process.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert new_generation == old_generation + 1
    assert app.active_stream_id == new_generation
    assert worker_result == [None]
    assert processed_frames == []
    assert app.inference_stats["dropped"] == 1
    assert processor.action_analyzer.reset_calls == 1
    assert processor.violence_heuristic.reset_calls == 1
    assert processor.action_display_buffer == {}
    assert app.inference_slot["frame"] is None
    assert app.inference_slot["stream_id"] is None


def test_current_generation_processes_normally(monkeypatch):
    _isolate_stream_globals(monkeypatch)
    processed_frames = []

    def fake_process(frame):
        processed_frames.append(frame)
        return {"frame": frame, "detections": [], "danger": False}

    monkeypatch.setattr(app, "process_frame_for_dashboard", fake_process)

    result = app.process_stream_frame_if_current("current-frame", 41)

    assert result["frame"] == "current-frame"
    assert processed_frames == ["current-frame"]
    assert app.inference_stats["dropped"] == 0
