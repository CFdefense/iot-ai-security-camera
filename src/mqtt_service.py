"""MQTT publisher service for the Smart Security Camera.

Publishes to the three topics defined in docs/Architecture.pdf:

  home/security/alerts  - unknown_face_detected (drives mobile notifications)
  home/security/events  - proximity / access_granted / detection_toggle / low_quality_capture
  home/security/status  - 60s heartbeat with uptime + detection state

Run directly to start a heartbeat loop so the broker always sees a live
camera device:

    python -m src.mqtt_service

Other modules (the REST API, the detection loop) import ``MqttService`` and
call ``publish_alert`` / ``publish_event`` when they have something to report.
"""

from __future__ import annotations

import contextlib
import json
import logging
import signal
import threading
import time
from pathlib import Path
from typing import Any, Protocol

import paho.mqtt.client as mqtt

from . import config

log = logging.getLogger("mqtt_service")

_EVENTS_LOG = config.ARTIFACTS_DIR / "mqtt_published.jsonl"


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
    def _publish(self, topic: str, payload: dict[str, Any], qos: int = 0, retain: bool = False) -> None:
        payload.setdefault("topic", topic)
        payload.setdefault("timestamp", _now_iso())
        payload.setdefault("sensor_id", self.sensor_id)
        body = json.dumps(payload, separators=(",", ":"))
        self._client.publish(topic, payload=body, qos=qos, retain=retain)
        _EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _EVENTS_LOG.open("a", encoding="utf-8") as f:
            f.write(body + "\n")
        log.info("pub %s %s", topic, body)

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

    def publish_status(self) -> None:
        """Publish a heartbeat to home/security/status."""
        self._publish(
            config.TOPIC_STATUS,
            {
                "event_type": "heartbeat",
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

        Blocks the calling thread; intended to be run either as the main
        thread (see :func:`main`) or inside a dedicated daemon thread from
        the REST API.

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


def main() -> None:
    """Run the MQTT service standalone: connect + heartbeat until SIGINT/SIGTERM."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    svc = MqttService()
    svc.start()

    stop_event = threading.Event()

    def _handle_signal(signum, frame):
        log.info("received signal %s, stopping", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        svc.run_heartbeat(stop_event=stop_event)
    finally:
        svc.stop()


if __name__ == "__main__":
    main()
