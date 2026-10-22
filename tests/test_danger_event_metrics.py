from scripts.evaluate_danger_events import evaluate_events, evaluate_runtime


def test_recall_first_metrics_and_p95():
    result = evaluate_events([
        {"truth": True, "predicted": True, "latency_ms": 100},
        {"truth": True, "predicted": False, "latency_ms": 110},
        {"truth": False, "predicted": True, "latency_ms": 120},
        {"truth": False, "predicted": False, "latency_ms": 130},
    ])
    assert result["tp"] == 1
    assert result["fn"] == 1
    assert result["precision"] == 0.5
    assert result["recall"] == 0.5
    assert result["f1"] == 0.5
    assert result["p95_latency_ms"] == 130


def test_unavailable_is_not_silent_normal():
    result = evaluate_events([
        {"truth": True, "predicted": None},
        {"truth": False, "predicted": False},
    ])
    assert result["unavailable"] == 1
    assert result["tn"] == 1
    assert result["coverage"] == 0.5


def test_runtime_fps_latency_and_vram():
    result = evaluate_runtime([
        {"elapsed_ms": 20, "vram_mb": 500},
        {"elapsed_ms": 30, "vram_mb": 540},
        {"elapsed_ms": 25, "vram_mb": 520},
    ], wall_time_sec=0.12)
    assert result["frame_count"] == 3
    assert result["p95_inference_ms"] == 30
    assert result["achieved_fps"] == 25
    assert result["peak_recorded_vram_mb"] == 540


def test_runtime_without_observations_has_no_claimed_performance():
    result = evaluate_runtime([])
    assert result["achieved_fps"] is None
    assert result["peak_recorded_vram_mb"] is None
    assert result["mean_inference_ms"] is None
