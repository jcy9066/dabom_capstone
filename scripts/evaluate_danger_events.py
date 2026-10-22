"""Offline event-level evaluation for danger detection. No models/hardware needed.

Input JSON: {"events":[{"truth":true,"predicted":false,"latency_ms":42.0},...]}
Positive truth means an observed unsafe event; unclassified/UNAVAILABLE must not
be counted as a true negative.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def evaluate_events(events: list[dict]) -> dict:
    counts = {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "unavailable": 0}
    latencies = []
    for event in events:
        if not isinstance(event.get("truth"), bool):
            raise ValueError("truth must be bool")
        predicted = event.get("predicted")
        if predicted is None:
            counts["unavailable"] += 1
            continue
        if not isinstance(predicted, bool):
            raise ValueError("predicted must be bool or null")
        key = ("t" if event["truth"] else "f") + ("p" if predicted else "n")
        counts[key] += 1
        if event.get("latency_ms") is not None:
            latency = float(event["latency_ms"])
            if not math.isfinite(latency) or latency < 0:
                raise ValueError("latency must be finite nonnegative")
            latencies.append(latency)
    tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision is not None and recall is not None and precision + recall else None)
    ordered = sorted(latencies)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1] if ordered else None
    return {**counts, "precision": precision, "recall": recall, "f1": f1,
            "p95_latency_ms": p95, "latency_samples": len(ordered),
            "coverage": (len(events) - counts["unavailable"]) / len(events) if events else None}


def evaluate_runtime(frames: list[dict], wall_time_sec: float | None = None) -> dict:
    """Summarize recorded inference performance without inventing GPU benchmarks.

    `frames`: elapsed_ms per processed frame, optional vram_mb observed then.
    Actual throughput is only reported when wall_time_sec was measured.
    """
    elapsed: list[float] = []
    vram: list[float] = []
    for frame in frames:
        milliseconds = float(frame["elapsed_ms"])
        if not math.isfinite(milliseconds) or milliseconds < 0:
            raise ValueError("elapsed_ms must be finite and nonnegative")
        elapsed.append(milliseconds)
        if frame.get("vram_mb") is not None:
            value = float(frame["vram_mb"])
            if not math.isfinite(value) or value < 0:
                raise ValueError("vram_mb must be finite and nonnegative")
            vram.append(value)
    if wall_time_sec is not None:
        wall_time_sec = float(wall_time_sec)
        if not math.isfinite(wall_time_sec) or wall_time_sec <= 0:
            raise ValueError("wall_time_sec must be positive and finite")
    ordered = sorted(elapsed)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1] if ordered else None
    return {
        "frame_count": len(elapsed),
        "mean_inference_ms": (sum(elapsed) / len(elapsed)) if elapsed else None,
        "p95_inference_ms": p95,
        "achieved_fps": len(elapsed) / wall_time_sec
        if wall_time_sec is not None else None,
        "peak_recorded_vram_mb": max(vram) if vram else None,
        "vram_samples": len(vram),
    }



def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="JSON with labeled events and predictions")
    args = parser.parse_args()
    payload = json.loads(args.dataset.read_text(encoding="utf-8"))
    report = {"detection": evaluate_events(payload["events"])}
    if "runtime_frames" in payload:
        report["runtime"] = evaluate_runtime(
            payload["runtime_frames"], payload.get("wall_time_sec")
        )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
