from __future__ import annotations

import argparse
import os
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WEIGHTS_DIR = PROJECT_ROOT / "weights"

X3D_MODEL_REVISION = "a744b6af7496f0cbfa4f0ba32acd46b65e52d4e1"
RTMO_ARCHIVE_NAME = (
    "rtmo-m_16xb16-600e_body7-640x640-39e78cc4_20231211.zip"
)
RTMO_ONNX_NAME = (
    "rtmo-m_16xb16-600e_body7-640x640-39e78cc4_20231211.onnx"
)
RTMO_ONNX_MIN_BYTES = 10_000_000


@dataclass(frozen=True)
class ModelAsset:
    key: str
    relative_path: str
    url: str
    min_bytes: int
    fallback_urls: tuple[str, ...] = ()

    @property
    def filename(self) -> str:
        return Path(self.relative_path).name


RTMO_ARCHIVE = ModelAsset(
    key="rtmo_archive",
    relative_path=f"rtmo/{RTMO_ARCHIVE_NAME}",
    url=(
        "https://download.openmmlab.com/mmpose/v1/projects/rtmo/onnx_sdk/"
        + RTMO_ARCHIVE_NAME
    ),
    min_bytes=10_000_000,
    fallback_urls=(
        "https://huggingface.co/Tau-J/RTMPose/resolve/main/"
        f"rtmo/onnx_sdk/{RTMO_ARCHIVE_NAME}?download=true",
    ),
)

SCENE_CHECKPOINT = ModelAsset(
    key="scene_checkpoint",
    relative_path="x3d/final_x3d_realtime.pt",
    url=(
        "https://huggingface.co/visionlab-ai/"
        "school-violence-detection-models/resolve/"
        f"{X3D_MODEL_REVISION}/final/final_x3d_realtime.pt?download=true"
    ),
    min_bytes=10_000_000,
)

PIPELINE_8_ASSETS = (RTMO_ARCHIVE, SCENE_CHECKPOINT)


def _is_valid_existing_file(path: Path, min_bytes: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= min_bytes
    except OSError:
        return False


def _download_atomic(asset: ModelAsset, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    urls = (asset.url, *asset.fallback_urls)
    last_error = None

    for index, url in enumerate(urls):
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.part")
        request = urllib.request.Request(
            url,
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
                    f"Downloaded model asset is incomplete: {asset.relative_path}"
                )
            os.replace(temporary, target)
            return
        except Exception as exc:
            last_error = exc
            temporary.unlink(missing_ok=True)
            if index + 1 < len(urls):
                print(
                    f"[model-assets] primary download failed for "
                    f"{asset.relative_path}; trying mirror"
                )

    raise RuntimeError(
        f"Failed to download model asset: {asset.relative_path}"
    ) from last_error


def _extract_rtmo_onnx_atomic(archive: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.part")
    try:
        with zipfile.ZipFile(archive, "r") as bundle:
            candidates = [
                info
                for info in bundle.infolist()
                if not info.is_dir()
                and info.filename.replace("\\", "/").endswith("end2end.onnx")
            ]
            if len(candidates) != 1:
                raise RuntimeError(
                    "RTMO archive must contain exactly one end2end.onnx "
                    f"(found={len(candidates)})"
                )
            member = candidates[0]
            if member.file_size < RTMO_ONNX_MIN_BYTES:
                raise RuntimeError(
                    f"RTMO ONNX member is unexpectedly small: {member.file_size}"
                )
            with bundle.open(member, "r") as source:
                with temporary.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())

        if not _is_valid_existing_file(temporary, RTMO_ONNX_MIN_BYTES):
            raise RuntimeError("Extracted RTMO ONNX model is incomplete")
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

    rtmo_onnx = directory / "rtmo" / RTMO_ONNX_NAME
    if not _is_valid_existing_file(rtmo_onnx, RTMO_ONNX_MIN_BYTES):
        archive = directory / RTMO_ARCHIVE.relative_path
        if not _is_valid_existing_file(archive, RTMO_ARCHIVE.min_bytes):
            print(f"[model-assets] downloading {RTMO_ARCHIVE.relative_path}")
            _download_atomic(RTMO_ARCHIVE, archive)
        print(f"[model-assets] extracting {RTMO_ONNX_NAME}")
        _extract_rtmo_onnx_atomic(archive, rtmo_onnx)
    resolved["rtmo_onnx"] = rtmo_onnx

    scene_target = directory / SCENE_CHECKPOINT.relative_path
    if not _is_valid_existing_file(
        scene_target,
        SCENE_CHECKPOINT.min_bytes,
    ):
        print(
            f"[model-assets] downloading "
            f"{SCENE_CHECKPOINT.relative_path}"
        )
        _download_atomic(SCENE_CHECKPOINT, scene_target)
    resolved["scene_checkpoint"] = scene_target

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
