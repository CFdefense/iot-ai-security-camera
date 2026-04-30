"""MQTT publisher implementation (used only from ``security_system``).

Publishes to the three topics defined in docs/Architecture.pdf:

  home/security/alerts  - unknown_face_detected (drives mobile notifications)
  home/security/events  - proximity / access_granted / detection_toggle / low_quality_capture
  home/security/status  - 60s heartbeat with uptime + detection state
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import paho.mqtt.client as mqtt

from ..core import config

log = logging.getLogger("mqtt.service")

_EVENTS_LOG = config.ARTIFACTS_DIR / "mqtt_published.jsonl"
_STATUS_COMPONENT_KEYS = ("mqtt", "camera", "api", "sensor")

# JSON field on ``home/security/status`` for per-service rows (singular key).
STATUS_PAYLOAD_COMPONENT_KEY = "component"


def _component_rows(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract component map; accept legacy ``components`` from older retained messages."""
    raw = payload.get(STATUS_PAYLOAD_COMPONENT_KEY) or payload.get("components")
    return dict(raw) if isinstance(raw, dict) else {}


class MqttPublisher(Protocol):
    """Structural interface satisfied by :class:`MqttService` and test fakes.

    Callers (the Flask API, the detection loop) should depend on this
    protocol instead of the concrete class so tests and alternate transports
    compose cleanly.
    """

    @property
    def detection_enabled(self) -> bool:
        """Whether the detection loop should act on triggers."""
        ...

    def publish_event(self, event_type: str, data: dict[str, Any] | None = ...) -> None:
        """Publish a non-critical event to the events topic."""
        ...

    def publish_alert(
        self,
        *,
        event_type: str,
        confidence: float,
        image_ref: str | Path,
        extra: dict[str, Any] | None = ...,
    ) -> None:
        """Publish an alert to the alerts topic (mobile notifications)."""
        ...

    def set_detection(self, enabled: bool) -> None:
        """Enable or disable the detection loop and announce the change."""
        ...

    def publish_component_status(self, component: str, *, state: str) -> None:
        """Publish a single component row on the status topic (``MqttService`` only)."""
        ...


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class MqttService:
    """Thin wrapper around paho-mqtt tailored to the project's topic plan."""

    def __init__(
        self,
        host: str = config.MQTT_HOST,
        port: int = config.MQTT_PORT,
        client_id: str = config.MQTT_CLIENT_ID,
        sensor_id: str = config.SENSOR_ID,
        *,
        on_publish: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.sensor_id = sensor_id
        self._client = mqtt.Client(client_id=client_id)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._started_ts = time.time()
        self._detection_enabled = True
        self._connected = False
        self._lock = threading.Lock()
        self._on_publish = on_publish
        self._last_status_payload: dict[str, Any] | None = None
        self._last_status_received_at: float | None = None
        self._client.on_message = self._on_message

    @property
    def broker_connected(self) -> bool:
        """True when the MQTT client has an active broker session."""
        return self._connected

    def start(self) -> None:
        """Connect to the broker and start the paho network loop thread.

        Uses ``connect_async`` so a temporarily-unreachable broker doesn't
        crash startup; paho's loop will retry in the background. Publishes
        issued before the broker comes up are silently dropped by paho.
        """
        log.info("connecting to mqtt %s:%s", self.host, self.port)
        try:
            self._client.connect_async(self.host, self.port, keepalive=config.MQTT_KEEPALIVE)
        except Exception as e:
            log.warning("mqtt connect_async failed (%s); will retry in background", e)
        self._client.loop_start()

    def stop(self) -> None:
        """Stop the paho loop and disconnect (idempotent)."""
        self._client.loop_stop()
        with contextlib.suppress(Exception):
            self._client.disconnect()

    def _on_connect(self, client, userdata, flags, rc):
        self._connected = rc == 0
        log.info("mqtt connected rc=%s", rc)
        if rc == 0:
            try:
                client.subscribe(config.TOPIC_STATUS, qos=1)
                log.debug("subscribed %s qos=1 for dashboard snapshot", config.TOPIC_STATUS)
            except Exception:
                log.debug("mqtt subscribe failed", exc_info=True)

    def _on_message(self, client, userdata, msg):
        """Record retained/live JSON from ``home/security/status`` for the dashboard."""
        if msg.topic != config.TOPIC_STATUS:
            return
        try:
            blob = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            log.debug("ignore non-json status payload", exc_info=True)
            return
        if not isinstance(blob, dict):
            return
        self._merge_status_snapshot(blob)

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        log.warning("mqtt disconnected rc=%s", rc)

    def set_detection(self, enabled: bool) -> None:
        """Enable or disable the detection loop and announce the change.

        Publishes a ``detection_toggle`` event on ``home/security/events`` so
        subscribers (logs, dashboards) see the state change in real time.

        Args:
            enabled: ``True`` to turn detection on, ``False`` to pause it.
        """
        with self._lock:
            self._detection_enabled = bool(enabled)
        self.publish_event(
            "detection_toggle",
            {"enabled": self._detection_enabled},
        )

    @property
    def detection_enabled(self) -> bool:
        """Return the current detection-enabled flag (thread-safe)."""
        with self._lock:
            return self._detection_enabled

    # Publish primitives
    def _publish(
        self,
        topic: str,
        payload: dict[str, Any],
        qos: int = 0,
        retain: bool = False,
        *,
        include_sensor_id: bool = True,
    ) -> None:
        payload.setdefault("topic", topic)
        payload.setdefault("timestamp", _now_iso())
        if include_sensor_id:
            payload.setdefault("sensor_id", self.sensor_id)
        else:
            payload.pop("sensor_id", None)
        body = json.dumps(payload, separators=(",", ":"))
        info = self._client.publish(topic, payload=body, qos=qos, retain=retain)
        rc = int(getattr(info, "rc", mqtt.MQTT_ERR_NO_CONN))
        if rc != mqtt.MQTT_ERR_SUCCESS:
            # During startup/reconnect, rc=NO_CONN is expected noise; keep it debug.
            if rc == mqtt.MQTT_ERR_NO_CONN:
                log.debug("drop %s publish rc=%s payload=%s", topic, rc, body)
            else:
                log.warning("drop %s publish rc=%s payload=%s", topic, rc, body)
            return
        _EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _EVENTS_LOG.open("a", encoding="utf-8") as f:
            f.write(body + "\n")
        log.info("pub %s %s", topic, body)
        if topic == config.TOPIC_STATUS:
            self._merge_status_snapshot(dict(payload))
        # SSE notification stream listens on EventHub: only HOME/security/events (alerts + status elsewhere).
        if self._on_publish and topic == config.TOPIC_EVENTS:
            try:
                self._on_publish(topic, dict(payload))
            except Exception:
                log.debug("on_publish hook raised", exc_info=True)

    def publish_alert(
        self,
        *,
        event_type: str,
        confidence: float,
        image_ref: str | Path,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Publish to home/security/alerts (mobile-notification topic).

        Matches the example schema from the Architecture doc: event_type,
        confidence, image_ref, sensor_id, timestamp.
        """
        msg: dict[str, Any] = {
            "event_type": event_type,
            "confidence": round(float(confidence), 4),
            "image_ref": str(image_ref),
        }
        if extra:
            msg.update(extra)
        self._publish(config.TOPIC_ALERTS, msg, qos=1)

    def publish_event(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Publish to home/security/events (log/monitoring topic)."""
        msg: dict[str, Any] = {"event_type": event_type}
        if data:
            msg.update(data)
        self._publish(config.TOPIC_EVENTS, msg, qos=0)

    def _merge_status_snapshot(self, data: dict[str, Any]) -> None:
        with self._lock:
            prev = self._last_status_payload if isinstance(self._last_status_payload, dict) else {}
            merged = dict(prev)
            merged.update(data)

            combined: dict[str, Any] = {}
            combined.update(_component_rows(prev))
            combined.update(_component_rows(data))
            if combined:
                merged[STATUS_PAYLOAD_COMPONENT_KEY] = combined
            merged.pop("components", None)

            self._last_status_payload = merged
            self._last_status_received_at = time.time()

    def _status_state(self, ok: bool | None) -> str:
        if ok is True:
            return "up"
        if ok is False:
            return "down"
        return "unknown"

    def publish_component_status(self, component: str, *, state: str) -> None:
        """Publish a component heartbeat payload to ``home/security/status``."""
        if component not in _STATUS_COMPONENT_KEYS:
            raise ValueError(f"unsupported status component: {component}")
        normalized = (state or "").strip().lower()
        if normalized not in {"up", "down", "unknown"}:
            raise ValueError("state must be one of: up, down, unknown")
        self._publish(
            config.TOPIC_STATUS,
            {
                "event_type": "heartbeat",
                STATUS_PAYLOAD_COMPONENT_KEY: {
                    component: {
                        "state": normalized,
                    }
                },
            },
            qos=1,
            retain=True,
            include_sensor_id=False,
        )

    def dashboard_status_bundle(self) -> dict[str, Any]:
        """Return latest status snapshot details for ``/dashboard/status.json``."""
        with self._lock:
            payload = self._last_status_payload
            connected = self._connected
            detection_enabled = self._detection_enabled
            started_ts = self._started_ts
        payload_out: dict[str, Any] | None = dict(payload) if isinstance(payload, dict) else None
        if payload_out is None:
            payload_out = {
                "event_type": "heartbeat",
                "sensor_id": self.sensor_id,
            }
        # Prefer live in-process state so startup race/stale retained payload
        # does not falsely show MQTT as offline in the dashboard.
        payload_out["connected"] = connected
        payload_out["detection_enabled"] = detection_enabled
        payload_out["uptime_sec"] = int(time.time() - started_ts)
        merged_components: dict[str, Any] = {}
        merged_components.update(_component_rows(payload_out))
        merged_components["mqtt"] = {"state": self._status_state(connected)}
        payload_out[STATUS_PAYLOAD_COMPONENT_KEY] = merged_components
        payload_out.pop("components", None)
        stamp = payload.get("timestamp") if isinstance(payload, dict) else None
        last_at = stamp if isinstance(stamp, str) else None
        return {
            "last_status_at": last_at,
            "status_topic": config.TOPIC_STATUS,
            "status_payload": payload_out,
        }

    def publish_status(self) -> None:
        """Publish a heartbeat to home/security/status."""
        self._publish(
            config.TOPIC_STATUS,
            {
                "event_type": "heartbeat",
                STATUS_PAYLOAD_COMPONENT_KEY: {
                    "mqtt": {
                        "state": self._status_state(self._connected),
                    }
                },
                "uptime_sec": int(time.time() - self._started_ts),
                "detection_enabled": self.detection_enabled,
                "connected": self._connected,
            },
            retain=True,
        )

    def run_heartbeat(
        self,
        interval_sec: int = config.HEARTBEAT_INTERVAL_SEC,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Publish a heartbeat to ``home/security/status`` every ``interval_sec``.

        Blocks the calling thread; intended to be run inside a dedicated daemon
        thread from ``security_system``.

        Args:
            interval_sec: Seconds between heartbeats. Defaults to
                :data:`config.HEARTBEAT_INTERVAL_SEC` (60s per the arch doc).
            stop_event: Optional event that, when set, cleanly breaks the loop.
        """
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            try:
                self.publish_status()
            except Exception as e:  # broker may be briefly unavailable
                log.warning("heartbeat publish failed: %s", e)
            stop_event.wait(interval_sec)
