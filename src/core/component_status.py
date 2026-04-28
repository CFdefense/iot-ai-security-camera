"""Structured health snapshot for the dashboard."""

from __future__ import annotations

from typing import Any

from flask import Flask

from . import config


def build_dashboard_status(app: Flask) -> dict[str, Any]:
    """Return JSON for ``/dashboard/status.json`` (MQTT status topic timestamps only).

    Uses :meth:`mqtt_service.MqttService.dashboard_status_bundle` when the MQTT client is present.
    """
    mq = app.config.get("mqtt")
    if mq is None:
        return {"last_status_at": None, "status_topic": config.TOPIC_STATUS, "status_payload": None}

    getter = getattr(mq, "dashboard_status_bundle", None)
    if callable(getter):
        return getter()  # type: ignore

    return {"last_status_at": None, "status_topic": config.TOPIC_STATUS, "status_payload": None}
