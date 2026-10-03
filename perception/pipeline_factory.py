from .device import resolve_cuda_device

PIPELINE_OPTIONS = {
    "1": "YOLO11n_ByteTrack_RTMPose_STGCN",
    "2": "YOLO11x_ByteTrack_RTMPose_STGCN",
    "3": "DINO_BotSORT_ViTPose_STGCNpp",
    "4": "YOLO11x_BotSORT_RTMPose_STGCNpp",
    "5": "YOLO11xPose_BotSORT_STGCNpp",
    "6": "YOLO26m_BotSORT_RTMPose_STGCNpp",
    "7": "YOLO26m_BotSORT_RTMPose_PoseConv3D",
    "8": "YOLO26mPose_BotSORT_STGCNpp",
    "9": "YOLO26mPose_BotSORT_PoseConv3D",
}


def print_pipeline_menu():
    print("\n=== 방범 순찰 로봇 행동 분석 파이프라인 선택 ===")
    print("1. YOLO11n - ByteTrack - RTMPose - ST-GCN")
    print("2. YOLO11x - ByteTrack - RTMPose - ST-GCN")
    print("3. DINO - Bot-SORT - ViTPose(H) - ST-GCN++")
    print("4. YOLO11x - Bot-SORT - RTMPose - ST-GCN++")
    print("5. YOLO11x-Pose - Bot-SORT - (통합) - ST-GCN++")
    print("6. YOLO26m - Bot-SORT - RTMPose - ST-GCN++")
    print("7. YOLO26m - Bot-SORT - RTMPose - PoseConv3D")
    print("8. YOLO26m-Pose - Bot-SORT - (통합) - ST-GCN++")
    print("9. YOLO26m-Pose - Bot-SORT - (통합) - PoseConv3D")


def resolve_device(device=None):
    return resolve_cuda_device(device)


def create_pipeline(choice, device=None):
    choice = str(choice).strip()
    device = resolve_device(device)

    if choice == "1":
        from .models.detector_yolo import YOLODetector
        from .models.action_rtmpose_stgcn import ActionRecognizer

        return {
            "name": PIPELINE_OPTIONS[choice],
            "detector": YOLODetector(weight="weights/yolo11n.pt", tracker="bytetrack", device=device),
            "action_analyzer": ActionRecognizer(device=device),
        }
    if choice == "2":
        from .models.detector_yolo import YOLODetector
        from .models.action_rtmpose_stgcn import ActionRecognizer

        return {
            "name": PIPELINE_OPTIONS[choice],
            "detector": YOLODetector(weight="weights/yolo11x.pt", tracker="bytetrack", device=device),
            "action_analyzer": ActionRecognizer(device=device),
        }
    if choice == "3":
        from .models.detector_dino import DINODetector
        from .models.action_vitpose_stgcnpp import ActionRecognizer

        return {
            "name": PIPELINE_OPTIONS[choice],
            "detector": DINODetector(device=device),
            "action_analyzer": ActionRecognizer(device=device),
        }
    if choice == "4":
        from .models.detector_yolo import YOLODetector
        from .models.action_rtmpose_stgcnpp import ActionRecognizer

        return {
            "name": PIPELINE_OPTIONS[choice],
            "detector": YOLODetector(weight="weights/yolo11x.pt", tracker="botsort", device=device),
            "action_analyzer": ActionRecognizer(device=device),
        }
    if choice == "5":
        from .models.detector_yolo import YOLOPoseDetector
        from .models.action_yolopose_stgcnpp import ActionRecognizer

        return {
            "name": PIPELINE_OPTIONS[choice],
            "detector": YOLOPoseDetector(weight="weights/yolo11x-pose.pt", tracker="botsort", device=device),
            "action_analyzer": ActionRecognizer(device=device),
        }
    if choice == "6":
        from .models.detector_yolo import YOLODetector
        from .models.action_rtmpose_stgcnpp import ActionRecognizer

        return {
            "name": PIPELINE_OPTIONS[choice],
            "detector": YOLODetector(weight="weights/yolo26m.pt", tracker="botsort", device=device),
            "action_analyzer": ActionRecognizer(device=device),
        }
    if choice == "7":
        from .models.detector_yolo import YOLODetector
        from .models.action_rtmpose_posec3d import ActionRecognizer

        return {
            "name": PIPELINE_OPTIONS[choice],
            "detector": YOLODetector(weight="weights/yolo26m.pt", tracker="botsort", device=device),
            "action_analyzer": ActionRecognizer(device=device),
        }
    if choice == "8":
        from .model_assets import ensure_pipeline_assets
        from .models.detector_yolo import YOLOPoseDetector
        from .models.action_yolopose_stgcnpp import ActionRecognizer

        assets = ensure_pipeline_assets(choice)
        return {
            "name": PIPELINE_OPTIONS[choice],
            "detector": YOLOPoseDetector(
                weight=str(assets["yolo_pose"]),
                tracker="botsort",
                device=device,
            ),
            "action_analyzer": ActionRecognizer(
                device=device,
                recall_mode=True,
                action_config=str(assets["action_config"]),
                action_checkpoint=str(assets["action_checkpoint"]),
            ),
        }
    if choice == "9":
        from .models.detector_yolo import YOLOPoseDetector
        from .models.action_yolopose_posec3d import ActionRecognizer

        return {
            "name": PIPELINE_OPTIONS[choice],
            "detector": YOLOPoseDetector(weight="weights/yolo26m-pose.pt", tracker="botsort", device=device),
            "action_analyzer": ActionRecognizer(device=device),
        }

    raise ValueError(f"지원하지 않는 파이프라인 번호입니다: {choice}")
