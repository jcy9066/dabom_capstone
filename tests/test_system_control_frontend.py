from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "frontend/services/static/system_control.js").read_text(encoding="utf-8")
STYLE = (ROOT / "frontend/services/static/system_control.css").read_text(encoding="utf-8")
BACKEND = (ROOT / "frontend/system_control.py").read_text(encoding="utf-8")


def test_settings_guide_uses_dashboard_actions_and_modal_manager():
    for label in (
        r"\ub300\uc2dc\ubcf4\ub4dc \uc0ac\uc6a9 \uc548\ub0b4",
        r"[Mapping] \uc120\ud0dd",
        r"[Driving] \uc120\ud0dd",
        r"[\uc21c\ucc30 \uae30\ub85d \uc870\ud68c]",
        r"[\uc774\ubbf8\uc9c0 \uac24\ub7ec\ub9ac]",
        r"[\ud604\uc7ac \uc0c1\ud669 \uc791\uc131]",
        r"[\uad00\ub9ac\uc790 \uc870\uce58 \uc870\ud68c]",
        r"[\uae30\uae30 \uc0c1\ud0dc \uc870\ud68c]",
    ):
        assert label in SCRIPT
    assert "DabomDashboardComponents?.modal" in SCRIPT
    assert "processGuideItems()" in SCRIPT
    assert "state.status.gpu" in SCRIPT
    assert "state.status.pi" in SCRIPT
    assert "component.id !== 'map_bridge'" in SCRIPT
    assert r"SLAM Mapping\uc740 ${label}\ub97c \ud568\uaed8 \uc2dc\uc791" in SCRIPT


def test_disconnected_status_does_not_become_process_guide_source():
    assert "function render(status, available = true)" in SCRIPT
    assert "state.status = available ? status : null" in SCRIPT
    assert "}] }, false);" in SCRIPT
    assert "if (!state.status) return [text.processUnavailable]" in SCRIPT


def test_backend_component_descriptions_match_frontend_contract():
    for description in (
        "Pi\uc758 LiDAR \ub370\uc774\ud130\ub97c ROS 2 /scan\uc73c\ub85c \uc804\ub2ec",
        "Pi\uc758 \uc5d4\ucf54\ub354 \ub370\uc774\ud130\ub97c ROS 2 /wheel_ticks\ub85c \uc804\ub2ec",
        "root GPU supervisor\uac00 \uad00\ub9ac\ud558\ub294 \uc5d4\ucf54\ub354 \uae30\ubc18 odometry",
        "LiDAR \uae30\ubc18 \uc9c0\ub3c4 \uc791\uc131\uacfc Map Bridge \ud568\uaed8 \uc2e4\ud589",
        "Mapping/Driving launch\uac00 \uc18c\uc720\ud558\ub294 \uc9c0\ub3c4\u00b7\uc704\uce58 \uc804\uc1a1 \ub178\ub4dc",
        "Pi\uc5d0\uc11c LiDAR \uc2a4\uce94 \ub370\uc774\ud130\ub97c \uc218\uc9d1",
    ):
        assert description in BACKEND


def test_pending_and_unavailable_cursors_are_distinct():
    assert "is-pending" in SCRIPT
    assert "is-unavailable" in SCRIPT
    assert ".is-pending:disabled" in STYLE
    assert "cursor: progress" in STYLE
    assert ".is-unavailable:disabled" in STYLE
    assert "cursor: not-allowed" in STYLE
    assert "system-control-normalize:disabled { cursor: wait" not in STYLE
