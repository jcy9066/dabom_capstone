try:
    from perception.env_config import env_text
except ModuleNotFoundError:  # Direct perception script execution.
    from env_config import env_text


def resolve_cuda_device(device=None):
    cuda_device_index = env_text("CUDA_DEVICE_INDEX")
    requested = device if device is not None and str(device).strip() else env_text("DEVICE")
    resolved = str(requested).strip().lower()

    if resolved == "cuda":
        resolved = f"cuda:{cuda_device_index}"

    if not resolved.startswith("cuda"):
        raise RuntimeError(f"GPU inference requires a CUDA device. CPU is not allowed: {resolved}")

    try:
        import torch
    except Exception as exc:
        raise RuntimeError(f"GPU inference requires PyTorch with CUDA support: {exc}") from exc

    if not torch.cuda.is_available():
        raise RuntimeError("GPU inference requires CUDA, but torch.cuda.is_available() is False")

    index = _cuda_index(resolved, cuda_device_index)
    if index < 0 or index >= torch.cuda.device_count():
        raise RuntimeError(f"CUDA device index out of range: cuda:{index}, available={torch.cuda.device_count()}")

    return f"cuda:{index}"


def _cuda_index(device, fallback_index):
    if ":" not in device:
        return int(fallback_index)
    suffix = device.split(":", 1)[1].strip()
    if not suffix:
        return int(fallback_index)
    try:
        return int(suffix)
    except ValueError as exc:
        raise RuntimeError(f"Invalid CUDA device string: {device}") from exc
