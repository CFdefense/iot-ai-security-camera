"""Proximity / door access check triggered from Arduino serial JSON."""

from __future__ import annotations

from .. import config
from ...mqtt_service import MqttPublisher
from ..persistence import db
from ..picam import imaging
from ..picam.helpers import utc_capture_timestamp_slug


def handle_trigger(mqtt_service: MqttPublisher) -> dict:
    """Run one detection pass against the whitelist MQTT side-effects included."""
    if not mqtt_service.detection_enabled:
        mqtt_service.publish_event("trigger_ignored", {"reason": "detection_disabled"})
        return {"status": "ignored", "reason": "detection_disabled"}

    mqtt_service.publish_event("proximity_detected")

    raw_jpeg = imaging.capture_frame_jpeg()
    cap_ref = f"inline:{utc_capture_timestamp_slug()}"
    try:
        embedding = imaging.embed_face_bytes(raw_jpeg)
    except ValueError as e:
        mqtt_service.publish_event(
            "low_quality_capture",
            {"image_ref": cap_ref, "reason": str(e)},
        )
        return {"status": "low_quality", "image_ref": cap_ref}

    with db.connect() as conn:
        name, sim = db.best_match(conn, embedding)

    if name is not None and sim >= config.MATCH_THRESHOLD:
        mqtt_service.publish_event(
            "access_granted",
            {"user": name, "confidence": round(sim, 4), "image_ref": cap_ref},
        )
        return {"status": "granted", "user": name, "confidence": sim}

    mqtt_service.publish_alert(
        event_type="unknown_face_detected",
        confidence=sim,
        image_ref=cap_ref,
    )
    return {"status": "unknown", "confidence": sim, "image_ref": cap_ref}
