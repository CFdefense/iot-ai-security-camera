"""Camera capture + face-embedding hooks.

These are pluggable so the REST API and the detection loop can import a
stable interface even though the real implementations require hardware
(Pi AI Camera + face_recognition library). On a dev laptop the stubs return
deterministic fake data so the rest of the system is still exercisable.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

from . import config

log = logging.getLogger("capture")


def capture_image() -> Path:
    """Trigger the Pi camera and return the path to the saved JPEG.

    Replace this body with a ``picamera2`` call on the Pi. The stub just
    creates an empty file under captures/ so downstream code has a valid
    image_ref to publish.
    """
    ts = time.strftime("%Y-%m-%d_%H%M%S", time.gmtime())
    out = config.CAPTURES_DIR / f"{ts}.jpg"
    out.touch(exist_ok=True)
    log.info("captured %s", out)
    return out


def embed_face(image_path: Path) -> list[float]:
    """Return a 128-dim face embedding for the given image.

    Real implementation (on the Pi)::

        import face_recognition
        img = face_recognition.load_image_file(image_path)
        encs = face_recognition.face_encodings(img)
        if not encs:
            raise ValueError("no face detected")
        return encs[0].tolist()

    The stub hashes the file bytes to produce 128 stable pseudo-random floats
    so tests and local runs don't require the heavy dependency.
    """
    digest = hashlib.sha512(Path(image_path).read_bytes() or image_path.name.encode()).digest()
    vals: list[float] = []
    while len(vals) < 128:
        digest = hashlib.sha512(digest).digest()
        for b in digest:
            vals.append((b / 255.0) * 2.0 - 1.0)
            if len(vals) >= 128:
                break
    return vals
