import threading

import server.app as app


class _FakeActionAnalyzer:
    def __init__(self):
        self.reset_calls = 0
        self.generation_seen_at_reset = None

    def reset_tracking_state(self):
        self.reset_calls += 1
        self.generation_seen_at_reset = app.active_stream_id


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
    monkeypatch.setattr(app, "model_generation", 7)
    monkeypatch.setattr(app, "MODEL_ACTIVE", True)
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
    assert processor.action_analyzer.generation_seen_at_reset == new_generation
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
    assert result["_model_generation"] == 7
    assert processed_frames == ["current-frame"]
    assert app.inference_stats["dropped"] == 0


def test_model_toggle_drops_result_processed_before_off(monkeypatch):
    _isolate_stream_globals(monkeypatch)
    processed_ready = threading.Event()
    allow_publish = threading.Event()
    published_frames = []
    latest_updates = []
    worker_result = []

    def fake_process(frame):
        return {
            "frame": "model-overlay",
            "detections": [{"label": "FALLING"}],
            "danger": True,
            "timings": {},
        }

    def fake_publish(frame, robot_id=None):
        published_frames.append((frame, robot_id))
        return 99

    def fake_update(result, **kwargs):
        latest_updates.append((result, kwargs))

    monkeypatch.setattr(app, "process_frame_for_dashboard", fake_process)
    monkeypatch.setattr(app, "publish_preview_frame", fake_publish)
    monkeypatch.setattr(app, "update_latest_result", fake_update)

    def old_inference():
        processed = app.process_stream_frame_if_current("old-frame", 41)
        assert processed is not None
        generation = processed.pop("_model_generation")
        processed_ready.set()
        assert allow_publish.wait(timeout=2)
        worker_result.append(
            app.publish_processed_inference_if_current(
                processed,
                robot_id="robot",
                stream_id=41,
                processed_model_generation=generation,
                captured_at=1.0,
                adaptive_wait_ms=0.0,
                started_at=1.0,
            )
        )

    worker = threading.Thread(target=old_inference)
    worker.start()
    assert processed_ready.wait(timeout=2)

    previous_generation = app.model_generation
    detached = app.detach_frame_processor()
    app.MODEL_ACTIVE = False

    assert detached is not None
    assert app.model_generation == previous_generation + 1
    assert app.frame_processor is None

    allow_publish.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert worker_result == [False]
    assert published_frames == []
    assert latest_updates == []
    assert app.inference_stats["dropped"] == 1


def test_old_result_stays_stale_after_off_then_on(monkeypatch):
    processor = _isolate_stream_globals(monkeypatch)
    processed = app.process_stream_frame_if_current("old-frame", 41)
    old_generation = processed.pop("_model_generation")

    app.detach_frame_processor()
    app.MODEL_ACTIVE = False

    app.MODEL_ACTIVE = True
    app.frame_processor = processor
    app.detach_frame_processor()
    app.frame_processor = processor

    published_frames = []
    monkeypatch.setattr(
        app,
        "publish_preview_frame",
        lambda frame, robot_id=None: published_frames.append(frame) or 1,
    )
    monkeypatch.setattr(app, "update_latest_result", lambda *args, **kwargs: None)

    accepted = app.publish_processed_inference_if_current(
        processed,
        robot_id="robot",
        stream_id=41,
        processed_model_generation=old_generation,
        captured_at=1.0,
        adaptive_wait_ms=0.0,
        started_at=1.0,
    )

    assert accepted is False
    assert published_frames == []
    assert app.model_generation > old_generation
