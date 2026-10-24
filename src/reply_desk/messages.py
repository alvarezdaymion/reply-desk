"""Shared interpretation of inbound message metadata."""

from typing import Any


def is_auto_reply(value: Any) -> bool:
    """Recognize only the provider's affirmative auto-reply flags."""
    return value in (True, 1, "1", "true")
