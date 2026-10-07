from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def test_pipeline8_server_uses_scene_level_violence_result():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert '"scene": scene_result' in source
    assert 'getattr(analyzer, "scene_level_classifier", False)' in source
    assert "scene_result = analyzer.classify_scene()" in source
    assert 'scene_result.get("raw_danger")' in source


def test_pipeline8_scene_danger_is_not_assigned_to_each_person():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert 'detection["inference_status"] = "pose_visualization"' in source
    assert '"track_label": f"ID {oid}"' in source
    assert "danger = danger or scene_danger" in source
    assert "draw_scene_overlay(display_frame, scene_result)" in source


def test_h264_path_feeds_every_decoded_frame_to_x3d_buffer():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert "observe_scene_frame(frame, now, stream_id=stream_id)" in source
    assert "frame_index += 1" in source


def test_x3d_buffer_rejects_stale_stream_generation():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert "def observe_scene_frame(frame, timestamp=None, stream_id=None):" in source
    assert "if stream_id != active_stream_id:" in source
    assert "observe_scene_frame(frame, now, stream_id=stream_id)" in source


def test_stream_reconnect_resets_pose_tracker():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert "processor.detector" in source
    assert '"reset_tracking_state"' in source
    assert "reset_detector_tracking()" in source


def test_dashboard_model_toggle_is_stream_master_switch():
    server = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")
    pi_start = (ROOT_DIR / "start_pi_stack.sh").read_text(encoding="utf-8")

    assert "infer_override" not in server
    assert 'request.query_params.get("infer")' not in server
    assert "infer = MODEL_ACTIVE" in server
    assert "&infer=" not in pi_start
    assert "STREAM_INFER" not in pi_start


def test_pipeline8_requires_at_least_one_hz_inference_requests():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert "effective_request_fps = min(" in source
    assert "effective_request_fps < 1.0" in source
    assert "Pipeline 8 requires an effective inference request rate >= 1 Hz" in source


def test_scene_error_and_person_ids_are_visible_without_false_normal():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")
    scene = (ROOT_DIR / "perception" / "models" / "scene_x3d.py").read_text(
        encoding="utf-8"
    )

    assert 'detection.get("track_label")' in source
    assert "SCENE AI  |  UNAVAILABLE" in scene
    assert "SCENE AI  |  WARMING UP" in scene


def test_stale_person_pose_overlay_expires_before_scene_status():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert "pose_overlay_fresh = result_age_sec <= min(" in source
    assert "1.25" in source
    assert "if pose_overlay_fresh:" in source


def test_latest_legacy_pair_staleness_guards_are_preserved():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert '"observation_stale": False' in source
    assert 'current_action.get("observation_stale")' in source
    assert 'source.get("observation_stale")' in source
    assert "draw_interaction_overlays" in source


def test_x3d_buffer_append_is_serialized_with_stream_reset():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert "Serialize scene-buffer append with stream/model reset" in source
    assert "with processing_lock:" in source
    assert "observe_scene_frame(" in source


def test_pipeline8_ignores_legacy_action_policy_controls():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert 'if pipeline != "8":' in source
    assert 'if config["pipeline"] != "8":' in source


def test_stale_h264_generation_cannot_overwrite_new_stream_state():
    source = (ROOT_DIR / "server" / "app.py").read_text(encoding="utf-8")

    assert "stream_id=None," in source
    assert "if stream_id is not None and stream_id != active_stream_id:" in source
    assert "stream_id=stream_id," in source
    assert "def ffmpeg_stderr_loop(proc, stream_id):" in source
    assert "args=(proc, stream_id)" in source
    assert "if stream_id != active_stream_id:" in source
    assert "if stream_id == active_stream_id:" in source
    assert "current_generation = (" in source
