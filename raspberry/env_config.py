from __future__ import annotations

import math
import os


class EnvConfigurationError(RuntimeError):
    pass


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def env_text(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise EnvConfigurationError(f"Required environment variable is missing: {name}")
    return value.strip()


def env_bool(name: str) -> bool:
    raw = env_text(name).lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise EnvConfigurationError(f"Environment variable {name} must be a boolean value")


def env_int(
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = env_text(name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise EnvConfigurationError(
            f"Environment variable {name} must be an integer"
        ) from exc
    if minimum is not None and value < minimum:
        raise EnvConfigurationError(f"Environment variable {name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise EnvConfigurationError(f"Environment variable {name} must be <= {maximum}")
    return value


def env_float(
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = env_text(name)
    try:
        value = float(raw)
    except ValueError as exc:
        raise EnvConfigurationError(
            f"Environment variable {name} must be a number"
        ) from exc
    if not math.isfinite(value):
        raise EnvConfigurationError(
            f"Environment variable {name} must be a finite number"
        )
    if minimum is not None and value < minimum:
        raise EnvConfigurationError(f"Environment variable {name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise EnvConfigurationError(f"Environment variable {name} must be <= {maximum}")
    return value
