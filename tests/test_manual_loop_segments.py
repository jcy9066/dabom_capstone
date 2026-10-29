"""Linear time event-to-segment conversion for LOOP MANUAL."""
import pytest

from server.manual_loop_segments import compile_manual_segments


def test_move_stop_and_reverse_duration_order():
    events = [
        {"at": 0.0, "type": "move", "direction": "forward", "speed": .2},
        {"at": 5.0, "type": "stop"},
        {"at": 5.5, "type": "move", "direction": "backward", "speed": .2},
        {"at": 6.5, "type": "stop"},
    ]
    segments = compile_manual_segments(events)
    assert [(s["type"], round(s["duration_ms"])) for s in segments] == [
        ("move", 5000), ("stop", 500), ("move", 1000),
    ]
    assert segments[2]["direction"] == "backward"


def test_movement_to_movement_preserves_each_direction():
    events = [
        {"at": 0, "type": "move", "direction": "forward", "speed": .25},
        {"at": .2, "type": "move", "direction": "rotate_left", "speed": .20},
        {"at": .4, "type": "stop"},
    ]
    segments = compile_manual_segments(events)
    assert [s["direction"] for s in segments] == ["forward", "rotate_left"]


@pytest.mark.parametrize("events", [
    [{"at": 0, "type": "move", "direction": "forward", "speed": .2}],
    [{"at": 0, "type": "move", "direction": "forward", "speed": .2},
     {"at": -1, "type": "stop"}],
    [{"at": 0, "type": "move", "direction": "forward", "speed": .9},
     {"at": 1, "type": "stop"}],
])
def test_invalid_bags_fail_closed(events):
    with pytest.raises(ValueError):
        compile_manual_segments(events)


def test_large_bag_has_bounded_segments():
    events = [{"at": i * .01, "type": "move", "direction": "forward", "speed": .2}
              for i in range(9999)]
    events.append({"at": 99.99, "type": "stop"})
    segments = compile_manual_segments(events)
    assert len(segments) == 9999
