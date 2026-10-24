"""Safe mapping from current model labels to the existing database taxonomy."""

ASSAULT_LABELS = frozenset({"PUNCHING", "KICKING", "PUSHING", "VIOLENCE"})


def vision_event_type(label: object) -> str | None:
    normalized = str(label or "").strip().upper()
    return "ASSAULT" if normalized in ASSAULT_LABELS else None


def vision_alert_type(label: object) -> str:
    """Return an alert key without forcing an unsupported database enum."""
    normalized = str(label or "").strip().upper()
    return vision_event_type(normalized) or normalized or "VISION_DANGER"
