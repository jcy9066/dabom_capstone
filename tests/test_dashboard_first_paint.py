"""Guard final dashboard markup against pre-hydration legacy UI flashes."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend/templates/index.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "frontend/services/static/script.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend/services/static/selected_ui.css").read_text(encoding="utf-8")
NAV = (ROOT / "frontend/components/controls/navigation_mode_control.js").read_text(encoding="utf-8")
MODEL = (ROOT / "frontend/components/controls/model_pipeline_toggle.js").read_text(encoding="utf-8")
LED = (ROOT / "frontend/components/controls/led_toggle.js").read_text(encoding="utf-8")
DRIVE = (ROOT / "frontend/components/controls/drive_mode_control.js").read_text(encoding="utf-8")


def test_controls_exist_in_final_form_before_scripts_run():
    first_scripts = HTML.index('<script src="/components/modal/modal_manager.js')
    markup = HTML[:first_scripts]
    assert markup.count('class="navigation-mode-toggle unknown"') == 2
    assert 'class="dashboard-model-toggle unknown"' in markup
    assert 'class="dashboard-led-toggle"' in markup
    assert 'class="dashboard-drive-mode-control"' in markup
    assert 'id="dashboardUiStartupError"' in markup


def test_runtime_adopts_existing_markup_without_double_init():
    for source, cls in (
        (NAV, "navigation-mode-toggle"),
        (MODEL, "dashboard-model-toggle"),
        (LED, "dashboard-led-toggle"),
    ):
        assert f"root.querySelector('.{cls}')" in source
        assert "if (existing)" in source
        assert "root.replaceChildren" in source  # only for missing new UI, not legacy fallback
    assert ".dashboard-drive-mode-group')?.remove()" not in DRIVE


def test_theme_applied_before_first_visual_control():
    first_theme = HTML.index("document.body.classList.add('dark-mode')")
    first_control = HTML.index('id="model-pipeline-toggle-mount"')
    assert first_theme < first_control
    assert "let darkMode = document.body.classList.contains('dark-mode');" in SCRIPT
    assert "if (saved === '1') toggleDarkMode();" not in SCRIPT
    assert "body.dark-mode #darkToggle" in CSS


def test_initialization_failure_never_falls_back_to_old_markup():
    assert 'id="dashboardUiStartupError"' in HTML
    assert "mount._dabomDriveModeController" in HTML
    assert "mount._dabomLedToggleController" in HTML
    assert "window.location.reload()" in HTML
    assert "dataset.dashboardUiReady" in HTML
