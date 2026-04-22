"""Detection flow triggered by the Arduino when proximity and lighting pass.

Publishes events and alerts according to the match result. Kept intentionally
small so it is easy to unit-test. The real trigger will come from a serial
read from the Arduino; for now this module exposes a ``handle_trigger()``
function that the serial bridge (or a test) can call.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import capture, config, db
from .mqtt_service import MqttPublisher

log = logging.getLogger("detection")


def handle_trigger(mqtt_service: MqttPublisher) -> dict:
    """Run one detection pass.

    Side effects: captures an image, queries the whitelist, publishes either
    an ``access_granted`` event or an ``unknown_face_detected`` alert.

    Args:
        mqtt_service: Connected MQTT publisher used for events and alerts.

    Returns:
        A dict describing what happened (status, optional user/confidence,
        image_ref) so callers and tests can assert on the outcome.
    """
    if not mqtt_service.detection_enabled:
        mqtt_service.publish_event("trigger_ignored", {"reason": "detection_disabled"})
        return {"status": "ignored", "reason": "detection_disabled"}

    mqtt_service.publish_event("proximity_detected")

    image_path: Path = capture.capture_image()
    try:
        embedding = capture.embed_face(image_path)
    except ValueError as e:
        mqtt_service.publish_event(
            "low_quality_capture",
            {"image_ref": str(image_path), "reason": str(e)},
        )
        return {"status": "low_quality", "image_ref": str(image_path)}

    with db.connect() as conn:
        name, sim = db.best_match(conn, embedding)

    if name is not None and sim >= config.MATCH_THRESHOLD:
        mqtt_service.publish_event(
            "access_granted",
            {"user": name, "confidence": round(sim, 4), "image_ref": str(image_path)},
        )
        return {"status": "granted", "user": name, "confidence": sim}

    mqtt_service.publish_alert(
        event_type="unknown_face_detected",
        confidence=sim,
        image_ref=image_path,
    )
    return {"status": "unknown", "confidence": sim, "image_ref": str(image_path)}
