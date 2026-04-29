"""OpenCV YuNet + SFace embeddings (ARM-friendly; no dlib / face_recognition)."""

from __future__ import annotations

import logging
import os
import urllib.request
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("picam.face_embed")

_EMBEDDING_DIM = 128

_YUNET_NAME = "face_detection_yunet_2023mar.onnx"
_SFACE_NAME = "face_recognition_sface_2021dec.onnx"

_YUNET_URL = f"https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/{_YUNET_NAME}"
_SFACE_URL = f"https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/{_SFACE_NAME}"


def _default_model_dir() -> Path:
    raw = os.environ.get("FACE_MODEL_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parent / "models"


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("downloading %s -> %s", url, dest)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "iot-ai-security-camera/face_embed"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        dest.write_bytes(resp.read())


def ensure_face_models(model_dir: Path | None = None) -> tuple[Path, Path]:
    """Return paths to YuNet and SFace ONNX files, downloading if missing."""
    base = model_dir or _default_model_dir()
    yunet = base / _YUNET_NAME
    sface = base / _SFACE_NAME
    if not yunet.is_file():
        _download(_YUNET_URL, yunet)
    if not sface.is_file():
        _download(_SFACE_URL, sface)
    return yunet, sface


def _create_face_detector(yunet_path: Path):
    """Build YuNet :class:`cv2.FaceDetectorYN` (OpenCV 4.7+)."""
    fd_cls = getattr(cv2, "FaceDetectorYN", None)
    if fd_cls is None:
        raise RuntimeError(
            "cv2.FaceDetectorYN not available — install opencv-python-headless>=4.9",
        )
    create = getattr(fd_cls, "create", None)
    if create is None:
        raise RuntimeError("FaceDetectorYN.create missing — upgrade OpenCV")
    return create(str(yunet_path), "", (320, 320))


def _create_face_recognizer(sface_path: Path):
    """Build SFace :class:`cv2.FaceRecognizerSF`."""
    fr_cls = getattr(cv2, "FaceRecognizerSF", None)
    if fr_cls is None:
        legacy = getattr(cv2, "FaceRecognizerSF_create", None)
        if legacy is None:
            raise RuntimeError(
                "cv2.FaceRecognizerSF not available — install opencv-python-headless>=4.9",
            )
        return legacy(str(sface_path), "")
    create = getattr(fr_cls, "create", None)
    if create is None:
        raise RuntimeError("FaceRecognizerSF.create missing — upgrade OpenCV")
    return create(str(sface_path), "")


@lru_cache(maxsize=1)
def _detector_singleton(yunet_path_str: str):
    return _create_face_detector(Path(yunet_path_str))


@lru_cache(maxsize=1)
def _recognizer_singleton(sface_path_str: str):
    return _create_face_recognizer(Path(sface_path_str))


def _ensure_bgr_three_channel(image_bgr: np.ndarray) -> np.ndarray:
    if image_bgr.ndim == 2:
        return cv2.cvtColor(image_bgr, cv2.COLOR_GRAY2BGR)
    if image_bgr.shape[2] == 4:
        return cv2.cvtColor(image_bgr, cv2.COLOR_BGRA2BGR)
    return image_bgr


def _pick_best_face(faces: np.ndarray) -> np.ndarray:
    """Return one face row (1x15); prefer highest detector confidence when present."""
    if faces.ndim == 1:
        return faces.reshape(1, -1)
    if faces.shape[0] == 1:
        return faces
    # YuNet: column 14 is typically detection score (opencv_zoo demos use [:, -1])
    if faces.shape[1] >= 15:
        scores = faces[:, -1]
        i = int(np.argmax(scores))
        return faces[i : i + 1, :]
    i = int(np.argmax(faces[:, 2] * faces[:, 3]))
    return faces[i : i + 1, :]


def embed_face_bgr_uint8(image_bgr: np.ndarray) -> list[float]:
    """Return a 128-D face embedding from a BGR uint8 image (OpenCV SFace)."""
    img = _ensure_bgr_three_channel(image_bgr)
    if img.size == 0:
        raise ValueError("empty image")

    yunet_path, sface_path = ensure_face_models()
    detector = _detector_singleton(str(yunet_path))
    recognizer = _recognizer_singleton(str(sface_path))

    h, w = img.shape[:2]
    detector.setInputSize((w, h))
    _retval, faces = detector.detect(img)

    if faces is None or (hasattr(faces, "size") and faces.size == 0):
        raise ValueError("no face detected")

    faces = np.asarray(faces, dtype=np.float32)
    if faces.ndim == 1:
        faces = faces.reshape(1, -1)
    if faces.shape[0] == 0:
        raise ValueError("no face detected")

    face_row = _pick_best_face(faces)[0]

    aligned = recognizer.alignCrop(img, face_row)
    if aligned is None or aligned.size == 0:
        raise ValueError("no face detected")

    feat = recognizer.feature(aligned)
    vec = np.asarray(feat, dtype=np.float64).reshape(-1)
    if vec.size != _EMBEDDING_DIM:
        raise ValueError(f"unexpected embedding length {vec.size} (expected {_EMBEDDING_DIM})")
    return vec.tolist()
