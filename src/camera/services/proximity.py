"""Proximity / door access check triggered from Arduino serial JSON."""

from __future__ import annotations

from ...core import config
from ...data import db
from ...mqtt_service import MqttPublisher
from ..picam import imaging
from ..picam.helpers import utc_capture_timestamp_slug


def _is_person_detection(detection: imaging.ObjectDetection) -> bool:
    return detection.confidence >= config.CAMERA_DETECTION_THRESHOLD and detection.label.strip().lower() == "person"


def handle_trigger(mqtt_service: MqttPublisher) -> dict:
    """Run one detection pass against the whitelist MQTT side-effects included."""
    if not mqtt_service.detection_enabled:
        mqtt_service.publish_event("trigger_ignored", {"reason": "detection_disabled"})
        return {"status": "ignored", "reason": "detection_disabled"}

    mqtt_service.publish_event("proximity_detected")

    capture = imaging.capture_frame_with_detections()
    raw_jpeg = capture.jpeg
    cap_ref = f"inline:{utc_capture_timestamp_slug()}"

    person_detections = [detection for detection in capture.detections if _is_person_detection(detection)]

    if not person_detections:
        reason = "no person detected"
        with db.connect() as conn:
            alert_id = db.record_detection_alert(
                conn,
                event_type="proximity_alert",
                outcome="ignored",
                image_ref=cap_ref,
                capture_image=raw_jpeg,
                reason=reason,
            )

        mqtt_service.publish_event(
            "proximity_alert",
            {
                "outcome": "ignored",
                "image_ref": cap_ref,
                "reason": reason,
            },
        )

        return {
            "status": "ignored",
            "reason": reason,
            "image_ref": cap_ref,
            "alert_id": alert_id,
        }

    embeddings = []
    embed_errors = []
    for detection in person_detections:
        try:
            if detection.bbox is None:
                embedding = imaging.embed_face_bytes(raw_jpeg)
            else:
                embedding = imaging.embed_face_bytes(raw_jpeg, detection.bbox)
            embeddings.append(embedding)
        except ValueError as e:
            embed_errors.append(str(e))

    if not embeddings:
        reason = "; ".join(embed_errors) if embed_errors else "no face detected"
        with db.connect() as conn:
            alert_id = db.record_detection_alert(
                conn,
                event_type="low_quality_capture",
                outcome="low_quality",
                image_ref=cap_ref,
                capture_image=raw_jpeg,
                reason=reason,
            )
        mqtt_service.publish_event(
            "low_quality_capture",
            {"image_ref": cap_ref, "reason": reason},
        )
        return {"status": "low_quality", "image_ref": cap_ref, "alert_id": alert_id}

    best_sim = 0.0
    with db.connect() as conn:
        for embedding in embeddings:
            name, sim = db.best_match(conn, embedding)
            best_sim = max(best_sim, sim)
            if name is not None and sim >= config.MATCH_THRESHOLD:
                alert_id = db.record_detection_alert(
                    conn,
                    event_type="access_granted",
                    outcome="granted",
                    confidence=sim,
                    image_ref=cap_ref,
                    capture_image=raw_jpeg,
                    matched_user_name=name,
                )

                mqtt_service.publish_event(
                    "access_granted",
                    {"user": name, "confidence": round(sim, 4), "image_ref": cap_ref},
                )
                return {"status": "granted", "user": name, "confidence": sim, "alert_id": alert_id}

        alert_id = db.record_detection_alert(
            conn,
            event_type="unknown_face_detected",
            outcome="unknown",
            confidence=best_sim,
            image_ref=cap_ref,
            capture_image=raw_jpeg,
        )

    mqtt_service.publish_alert(
        event_type="unknown_face_detected",
        confidence=best_sim,
        image_ref=cap_ref,
    )
    return {"status": "unknown", "confidence": best_sim, "image_ref": cap_ref, "alert_id": alert_id}
