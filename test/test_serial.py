import json
import threading

import pytest

from src import serial_bridge


class FakeSerial:
    """Stand-in for ``pyserial`` ``Serial`` that feeds scripted lines in tests."""

    def __init__(self, lines, stop_event):
        self._lines = [ln if isinstance(ln, bytes) else (ln + "\n").encode("utf-8") for ln in lines]
        self._stop_event = stop_event
        self.closed = False

    def readline(self):
        """Return the next queued line, or end the bridge loop when the queue is empty."""
        if self._lines:
            return self._lines.pop(0)
        self._stop_event.set()
        return b""

    def close(self):
        """Mark the fake port as closed (mirrors real ``Serial.close``)."""
        self.closed = True


class RecordingMqtt:
    """MqttPublisher fake: records :meth:`publish_event` calls; other protocol members are no-ops.

    Serial tests monkeypatch :func:`src.services.proximity.handle_trigger`, so
    :meth:`publish_alert` is not exercised; we still provide it (and
    :attr:`detection_enabled` / :meth:`set_detection`) to match
    :class:`src.mqtt_service.MqttPublisher` if a test ever calls the real
    :func:`src.services.proximity.handle_trigger` without a stub.
    """

    def __init__(self):
        self.events = []

    @property
    def detection_enabled(self):
        """True so :func:`src.services.proximity.handle_trigger` does not early-exit on disabled detection."""
        return True

    def publish_event(self, event_type, data=None):
        """Append a ``(event_type, data)`` tuple to :attr:`events`."""
        self.events.append((event_type, data or {}))

    def publish_alert(self, *, event_type, confidence, image_ref, extra=None):
        """Protocol hook; not used in these tests (see class docstring)."""
        pass

    def set_detection(self, enabled: bool):
        """Protocol hook; the serial bridge does not toggle detection."""
        pass


def test_should_trigger_accepts_only_expected_events():
    """``should_trigger`` is True only for the expected ``event_type`` values."""
    assert serial_bridge.should_trigger({"event_type": "proximity_detected"}) is True
    assert serial_bridge.should_trigger({"event_type": "confirmed_trigger"}) is True
    assert serial_bridge.should_trigger({"event_type": "sensor_triggered"}) is True
    assert serial_bridge.should_trigger({"event_type": "heartbeat"}) is False
    assert serial_bridge.should_trigger({"event_type": "sample"}) is False
    assert serial_bridge.should_trigger({}) is False


def test_run_serial_bridge_logs_and_triggers_detection(monkeypatch, isolated_paths):
    """A confirmed trigger line runs detection, logs JSON, and publishes MQTT events."""
    stop = threading.Event()
    mqtt = RecordingMqtt()

    line = json.dumps({"event_type": "confirmed_trigger", "blocked": True})
    fake_serial = FakeSerial([line], stop)

    monkeypatch.setattr(serial_bridge.serial, "Serial", lambda *args, **kwargs: fake_serial)

    called = {"count": 0}

    def fake_handle_trigger(mq):
        called["count"] += 1
        return {"status": "granted", "user": "alice"}

    monkeypatch.setattr(serial_bridge.proximity, "handle_trigger", fake_handle_trigger)

    serial_bridge.run_serial_bridge(
        mqtt_service=mqtt,
        stop_event=stop,
        serial_port="/dev/fake",
        baudrate=115200,
        timeout=0.1,
    )

    assert called["count"] == 1
    assert any(evt[0] == "arduino_serial_received" for evt in mqtt.events)
    assert any(evt[0] == "detection_result" for evt in mqtt.events)

    log_file = isolated_paths / "artifacts" / "arduino_serial.jsonl"
    assert log_file.exists()
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_type"] == "confirmed_trigger"
    assert fake_serial.closed is True


@pytest.mark.usefixtures("isolated_paths")
def test_run_serial_bridge_ignores_non_trigger_event(monkeypatch):
    """Non-matching events are logged to MQTT as raw lines but do not start detection."""
    stop = threading.Event()
    mqtt = RecordingMqtt()

    line = json.dumps({"event_type": "sample", "value": 123})
    fake_serial = FakeSerial([line], stop)

    monkeypatch.setattr(serial_bridge.serial, "Serial", lambda *args, **kwargs: fake_serial)

    def should_not_run(_mq):
        raise AssertionError("handle_trigger should not have been called")

    monkeypatch.setattr(serial_bridge.proximity, "handle_trigger", should_not_run)

    serial_bridge.run_serial_bridge(
        mqtt_service=mqtt,
        stop_event=stop,
        serial_port="/dev/fake",
        baudrate=115200,
        timeout=0.1,
    )

    assert any(evt[0] == "arduino_serial_received" for evt in mqtt.events)
    assert not any(evt[0] == "detection_result" for evt in mqtt.events)


@pytest.mark.usefixtures("isolated_paths")
def test_run_serial_bridge_bad_json_publishes_error(monkeypatch):
    """Malformed JSON from the line reader publishes a ``serial_json_error`` event."""
    stop = threading.Event()
    mqtt = RecordingMqtt()

    fake_serial = FakeSerial(['{"event_type": bad json}'], stop)
    monkeypatch.setattr(serial_bridge.serial, "Serial", lambda *args, **kwargs: fake_serial)

    serial_bridge.run_serial_bridge(
        mqtt_service=mqtt,
        stop_event=stop,
        serial_port="/dev/fake",
        baudrate=115200,
        timeout=0.1,
    )

    assert any(evt[0] == "serial_json_error" for evt in mqtt.events)


def test_run_serial_bridge_open_failure_returns_without_raising(monkeypatch):
    """Missing device: log warning path; no MQTT events from the reader loop."""

    def fail_open(*args, **kwargs):
        raise serial_bridge.serial.SerialException("could not open port")

    monkeypatch.setattr(serial_bridge.serial, "Serial", fail_open)
    stop = threading.Event()
    mqtt = RecordingMqtt()
    serial_bridge.run_serial_bridge(
        mqtt_service=mqtt,
        stop_event=stop,
        serial_port="/dev/nonexistent",
        baudrate=115200,
        timeout=0.1,
    )

    assert mqtt.events == []
