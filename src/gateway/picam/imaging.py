"""Camera capture + face-embedding hooks (Pi Picamera / local stubs).

Pluggable implementations so REST and proximity detection share one interface.
On a dev laptop stubs return deterministic fake data without hardware.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

from .helpers import utc_capture_timestamp_slug

log = logging.getLogger("picam.imaging")


def capture_frame_jpeg() -> bytes:
    """Capture one frame as JPEG bytes (in memory; no on-disk ``captures/``).

    Stub: minimal synthetic JPEG. On the Pi, replace with ``picamera2`` buffer output.
    """
    unique = hashlib.sha256(str(time.time_ns()).encode()).digest()[:16]
    raw = b"\xff\xd8\xff\xdb" + unique + b"\xff\xd9"
    log.info("captured frame (%s bytes)", len(raw))
    return raw


def capture_registration_jpeg() -> bytes:
    """Registration capture; same pipeline as :func:`capture_frame_jpeg` in the stub."""
    return capture_frame_jpeg()


def embed_face_bytes(raw: bytes) -> list[float]:
    """128-d embedding for raw JPEG bytes (stub hashes; Pi uses ``face_recognition``)."""
    digest = hashlib.sha512(raw).digest()
    vals: list[float] = []
    while len(vals) < 128:
        digest = hashlib.sha512(digest).digest()
        for b in digest:
            vals.append((b / 255.0) * 2.0 - 1.0)
            if len(vals) >= 128:
                break
    return vals


def embed_face(image_path: Path) -> list[float]:
    """128-d embedding for an on-disk JPEG path."""
    p = Path(image_path)
    raw = p.read_bytes()
    if not raw:
        raw = p.name.encode()
    return embed_face_bytes(raw)
