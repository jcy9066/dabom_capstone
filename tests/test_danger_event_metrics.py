from scripts.evaluate_danger_events import evaluate_events


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
