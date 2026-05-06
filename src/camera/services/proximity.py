"""Proximity / door access check triggered from Arduino serial JSON."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ...core import config
from ...core.task_logging import TASK_LEVEL
from ...data import db
from ...mqtt import MqttPublisher
from ..picam import imaging, imx500_person_gate as imx_gate
from ..picam.helpers import utc_capture_timestamp_slug

log = logging.getLogger("proximity")


def _extract_detection_hint(trigger_event: Mapping[str, Any] | None) -> tuple[str | None, float | None]:
    """Optional class + score only from the trigger's ``detection`` mapping (``object`` + ``score`` keys)."""
    if not trigger_event:
        return None, None

    nested = trigger_event.get("detection")
    if not isinstance(nested, Mapping):
        return None, None

    raw_object = nested.get("object")
    detected_object: str | None = None
    if raw_object is not None:
        s = str(raw_object).strip()
        if s:
            detected_object = s.lower()

    raw_score = nested.get("score")
    detected_score: float | None = None
    if raw_score is not None:
        try:
            detected_score = float(raw_score)
        except (TypeError, ValueError):
            detected_score = None

    return detected_object, detected_score


def handle_trigger(mqtt_service: MqttPublisher, trigger_event: Mapping[str, Any] | None = None) -> dict:
    """Run one detection pass against the whitelist MQTT side-effects included."""
    if not mqtt_service.detection_enabled:
        mqtt_service.publish_event("trigger_ignored", {"reason": "detection_disabled"})
        log.log(TASK_LEVEL, "detection: ignored (service paused)")
        return {"status": "ignored", "reason": "detection_disabled"}

    mqtt_service.publish_event("proximity_detected")

    imx_blocked_no_person = False
    imx_person_conf: float | None = None
    raw_jpeg, saw_person, imx_person_conf = imx_gate.capture_jpeg_and_person_seen()
    imaging.persist_detection_capture(raw_jpeg)
    if not saw_person:
        imx_blocked_no_person = True

    stored_jpeg = imaging.normalize_stored_jpeg(raw_jpeg)
    cap_ref = f"inline:{utc_capture_timestamp_slug()}"
    if imx_blocked_no_person:
        with db.connect() as conn:
            alert_id = db.record_detection_alert(
                conn,
                event_type="imx500_no_person",
                outcome="skipped_no_person",
                confidence=imx_person_conf,
                image_ref=cap_ref,
                capture_image=stored_jpeg,
                reason="IMX500 did not detect a person above threshold",
            )
        mqtt_service.publish_event(
            "imx500_no_person",
            {
                "image_ref": cap_ref,
                "threshold": float(config.IMX500_CONFIDENCE_THRESH),
                "best_person_confidence": imx_person_conf,
            },
        )
        log.log(TASK_LEVEL, "detection: imx500_no_person (alert_id=%s)", alert_id)
        return {
            "status": "skipped_no_person",
            "reason": "imx500_no_person",
            "image_ref": cap_ref,
            "alert_id": alert_id,
        }

    detected_object, detection_score = _extract_detection_hint(trigger_event)
    if detected_object is not None and detected_object != "person":
        with db.connect() as conn:
            alert_id = db.record_detection_alert(
                conn,
                event_type="non_person_ignored",
                outcome="ignored_non_person",
                confidence=detection_score,
                image_ref=cap_ref,
                capture_image=stored_jpeg,
                reason=f"detector reported {detected_object}",
            )
        mqtt_service.publish_event(
            "non_person_ignored",
            {"image_ref": cap_ref, "object": detected_object, "score": detection_score},
        )
        log.log(
            TASK_LEVEL,
            "detection: ignored_non_person object=%s score=%s alert_id=%s",
            detected_object,
            f"{detection_score:.3f}" if detection_score is not None else "n/a",
            alert_id,
        )
        return {
            "status": "ignored_non_person",
            "object": detected_object,
            "score": detection_score,
            "image_ref": cap_ref,
            "alert_id": alert_id,
        }
    if (
        detected_object == "person"
        and detection_score is not None
        and detection_score < config.PERSON_DETECTION_MIN_SCORE
    ):
        with db.connect() as conn:
            alert_id = db.record_detection_alert(
                conn,
                event_type="low_person_score_ignored",
                outcome="ignored_low_person_score",
                confidence=detection_score,
                image_ref=cap_ref,
                capture_image=stored_jpeg,
                reason="detector score below person threshold",
            )
        mqtt_service.publish_event(
            "low_person_score_ignored",
            {
                "image_ref": cap_ref,
                "object": detected_object,
                "score": detection_score,
                "threshold": config.PERSON_DETECTION_MIN_SCORE,
            },
        )
        log.log(
            TASK_LEVEL,
            "detection: ignored_low_person_score score=%.3f threshold=%.3f alert_id=%s",
            detection_score,
            config.PERSON_DETECTION_MIN_SCORE,
            alert_id,
        )
        return {
            "status": "ignored_low_person_score",
            "object": detected_object,
            "score": detection_score,
            "threshold": config.PERSON_DETECTION_MIN_SCORE,
            "image_ref": cap_ref,
            "alert_id": alert_id,
        }
    try:
        embedding = imaging.embed_face_bytes(raw_jpeg)
    except ValueError as e:
        detail = str(e)
        reason = f"YuNet/SFace embedding: {detail}"
        with db.connect() as conn:
            alert_id = db.record_detection_alert(
                conn,
                event_type="low_quality_capture",
                outcome="low_quality",
                image_ref=cap_ref,
                capture_image=stored_jpeg,
                reason=reason,
            )
        mqtt_service.publish_event(
            "low_quality_capture",
            {
                "image_ref": cap_ref,
                "component": "yunet_sface",
                "reason": detail,
            },
        )
        log.log(
            TASK_LEVEL,
            "detection: low_quality [YuNet/SFace embedding, IMX500 already saw a person] alert_id=%s: %s",
            alert_id,
            detail,
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
        log.log(TASK_LEVEL, "detection: granted user='%s' score=%.3f alert_id=%s", name, sim, alert_id)
        return {"status": "granted", "user": name, "confidence": sim, "alert_id": alert_id}

    mqtt_service.publish_alert(
        event_type="unknown_face_detected",
        confidence=sim,
        image_ref=cap_ref,
    )
    log.log(TASK_LEVEL, "detection: unknown score=%.3f alert_id=%s", sim, alert_id)
    return {"status": "unknown", "confidence": sim, "image_ref": cap_ref, "alert_id": alert_id}
