"""Camera capture + face-embedding hooks (Pi Picamera / local stubs).

Pluggable implementations so REST and proximity detection share one interface.
On a dev laptop stubs return deterministic fake data without hardware.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any

from ...core import config

log = logging.getLogger("picam.imaging")
_PICAM_LOCK = Lock()


@dataclass(frozen=True)
class ObjectDetection:
    """One object detector result associated with a captured frame."""

    label: str
    confidence: float
    bbox: tuple[int, int, int, int] | None  # x, y, width, height
    category: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of the detection."""
        out: dict[str, Any] = {
            "label": self.label,
            "confidence": round(float(self.confidence), 4),
            "bbox": None,
            "category": int(self.category),
        }
        if self.bbox is not None:
            out["bbox"] = {
                "x": int(self.bbox[0]),
                "y": int(self.bbox[1]),
                "width": int(self.bbox[2]),
                "height": int(self.bbox[3]),
            }
        return out


@dataclass(frozen=True)
class CapturedFrame:
    """A JPEG capture plus any detector metadata from the same frame."""

    jpeg: bytes
    detections: list[ObjectDetection]


@lru_cache(maxsize=1)
def _picam_imx500():
    from picamera2 import Picamera2
    from picamera2.devices import IMX500
    from picamera2.devices.imx500 import NetworkIntrinsics

    imx500 = IMX500(config.AI_CAMERA_MODEL)
    intrinsics = imx500.network_intrinsics
    if not intrinsics:
        intrinsics = NetworkIntrinsics()
        intrinsics.task = "object detection"
    intrinsics.update_with_defaults()

    picam2 = Picamera2(imx500.camera_num)
    preview_config = picam2.create_preview_configuration(
        controls={"FrameRate": intrinsics.inference_rate}, buffer_count=12
    )
    imx500.show_network_fw_progress_bar()
    picam2.start(preview_config, show_preview=False)

    return picam2, imx500, intrinsics


def capture_frame_jpeg() -> bytes:
    """Capture one frame as JPEG bytes (in memory; no on-disk ``captures/``).

    ``CAMERA_BACKEND=stub`` returns synthetic JPEG-like bytes for local dev.
    ``CAMERA_BACKEND=picam`` captures from the Raspberry Pi camera via picamera2.
    """
    return capture_frame_with_detections().jpeg


def capture_frame_with_detections() -> CapturedFrame:
    """Capture one JPEG frame and return object detections from the same frame."""
    if config.CAMERA_BACKEND == "picam":
        return _capture_picam_jpeg_and_metadata()
    if config.CAMERA_BACKEND == "stub":
        raw = _capture_stub_jpeg()
        return CapturedFrame(
            jpeg=raw,
            detections=[
                ObjectDetection(
                    label="person",
                    confidence=1.0,
                    bbox=None,
                    category=-1,
                )
            ],
        )
    raise ValueError(f"unsupported CAMERA_BACKEND={config.CAMERA_BACKEND!r}; expected 'stub' or 'picam'")


def _capture_stub_jpeg() -> bytes:
    """Return small JPEG-like bytes so tests/local runs do not need camera hardware."""
    unique = hashlib.sha256(str(time.time_ns()).encode()).digest()[:16]
    raw = b"\xff\xd8\xff\xdb" + unique + b"\xff\xd9"
    log.info("captured stub frame (%s bytes)", len(raw))
    return raw


def _capture_picam_jpeg_and_metadata() -> CapturedFrame:
    """Capture a JPEG frame and detector output from the Raspberry Pi camera."""
    camera, imx500, intrinsics = _picam_imx500()

    buf = BytesIO()
    with _PICAM_LOCK:
        metadata = camera.capture_file(buf, format="jpeg")
        outputs = imx500.get_outputs(metadata, add_batch=True)

    raw = buf.getvalue()
    if not raw:
        raise RuntimeError("picamera2 returned an empty capture")

    detections: list[ObjectDetection] = []
    if outputs:
        boxes, scores, classes = outputs[0][0], outputs[1][0], outputs[2][0]
        labels = intrinsics.labels or []

        if intrinsics.bbox_normalization:
            _, input_h = imx500.get_input_size()
            boxes = boxes / input_h

        if intrinsics.bbox_order == "xy":
            boxes = boxes[:, [1, 0, 3, 2]]

        for box, score, category in zip(boxes, scores, classes):
            category_id = int(category)
            label = labels[category_id] if 0 <= category_id < len(labels) else str(category_id)
            scaled = imx500.convert_inference_coords(box, metadata, camera)
            detections.append(
                ObjectDetection(
                    label=label,
                    confidence=float(score),
                    bbox=tuple(int(x) for x in scaled),
                    category=category_id,
                )
            )

    log.info("captured picam frame (%s bytes, %s detections)", len(raw), len(detections))

    return CapturedFrame(jpeg=raw, detections=detections)


def capture_registration_jpeg() -> bytes:
    """Registration capture; currently uses the same pipeline as detection captures."""
    return capture_frame_jpeg()


def embed_face_bytes(raw: bytes, bbox: tuple[int, int, int, int] | None = None) -> list[float]:
    """Return a 128-d face embedding for raw JPEG bytes."""
    if config.CAMERA_BACKEND == "picam":
        return _embed_picam_face_bytes(raw, bbox)
    if config.CAMERA_BACKEND == "stub":
        return _embed_stub_face_bytes(raw)
    raise ValueError(f"unsupported CAMERA_BACKEND={config.CAMERA_BACKEND!r}; expected 'stub' or 'picam'")


def _embed_stub_face_bytes(raw: bytes) -> list[float]:
    """Return deterministic 128-d stub embeddings derived from image bytes."""
    digest = hashlib.sha512(raw).digest()
    vals: list[float] = []
    while len(vals) < 128:
        digest = hashlib.sha512(digest).digest()
        for b in digest:
            vals.append((b / 255.0) * 2.0 - 1.0)
            if len(vals) >= 128:
                break
    return vals


def _crop_to_bbox(image, bbox: tuple[int, int, int, int], *, padding: float = 0.15):
    image_h, image_w = image.shape[:2]
    x, y, w, h = bbox

    pad_x = int(w * padding)
    pad_y = int(h * padding)

    left = max(0, int(x) - pad_x)
    top = max(0, int(y) - pad_y)
    right = min(image_w, int(x + w) + pad_x)
    bottom = min(image_h, int(y + h) + pad_y)

    if right <= left or bottom <= top:
        raise ValueError("invalid detection bbox")

    return image[top:bottom, left:right]


def _embed_picam_face_bytes(raw: bytes, bbox=None) -> list[float]:
    """Encode exactly one detected face from JPEG bytes using face_recognition."""
    try:
        import face_recognition
    except ImportError as e:
        raise RuntimeError(
            "CAMERA_BACKEND=picam requires face_recognition; install the project's pi extras "
            "or set CAMERA_BACKEND=stub for local development"
        ) from e

    try:
        image = face_recognition.load_image_file(BytesIO(raw))
    except Exception as e:
        raise ValueError(f"invalid image data: {e}") from e

    if bbox is not None:
        image = _crop_to_bbox(image, bbox, padding=0.15)

    locations = face_recognition.face_locations(image, model="hog")
    if not locations:
        raise ValueError("no face detected")
    if len(locations) > 1:
        raise ValueError("multiple faces detected")

    encodings = face_recognition.face_encodings(image, known_face_locations=locations)
    if not encodings:
        raise ValueError("face encoding failed")

    return [float(x) for x in encodings[0]]


def embed_face(image_path: Path) -> list[float]:
    """128-d embedding for an on-disk JPEG path."""
    p = Path(image_path)
    raw = p.read_bytes()
    if not raw:
        raw = p.name.encode()
    return embed_face_bytes(raw)
