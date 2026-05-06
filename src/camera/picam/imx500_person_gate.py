"""Tiny IMX500 person check — same inference parsing as marist lab9 ``integrate_vision_pipeline``."""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import Any

from ...core import config
from .session_lock import PICAMERA2_SESSION_LOCK

log = logging.getLogger("picam.imx500_person_gate")

try:
    from picamera2 import Picamera2  # type: ignore[import-not-found]
except ImportError:
    Picamera2 = None  # type: ignore[assignment]

try:
    from picamera2.devices import IMX500  # type: ignore[import-not-found]
    from picamera2.devices.imx500 import NetworkIntrinsics  # type: ignore[import-untyped]
except ImportError:
    IMX500 = None  # type: ignore[assignment]
    NetworkIntrinsics = None  # type: ignore[assignment]


def bindings_ok() -> bool:
    """True when Picamera2 + IMX500 Python bindings load successfully."""
    return Picamera2 is not None and IMX500 is not None and NetworkIntrinsics is not None


def startup_check_failed_reason() -> str | None:
    """If non-empty, edge ``security-system`` startup must abort (bindings or MobilenetSSD .rpk)."""
    if not bindings_ok():
        return (
            "Picamera2 with IMX500 is required "
            "(e.g. python3-picamera2 on Raspberry Pi with Pi AI Camera)"
        )
    p = Path(config.IMX500_RPK_PATH)
    if not p.is_file():
        return (
            f"IMX500 MobilenetSSD model missing at {p} "
            "(install imx500-models on Raspberry Pi OS)"
        )
    return None


def _imx500_rpk_or_raise() -> Path:
    p = Path(config.IMX500_RPK_PATH).resolve(strict=False)
    if not p.is_file():
        raise RuntimeError(f"IMX500 model missing at {p} (install imx500-models)")
    return p


def _parse_frame(
    imx500: Any,
    intr: Any,
    picam2: Any,
    metadata: Any,
    labels: list[str],
    thresh: float,
) -> list[dict[str, Any]]:
    out = imx500.get_outputs(metadata, add_batch=True)
    if out is None:
        return []
    boxes, scores, classes = out[0][0], out[1][0], out[2][0]
    if intr.bbox_normalization:
        _w, h = imx500.get_input_size()
        boxes = boxes / h
    if intr.bbox_order == "xy":
        boxes = boxes[:, [1, 0, 3, 2]]
    ts_ms = int(time.time() * 1000)
    dets: list[dict[str, Any]] = []
    for box, score, cat in zip(boxes, scores, classes):
        sc = float(score)
        if sc < thresh:
            continue
        scaled = imx500.convert_inference_coords(box, metadata, picam2)
        ci = int(cat)
        lab = labels[ci] if labels and ci < len(labels) else str(cat)
        dets.append(
            {
                "label": lab,
                "confidence": sc,
                "bbox": [int(scaled[0]), int(scaled[1]), int(scaled[2]), int(scaled[3])],
                "ts_ms": ts_ms,
            }
        )
    return dets


def capture_jpeg_and_person_seen() -> tuple[bytes, bool, float | None]:
    """One IMX500 session; poll inference until timeout or confident ``person``. Always writes one JPEG.

    Returns:
        ``(jpeg, person_seen, best_person_confidence_or_None)``
    """
    if not bindings_ok():
        raise RuntimeError(
            "IMX500 requires Picamera2 with IMX500 device support (deploy on Raspberry Pi + Pi AI Camera)"
        )

    mp = _imx500_rpk_or_raise()
    thresh = float(config.IMX500_CONFIDENCE_THRESH)
    timeout = max(0.5, float(config.IMX500_VISION_TIMEOUT_SEC))
    warmup = max(0.0, float(config.IMX500_WARMUP_SEC))

    picam2: Any = None
    tmp_path: Path | None = None
    person_seen = False
    best_person: float | None = None
    jpeg_bytes = b""

    with PICAMERA2_SESSION_LOCK:
        imx = IMX500(str(mp))
        intr = imx.network_intrinsics
        if not intr:
            intr = NetworkIntrinsics()
            intr.task = "object detection"
        intr.update_with_defaults()
        labels = list(intr.labels or [])

        picam2 = Picamera2(imx.camera_num)
        cfg = picam2.create_preview_configuration(
            controls={"FrameRate": intr.inference_rate},
            buffer_count=12,
        )
        try:
            imx.show_network_fw_progress_bar()
        except Exception:
            pass
        picam2.start(cfg, show_preview=False)
        try:
            if warmup:
                time.sleep(warmup)
            deadline = time.time() + timeout
            while time.time() < deadline and not person_seen:
                meta = picam2.capture_metadata()
                for d in _parse_frame(imx, intr, picam2, meta, labels, thresh):
                    if str(d.get("label", "")).strip().lower() == "person":
                        cf = float(d["confidence"])
                        best_person = cf if best_person is None else max(best_person, cf)
                        person_seen = True

            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fp:
                tmp_path = Path(fp.name)
            picam2.capture_file(str(tmp_path))
            jpeg_bytes = tmp_path.read_bytes()
            if not jpeg_bytes:
                raise RuntimeError("IMX500 captured empty JPEG")
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

    log.info(
        "imx500 person_gate: jpeg=%sB person_seen=%s best_person_conf=%s",
        len(jpeg_bytes),
        person_seen,
        f"{best_person:.3f}" if best_person is not None else "n/a",
    )
    return jpeg_bytes, person_seen, best_person
