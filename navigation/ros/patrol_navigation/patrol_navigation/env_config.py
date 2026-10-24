import math
import os


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def env_text(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value.strip()


def env_bool(name: str, *, default: bool | None = None) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        if default is not None:
            return default
        raise RuntimeError(f"Required environment variable is missing: {name}")
    raw = value.strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise RuntimeError(f"Environment variable {name} must be a boolean value")


def env_float(name: str, *, minimum: float | None = None) -> float:
    try:
        value = float(env_text(name))
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be a number") from exc
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        raise RuntimeError(f"Environment variable {name} must be finite and >= {minimum}")
    return value
