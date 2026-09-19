from server.navigation_trajectory import NavigationTrajectoryTracker


def test_trajectory_samples_motion_and_resets_on_mode_change():
    tracker = NavigationTrajectoryTracker(min_distance_m=0.05)

    assert tracker.note_pose("mapping", {"x": 0.0, "y": 0.0})
    assert not tracker.note_pose("mapping", {"x": 0.02, "y": 0.0})
    assert tracker.note_pose("mapping", {"x": 0.06, "y": 0.0})

    mapping = tracker.snapshot()
    assert mapping["mode"] == "MAPPING"
    assert mapping["point_count"] == 2
    assert len(mapping["segments"]) == 1

    assert tracker.note_pose("localization_nav2", {"x": 1.0, "y": 1.0})
    driving = tracker.snapshot()
    assert driving["mode"] == "DRIVING"
    assert driving["point_count"] == 1
    assert driving["segments"] == [[{"x": 1.0, "y": 1.0}]]


def test_trajectory_breaks_segment_on_localization_jump():
    tracker = NavigationTrajectoryTracker(
        min_distance_m=0.05,
        segment_break_distance_m=1.0,
    )

    tracker.note_pose("localization_nav2", {"x": 0.0, "y": 0.0})
    tracker.note_pose("localization_nav2", {"x": 0.1, "y": 0.0})
    tracker.note_pose("localization_nav2", {"x": 2.0, "y": 2.0})
    tracker.note_pose("localization_nav2", {"x": 2.1, "y": 2.0})

    snapshot = tracker.snapshot()
    assert snapshot["point_count"] == 4
    assert len(snapshot["segments"]) == 2
    assert snapshot["segments"][0][-1] == {"x": 0.1, "y": 0.0}
    assert snapshot["segments"][1][0] == {"x": 2.0, "y": 2.0}


def test_driving_map_change_resets_trajectory_but_mapping_updates_do_not():
    tracker = NavigationTrajectoryTracker(min_distance_m=0.05)

    tracker.note_map("mapping", "map-a")
    tracker.note_pose("mapping", {"x": 0.0, "y": 0.0})
    tracker.note_pose("mapping", {"x": 0.1, "y": 0.0})
    tracker.note_map("mapping", "map-b")
    assert tracker.snapshot()["point_count"] == 2

    tracker.note_map("localization_nav2", "saved-a")
    tracker.note_pose("localization_nav2", {"x": 1.0, "y": 1.0})
    tracker.note_pose("localization_nav2", {"x": 1.1, "y": 1.0})
    tracker.note_map("localization_nav2", "saved-b")

    snapshot = tracker.snapshot()
    assert snapshot["mode"] == "DRIVING"
    assert snapshot["point_count"] == 0
    assert snapshot["segments"] == []


def test_trajectory_point_history_is_bounded():
    tracker = NavigationTrajectoryTracker(
        min_distance_m=0.01,
        segment_break_distance_m=10.0,
        max_points=3,
    )

    for index in range(5):
        tracker.note_pose("mapping", {"x": index * 0.1, "y": 0.0})

    snapshot = tracker.snapshot()
    assert snapshot["point_count"] == 3
    assert snapshot["segments"][0] == [
        {"x": 0.2, "y": 0.0},
        {"x": 0.30000000000000004, "y": 0.0},
        {"x": 0.4, "y": 0.0},
    ]
