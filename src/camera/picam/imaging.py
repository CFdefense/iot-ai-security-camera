"""Camera capture (Picamera2) and face embeddings (OpenCV YuNet + SFace)."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

import cv2
import numpy as np

from ...core import config
from .face_embed import embed_face_bgr_uint8

log = logging.getLogger("picam.imaging")

# libcamera allows only one Picamera2 session per device — serialize capture from Flask + serial bridge.
_PICAMERA2_LOCK = threading.Lock()


def capture_frame_jpeg() -> bytes:
    """Capture one frame as JPEG bytes from Picamera2."""
    return _capture_jpeg_via_picamera2()


def _capture_jpeg_via_picamera2() -> bytes:
    """Capture one still JPEG: preview config, short warmup, ``capture_file``."""
    try:
        from picamera2 import Picamera2  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "Picamera2 is required for JPEG capture. On Raspberry Pi OS install e.g. "
            "`sudo apt install python3-picamera2 python3-libcamera`, then recreate the project "
            "venv with `uv venv --system-site-packages && uv sync` so imports match apt (see README).",
        ) from e

    with _PICAMERA2_LOCK:
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
            try:
                picam2.close()
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


def normalize_stored_jpeg(jpeg_bytes: bytes) -> bytes:
    """Resize (max edge) and JPEG re-encode for SQLite blobs — matches registration + alert thumbnails."""
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return jpeg_bytes
    max_edge = max(64, int(config.STORED_JPEG_MAX_EDGE))
    quality = min(100, max(1, int(config.STORED_JPEG_QUALITY)))
    h, w = img.shape[:2]
    m = max(h, w)
    if m > max_edge:
        scale = max_edge / float(m)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, (int(cv2.IMWRITE_JPEG_QUALITY), quality))
    if not ok or buf is None:
        return jpeg_bytes
    return bytes(buf)


def embed_face_bytes(raw: bytes) -> list[float]:
    """128-D face embedding from JPEG bytes (OpenCV SFace; ARM-friendly)."""
    arr = np.frombuffer(raw, dtype=np.uint8)
    image_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image_bgr is None:
        raise ValueError("could not decode image for embedding")
    return embed_face_bgr_uint8(image_bgr)


def embed_face(image_path: Path) -> list[float]:
    """128-d embedding for an on-disk JPEG path."""
    p = Path(image_path)
    raw = p.read_bytes()
    if not raw:
        raw = p.name.encode()
    return embed_face_bytes(raw)
