from .device import resolve_cuda_device


# Preserve the existing pipeline ID for clients and settings.
PIPELINE_OPTIONS = {"8": "RTMOM_BotSORT_X3DMScene"}


def print_pipeline_menu():
    print("\n=== 방범 순찰 로봇 행동 분석 파이프라인 ===")
    print("8. RTMO-M + BotSORT + X3D-M")


def resolve_device(device=None):
    return resolve_cuda_device(device)


def create_pipeline(choice="8", device=None):
    choice = str(choice).strip()
    if choice != "8":
        raise ValueError(
            f"지원하지 않는 파이프라인 번호입니다: {choice}. "
            "RTMO-M + BotSORT + X3D-M (8)만 지원합니다."
        )

    # Reject legacy choices before loading CUDA or model assets.
    device = resolve_device(device)
    from .model_assets import ensure_pipeline_assets
    from .models.detector_rtmo import RTMOPoseDetector
    from .models.scene_x3d import SceneViolenceRecognizer

    assets = ensure_pipeline_assets(choice)
    return {
        "name": PIPELINE_OPTIONS[choice],
        "detector": RTMOPoseDetector(
            onnx_model=assets["rtmo_onnx"],
            tracker="perception/config/botsort_recall.yaml",
            device=device,
        ),
        "action_analyzer": SceneViolenceRecognizer(
            device=device,
            checkpoint=assets["scene_checkpoint"],
        ),
    }
