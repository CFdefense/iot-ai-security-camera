"""Publish ``home/security/status`` component rows for camera and serial sensor (avoidance/fake link)."""

from __future__ import annotations

import logging
import threading
from typing import Any

from . import config

log = logging.getLogger("status_publish")


def publish_component_safe(mqtt_service: Any, component: str, state: str) -> None:
    """Call ``publish_component_status`` when the service implements it (tests may omit)."""
    pub = getattr(mqtt_service, "publish_component_status", None)
    if not callable(pub):
        return
    try:
        pub(component, state=state)
    except Exception:
        log.debug("publish_component_status failed", exc_info=True)


def publish_camera_from_probe(mqtt_service: Any) -> None:
    """Set ``camera`` to up/down from Picamera2 / libcamera probe."""
    from ..camera.picam.probe import describe_picamera2

    line = describe_picamera2()
    publish_component_safe(
        mqtt_service,
        "camera",
        "up" if line.startswith("OK ") else "down",
    )


def publish_initial_edge_components(mqtt_service: Any) -> None:
    """Initial camera row; sensor link is published only by ``serial_bridge`` (open=up, close=down).

    Do not publish ``sensor`` here when ``SERIAL_PORT`` is set — a stale ``down`` would race after the
    bridge opens and overwrite merged MQTT state with ``down`` while the banner shows UP.
    """
    publish_camera_from_probe(mqtt_service)
    if not config.SERIAL_PORT:
        publish_component_safe(mqtt_service, "sensor", "unknown")


def run_camera_status_refresh_loop(
    mqtt_service: Any,
    stop_event: threading.Event,
    *,
    interval_sec: float = 45.0,
) -> None:
    """Periodically re-probe the camera so the dashboard tracks hot-plug / driver issues."""
    while not stop_event.wait(interval_sec):
        try:
            publish_camera_from_probe(mqtt_service)
        except Exception:
            log.debug("camera status refresh failed", exc_info=True)
