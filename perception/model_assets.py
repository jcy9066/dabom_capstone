from __future__ import annotations

import argparse
import os
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WEIGHTS_DIR = PROJECT_ROOT / "weights"


@dataclass(frozen=True)
class ModelAsset:
    key: str
    filename: str
    url: str
    min_bytes: int


PIPELINE_8_ASSETS = (
    ModelAsset(
        key="yolo_pose",
        filename="yolo26m-pose.pt",
        url="https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26m-pose.pt",
        min_bytes=1_000_000,
    ),
    ModelAsset(
        key="action_config",
        filename="stgcnpp_8xb16-joint-u100-80e_ntu60-xsub-keypoint-2d.py",
        url=(
            "https://raw.githubusercontent.com/open-mmlab/mmaction2/v1.2.0/"
            "configs/skeleton/stgcnpp/"
            "stgcnpp_8xb16-joint-u100-80e_ntu60-xsub-keypoint-2d.py"
        ),
        min_bytes=100,
    ),
    ModelAsset(
        key="action_checkpoint",
        filename=(
            "stgcnpp_8xb16-joint-u100-80e_ntu60-xsub-keypoint-2d_"
            "20221228-86e1e77a.pth"
        ),
        url=(
            "https://download.openmmlab.com/mmaction/v1.0/skeleton/stgcnpp/"
            "stgcnpp_8xb16-joint-u100-80e_ntu60-xsub-keypoint-2d/"
            "stgcnpp_8xb16-joint-u100-80e_ntu60-xsub-keypoint-2d_"
            "20221228-86e1e77a.pth"
        ),
        min_bytes=1_000_000,
    ),
)


def _is_valid_existing_file(path: Path, min_bytes: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= min_bytes
    except OSError:
        return False


def _download_atomic(asset: ModelAsset, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.part")
    request = urllib.request.Request(
        asset.url,
        headers={"User-Agent": "dabom-capstone-model-bootstrap/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            with temporary.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
        if not _is_valid_existing_file(temporary, asset.min_bytes):
            raise RuntimeError(
                f"Downloaded model asset is incomplete: {asset.filename}"
            )
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def ensure_pipeline_assets(pipeline, *, weights_dir: str | Path | None = None):
    choice = str(pipeline).strip()
    if choice != "8":
        return {}

    directory = Path(weights_dir) if weights_dir is not None else DEFAULT_WEIGHTS_DIR
    resolved = {}
    for asset in PIPELINE_8_ASSETS:
        target = directory / asset.filename
        if not _is_valid_existing_file(target, asset.min_bytes):
            print(f"[model-assets] downloading {asset.filename}")
            _download_atomic(asset, target)
        resolved[asset.key] = target
    return resolved


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pipeline", required=True)
    parser.add_argument("--weights-dir")
    args = parser.parse_args()
    assets = ensure_pipeline_assets(args.pipeline, weights_dir=args.weights_dir)
    for key, path in assets.items():
        print(f"[model-assets] {key}={path}")


if __name__ == "__main__":
    main()
