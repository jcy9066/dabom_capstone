import cv2
import argparse
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(CURRENT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from perception.frame_processor import FrameProcessor
from perception.pipeline_factory import create_pipeline, print_pipeline_menu
from server.privacy import PrivateVideoWriter, PrivacyProcessingError

class LocalVideoReader:
    def __init__(self, path):
        if not os.path.exists(path):
            raise FileNotFoundError(f"에러: 영상 파일이 없습니다 -> {path}")
        self.cap = cv2.VideoCapture(path)
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        source_fps = float(self.cap.get(cv2.CAP_PROP_FPS))
        self.fps = source_fps if source_fps > 0.0 else 30.0

    def get_frame(self):
        ret, frame = self.cap.read()
        return frame if ret else None

    def release(self):
        self.cap.release()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=str, required=True, help="테스트 영상 경로")
    parser.add_argument('--pipeline', type=str, help="구동할 파이프라인 번호 (1-9)")
    parser.add_argument('--max-frames', type=int, default=0, help="테스트용 최대 처리 프레임 수")
    args = parser.parse_args()

    if args.pipeline:
        choice = args.pipeline
    else:
        print_pipeline_menu()
        choice = input("\n구동할 파이프라인 번호를 입력하십시오 (1-9): ").strip()

    try:
        pipeline = create_pipeline(choice)
    except ValueError as exc:
        print(exc)
        return

    pipeline_name = pipeline["name"]
    processor = FrameProcessor(pipeline["detector"], pipeline["action_analyzer"])
    reader = LocalVideoReader(args.source)

    out_dir = os.path.join("output", pipeline_name)
    out_path = os.path.join(out_dir, f"result_{os.path.basename(args.source)}")
    try:
        writer = PrivateVideoWriter(
            out_path,
            fps=reader.fps,
            frame_size=(reader.width, reader.height),
        )
    except PrivacyProcessingError:
        reader.release()
        raise

    print(f"\n🚀 [{pipeline_name}] 분석을 시작합니다...")

    frame_count = 0
    try:
        while True:
            frame = reader.get_frame()
            if frame is None: break

            frame_timestamp = frame_count / reader.fps
            processed = processor.process(
                frame,
                timestamp=frame_timestamp,
            )
            writer.write(processed["frame"])
            frame_count += 1
            if args.max_frames and frame_count >= args.max_frames:
                break
    except Exception:
        writer.abort()
        raise
    else:
        writer.close()
    finally:
        reader.release()
    print(f"✅ 분석 완료. 개인정보 처리된 결과 파일이 저장되었습니다: {out_path}")

if __name__ == "__main__":
    main()
