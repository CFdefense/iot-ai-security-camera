"""Small helpers shared by Picamera stubs and future Pi implementations."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_capture_timestamp_slug() -> str:
    """Return a filenames-safe UTC timestamp stem (stub + Pi capture naming)."""
    return datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")
