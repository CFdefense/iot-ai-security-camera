"""Camera capture (Picamera2) and face embeddings (OpenCV + ``face_recognition``)."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

import cv2
import face_recognition
import numpy as np

log = logging.getLogger("picam.imaging")


def capture_frame_jpeg() -> bytes:
    """Capture one frame as JPEG bytes from Picamera2."""
    return _capture_jpeg_via_picamera2()


def _capture_jpeg_via_picamera2() -> bytes:
    """Capture one still JPEG: preview config, short warmup, ``capture_file``."""
    try:
        from picamera2 import Picamera2
    except ImportError as e:
        raise RuntimeError(
            "Picamera2 is required for JPEG capture. On Raspberry Pi OS install e.g. "
            "`sudo apt install python3-picamera2 python3-libcamera` (see README); use a venv "
            "that can import system packages if needed.",
        ) from e

    picam2 = Picamera2()
    cfg = picam2.create_preview_configuration(
        controls={"FrameRate": 30},
        buffer_count=4,
    )
    picam2.start(cfg, show_preview=False)
    tmp_path: Path | None = None
    try:
        time.sleep(2.0)  # exposure settle
        with NamedTemporaryFile(suffix=".jpg", delete=False) as fp:
            tmp_path = Path(fp.name)
        picam2.capture_file(str(tmp_path))
        raw = tmp_path.read_bytes()
        log.info("captured frame via Picamera2 (%s bytes)", len(raw))
        return raw
    finally:
        try:
            picam2.stop()
        except Exception:
            pass
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def capture_registration_jpeg() -> bytes:
    """Registration capture — same Picamera pipeline as proximity detection."""
    return capture_frame_jpeg()


def embed_face_bytes(raw: bytes) -> list[float]:
    """128‑d embedding for JPEG bytes via ``face_recognition``."""
    arr = np.frombuffer(raw, dtype=np.uint8)
    image_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image_bgr is None:
        raise ValueError("could not decode image for embedding")
    image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)

    boxes = face_recognition.face_locations(image_rgb, model="hog")
    if not boxes:
        raise ValueError("no face detected")
    vectors = face_recognition.face_encodings(image_rgb, known_face_locations=boxes)
    if not vectors:
        raise ValueError("no face embedding produced")
    return vectors[0].astype(float).tolist()


def embed_face(image_path: Path) -> list[float]:
    """128-d embedding for an on-disk JPEG path."""
    p = Path(image_path)
    raw = p.read_bytes()
    if not raw:
        raw = p.name.encode()
    return embed_face_bytes(raw)
