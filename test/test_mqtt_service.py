import json
import threading
import time

import pytest

from src import config, mqtt_service


class FakeMqttClient:
    """Minimal stand-in for paho.mqtt.client.Client for unit tests."""

    instances: list["FakeMqttClient"] = []

    def __init__(self, client_id=None, *args, **kwargs):
        self.client_id = client_id
        self.on_connect = None
        self.on_disconnect = None
        self.host: str | None = None
        self.port: int | None = None
        self.published: list[dict] = []
        self.loop_running = False
        FakeMqttClient.instances.append(self)

    def connect(self, host, port, keepalive=60):
        """Pretend to connect and fire the on_connect callback immediately with rc=0."""
        self.host, self.port = host, port
        if self.on_connect:
            self.on_connect(self, None, {}, 0)

    def connect_async(self, host, port, keepalive=60):
        """Async variant used by MqttService.start(); behaves like connect() in tests."""
        self.connect(host, port, keepalive)

    def disconnect(self):
        """Invoke on_disconnect (if set) to match paho's lifecycle semantics."""
        if self.on_disconnect:
            self.on_disconnect(self, None, 0)

    def loop_start(self):
        """Record that the background loop thread would have started."""
        self.loop_running = True

    def loop_stop(self):
        """Record that the background loop thread would have stopped."""
        self.loop_running = False

    def publish(self, topic, payload=None, qos=0, retain=False):
        """Capture the publish call so tests can assert on topic/payload/qos/retain."""
        self.published.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})


@pytest.fixture
def svc(monkeypatch, tmp_path):
    """MqttService wired to FakeMqttClient and a tmp published-events log."""
    FakeMqttClient.instances.clear()
    monkeypatch.setattr(mqtt_service.mqtt, "Client", FakeMqttClient)
    monkeypatch.setattr(mqtt_service, "_EVENTS_LOG", tmp_path / "pub.jsonl")
    s = mqtt_service.MqttService(host="test", port=1234, client_id="t")
    s.start()
    yield s
    s.stop()


def _last_payload(svc_):
    """Return (message_dict, decoded_payload) for the most recent publish on the fake client."""
    msg = FakeMqttClient.instances[0].published[-1]
    return msg, json.loads(msg["payload"])


def test_start_connects_and_starts_loop(svc):
    """start() must call paho connect() with the configured host/port and kick off the background loop."""
    client = FakeMqttClient.instances[0]
    assert client.host == "test"
    assert client.port == 1234
    assert client.loop_running is True


def test_publish_alert_matches_architecture_schema(svc):
    """publish_alert must emit QoS 1 on TOPIC_ALERTS with the exact keys given in Architecture section 3."""
    svc.publish_alert(
        event_type="unknown_face_detected",
        confidence=0.34,
        image_ref="/captures/2026-04-14_183205.jpg",
    )
    msg, payload = _last_payload(svc)
    assert msg["topic"] == config.TOPIC_ALERTS
    assert msg["qos"] == 1
    assert payload["topic"] == config.TOPIC_ALERTS
    assert payload["event_type"] == "unknown_face_detected"
    assert payload["confidence"] == 0.34
    assert payload["image_ref"] == "/captures/2026-04-14_183205.jpg"
    assert payload["sensor_id"] == config.SENSOR_ID
    assert "timestamp" in payload


def test_publish_event_uses_events_topic(svc):
    """publish_event routes generic events to TOPIC_EVENTS and merges caller-supplied data fields."""
    svc.publish_event("proximity_detected", {"distance_cm": 15})
    msg, payload = _last_payload(svc)
    assert msg["topic"] == config.TOPIC_EVENTS
    assert payload["event_type"] == "proximity_detected"
    assert payload["distance_cm"] == 15


def test_set_detection_toggles_flag_and_publishes(svc):
    """set_detection must both mutate the in-memory flag and announce the change on TOPIC_EVENTS."""
    svc.set_detection(False)
    assert svc.detection_enabled is False
    _, payload = _last_payload(svc)
    assert payload["event_type"] == "detection_toggle"
    assert payload["enabled"] is False


def test_publish_status_is_retained_heartbeat(svc):
    """publish_status emits a retained heartbeat on TOPIC_STATUS so late subscribers see current state."""
    svc.publish_status()
    msg, payload = _last_payload(svc)
    assert msg["topic"] == config.TOPIC_STATUS
    assert msg["retain"] is True
    assert payload["event_type"] == "heartbeat"
    assert payload["detection_enabled"] is True
    assert isinstance(payload["uptime_sec"], int)


def test_published_messages_are_logged_to_jsonl(svc):
    """Every publish must also be persisted to _EVENTS_LOG (artifacts/mqtt_published.jsonl) for audit."""
    svc.publish_event("x", {"k": 1})
    log = mqtt_service._EVENTS_LOG
    lines = [ln for ln in log.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    decoded = json.loads(lines[0])
    assert decoded["event_type"] == "x"
    assert decoded["k"] == 1


def test_run_heartbeat_stops_when_event_is_set(svc):
    """run_heartbeat must exit promptly when stop_event is set, even mid-interval."""
    stop = threading.Event()
    t = threading.Thread(
        target=svc.run_heartbeat,
        kwargs={"interval_sec": 10, "stop_event": stop},
        daemon=True,
    )
    t.start()
    time.sleep(0.1)  # let one heartbeat go out
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()
    payloads = [json.loads(m["payload"]) for m in FakeMqttClient.instances[0].published]
    assert any(p.get("event_type") == "heartbeat" for p in payloads)


def test_on_connect_callback_marks_connected(svc):
    """The on_connect callback flips _connected=True, which the next heartbeat should reflect."""
    svc.publish_status()
    _, payload = _last_payload(svc)
    assert payload["connected"] is True
