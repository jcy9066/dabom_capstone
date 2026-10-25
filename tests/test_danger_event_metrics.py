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

def test_operational_recall_counts_unavailable_positive_as_missed():
    r = evaluate_events([
        {"truth": True, "predicted": True},
        {"truth": True, "predicted": None},
        {"truth": False, "predicted": False},
    ])
    assert r["unavailable_positive"] == 1
    assert r["recall"] == 1.0
    assert r["operational_recall"] == 0.5


def test_unavailable_only_cannot_claim_perfect_recall():
    r = evaluate_events([{"truth": True, "predicted": None}])
    assert r["operational_recall"] == 0.0
    assert r["coverage"] == 0.0


def test_all_four_confusion_matrix_cells():
    events = [
        {"truth": True, "predicted": True},
        {"truth": True, "predicted": False},
        {"truth": False, "predicted": True},
        {"truth": False, "predicted": False},
    ]
    result = evaluate_events(events)
    assert {key:result[key] for key in ("tp", "fp", "fn", "tn")} == {
        "tp": 1, "fp": 1, "fn": 1, "tn": 1,
    }


def test_only_negative_events_do_not_inflate_false_negatives():
    result = evaluate_events([{"truth": False, "predicted": False}] * 4)
    assert result["tn"] == 4
    assert result["fn"] == 0
    assert result["recall"] is None


def test_violence_misses_reduce_recall_and_f1():
    result = evaluate_events([
        {"truth": True, "predicted": True},
        {"truth": True, "predicted": False},
        {"truth": True, "predicted": False},
        {"truth": False, "predicted": False},
    ])
    assert result["fn"] == 2
    assert result["tn"] == 1
    assert result["recall"] == 1 / 3
    assert result["f1"] == 0.5
