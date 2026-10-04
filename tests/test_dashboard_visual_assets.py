import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
STYLE = (ROOT / "frontend/services/static/style.css").read_text(encoding="utf-8")
SYSTEM_CONTROL = (ROOT / "frontend/services/static/system_control.js").read_text(encoding="utf-8")
SYSTEM_CONTROL_STYLE = (ROOT / "frontend/services/static/system_control.css").read_text(encoding="utf-8")
CONTROL_STYLE = (ROOT / "frontend/components/controls/controls.css").read_text(encoding="utf-8")
RECORD_STYLE = (ROOT / "frontend/components/records/records.css").read_text(encoding="utf-8")


def test_dashboard_guide_explains_state_information_sources():
    expected = (
        "상태 정보 출처",
        "CPU / 온도 / RAM / Ping",
        "Camera Stream 상태",
        "Navigation Control 상태",
        "LiDAR ROS Bridge / Encoder ROS Bridge / Wheel Odometry / SLAM Mapping / Map Bridge",
        "서버 DB 기록",
    )
    for text in expected:
        assert text in SYSTEM_CONTROL

    for preserved_heading in ("'Mapping'", "'Driving'", "'\\uc704\\ud5d8 \\ub300\\uc751'", "'\\uae30\\uae30 \\uc0c1\\ud0dc'", "'\\ud504\\ub85c\\uc138\\uc2a4 \\uc2e4\\ud589 \\uc21c\\uc11c'"):
        assert preserved_heading in SYSTEM_CONTROL


def test_all_component_assets_share_cache_busting_key():
    component_assets = re.findall(r'(?:href|src)="(/components/[^"]+)"', TEMPLATE)
    assert component_assets
    assert all("?v=" in asset for asset in component_assets)
    assert len(component_assets) == 18


def test_static_assets_use_same_cache_busting_strategy():
    static_assets = re.findall(r'(?:href|src)="(static/[^"]+)"', TEMPLATE)
    assert static_assets
    assert all("?v=" in asset for asset in static_assets)


def test_space_mono_is_not_requested_or_referenced():
    combined = TEMPLATE + STYLE + SYSTEM_CONTROL_STYLE + CONTROL_STYLE + RECORD_STYLE
    assert "Space Mono" not in combined
    assert "Space+Mono" not in combined
    assert "'Noto Sans KR', 'Noto Sans', sans-serif" in STYLE


def test_toolbar_dom_and_tab_order_match_required_order_without_css_order():
    start = TEMPLATE.index('id="records-toolbar-mount"')
    end = TEMPLATE.index("\n            </div>\n        </div>", start)
    toolbar = TEMPLATE[start:end]
    views = ("patrolModal", "galleryModal", "currentSituation", "actionsModal", "statusModal")
    positions = [toolbar.index(f'data-record-view="{view}"') for view in views]
    assert positions == sorted(positions)
    assert "tabindex" not in toolbar
    assert 'data-record-view="patrolModal"] { order:' not in STYLE
    assert ".current-situation-record-btn { order:" not in STYLE


def test_component_buttons_follow_dashboard_visual_states():
    assert "background: var(--card-bg2);" in CONTROL_STYLE
    assert ".dashboard-mode-button:disabled" in CONTROL_STYLE
    assert ".dpad-estop-button:hover:not(:disabled)" in CONTROL_STYLE
    assert ".filter-btn.secondary:focus-visible" in STYLE
    assert ".record-cycle-filter" not in RECORD_STYLE
    assert ".records-pagination button:hover:not(:disabled)" in RECORD_STYLE
    assert ".system-control-manual:focus-visible" in SYSTEM_CONTROL_STYLE
    assert ".dashboard-drive-mode-group" not in CONTROL_STYLE


def test_dashboard_controls_preserve_text_at_compact_widths():
    assert "flex-wrap: wrap; gap: 6px; flex-shrink: 0;" in STYLE
    assert "line-height: 1.25; overflow-wrap: anywhere; white-space: normal;" in STYLE
    assert "flex: 1 1 112px;" in STYLE
    assert "flex-basis: 100%;" in STYLE
    assert "max-width: calc(100% - 40px);" in STYLE
    assert "@media (max-width: 520px)" in STYLE
    assert ".control-grid { grid-template-columns: minmax(0, 1fr);" in STYLE
    assert ".server-state-field-wide { grid-column: auto; }" in STYLE
    report_button = re.search(
        r'class="card action-btn action-report"[^>]*>\s*([^<]+?)\s*</button>',
        TEMPLATE,
    )
    assert report_button and report_button.group(1) == "신고"
