"""Proximity / door access check triggered from Arduino serial JSON."""

from __future__ import annotations

from ...mqtt import MqttPublisher
from ...core import config
from ...data import db
from ..picam import imaging
from ..picam.helpers import utc_capture_timestamp_slug


def handle_trigger(mqtt_service: MqttPublisher) -> dict:
    """Run one detection pass against the whitelist MQTT side-effects included."""
    if not mqtt_service.detection_enabled:
        mqtt_service.publish_event("trigger_ignored", {"reason": "detection_disabled"})
        return {"status": "ignored", "reason": "detection_disabled"}

    mqtt_service.publish_event("proximity_detected")

    raw_jpeg = imaging.capture_frame_jpeg()
    stored_jpeg = imaging.normalize_stored_jpeg(raw_jpeg)
    cap_ref = f"inline:{utc_capture_timestamp_slug()}"
    try:
        embedding = imaging.embed_face_bytes(raw_jpeg)
    except ValueError as e:
        with db.connect() as conn:
            alert_id = db.record_detection_alert(
                conn,
                event_type="low_quality_capture",
                outcome="low_quality",
                image_ref=cap_ref,
                capture_image=stored_jpeg,
                reason=str(e),
            )
        mqtt_service.publish_event(
            "low_quality_capture",
            {"image_ref": cap_ref, "reason": str(e)},
        )
        return {"status": "low_quality", "image_ref": cap_ref, "alert_id": alert_id}

    with db.connect() as conn:
        name, sim = db.best_match(conn, embedding)
        if name is not None and sim >= config.MATCH_THRESHOLD:
            alert_id = db.record_detection_alert(
                conn,
                event_type="access_granted",
                outcome="granted",
                confidence=sim,
                image_ref=cap_ref,
                capture_image=stored_jpeg,
                matched_user_name=name,
            )
        else:
            alert_id = db.record_detection_alert(
                conn,
                event_type="unknown_face_detected",
                outcome="unknown",
                confidence=sim,
                image_ref=cap_ref,
                capture_image=stored_jpeg,
            )

    if name is not None and sim >= config.MATCH_THRESHOLD:
        mqtt_service.publish_event(
            "access_granted",
            {"user": name, "confidence": round(sim, 4), "image_ref": cap_ref},
        )
        return {"status": "granted", "user": name, "confidence": sim, "alert_id": alert_id}

    mqtt_service.publish_alert(
        event_type="unknown_face_detected",
        confidence=sim,
        image_ref=cap_ref,
    )
    return {"status": "unknown", "confidence": sim, "image_ref": cap_ref, "alert_id": alert_id}
