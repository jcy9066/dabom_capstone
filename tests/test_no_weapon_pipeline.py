"""Final pipeline has no weapon detector and ignores non-person classes."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pipeline_8_does_not_load_weapon_model():
    factory = (ROOT / "perception/pipeline_factory.py").read_text(encoding="utf-8")
    section = factory.split('if choice == "8":', 1)[1].split('if choice == "9":', 1)[0]
    assert "RTMOPoseDetector" in section
    assert "SceneViolenceRecognizer" in section
    assert "WeaponDetector" not in section
    assert "WEAPON_MODEL_PATH" not in section


def test_person_only_model_cannot_mislabel_unknown_class():
    app = (ROOT / "server/app.py").read_text(encoding="utf-8")
    processor = (ROOT / "perception/frame_processor.py").read_text(encoding="utf-8")
    trigger = (ROOT / "perception/core/trigger.py").read_text(encoding="utf-8")
    assert "weapon_predictions" not in app
    assert "WEAPON_" not in app
    assert 'detection["label"] = "WEAPON"' not in app
    assert 'detection["label"] = "WEAPON"' not in processor
    assert "weapon_detector" not in processor
    assert "weapons = " not in trigger


def test_no_new_weight_requirement():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "WEAPON_MODEL_PATH" not in env
    assert not (ROOT / "perception/models/weapon_detector.py").exists()
