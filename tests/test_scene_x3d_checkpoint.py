from collections import OrderedDict

import torch

from perception.models.scene_x3d import SceneViolenceRecognizer


def test_normalize_released_x3d_checkpoint_keys():
    state = OrderedDict(
        {
            "backbone.blocks.0.conv.conv_t.weight": torch.zeros(1),
            "backbone.blocks.5.proj.1.weight": torch.zeros(2, 2048),
            "backbone.blocks.5.proj.1.bias": torch.zeros(2),
        }
    )

    normalized = SceneViolenceRecognizer._normalize_checkpoint_state_dict(
        state
    )

    assert "blocks.0.conv.conv_t.weight" in normalized
    assert "blocks.5.proj.weight" in normalized
    assert "blocks.5.proj.bias" in normalized

    assert not any(
        key.startswith("backbone.")
        for key in normalized
    )


def test_normalize_module_and_backbone_prefixes():
    state = OrderedDict(
        {
            "module.backbone.blocks.0.norm.weight": torch.zeros(24),
        }
    )

    normalized = SceneViolenceRecognizer._normalize_checkpoint_state_dict(
        state
    )

    assert list(normalized) == ["blocks.0.norm.weight"]


def test_normalize_native_pytorchvideo_keys_are_preserved():
    state = OrderedDict(
        {
            "blocks.0.norm.weight": torch.zeros(24),
            "blocks.5.proj.weight": torch.zeros(2, 2048),
        }
    )

    normalized = SceneViolenceRecognizer._normalize_checkpoint_state_dict(
        state
    )

    assert list(normalized) == list(state)
