import json
import threading

from src import serial_bridge


class FakeSerial:
    def __init__(self, lines, stop_event):
        self._lines = [
            ln if isinstance(ln, bytes) else (ln + "\n").encode("utf-8")
            for ln in lines
        ]
        self._stop_event = stop_event
        self.closed = False

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        self._stop_event.set()
        return b""

    def close(self):
        self.closed = True


class RecordingMqtt:
    def __init__(self):
        self.events = []
        self.alerts = []

    @property
    def detection_enabled(self):
        return True

    def publish_event(self, event_type, data=None):
        self.events.append((event_type, data or {}))

    def publish_alert(self, *, event_type, confidence, image_ref, extra=None):
        self.alerts.append(
            {
                "event_type": event_type,
                "confidence": confidence,
                "image_ref": str(image_ref),
                "extra": extra or {},
            }
        )

    def set_detection(self, enabled: bool):
        pass


def test_should_trigger_accepts_only_expected_events():
    assert serial_bridge.should_trigger({"event_type": "proximity_detected"}) is True
    assert serial_bridge.should_trigger({"event_type": "confirmed_trigger"}) is True
    assert serial_bridge.should_trigger({"event_type": "sensor_triggered"}) is True
    assert serial_bridge.should_trigger({"event_type": "heartbeat"}) is False
    assert serial_bridge.should_trigger({"event_type": "sample"}) is False
    assert serial_bridge.should_trigger({}) is False


def test_run_serial_bridge_logs_and_triggers_detection(monkeypatch, isolated_paths):
    stop = threading.Event()
    mqtt = RecordingMqtt()

    line = json.dumps({"event_type": "confirmed_trigger", "blocked": True})
    fake_serial = FakeSerial([line], stop)

    monkeypatch.setattr(serial_bridge.serial, "Serial", lambda *args, **kwargs: fake_serial)

    called = {"count": 0}

    def fake_handle_trigger(mq):
        called["count"] += 1
        return {"status": "granted", "user": "alice"}

    monkeypatch.setattr(serial_bridge.detection, "handle_trigger", fake_handle_trigger)

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


def test_run_serial_bridge_ignores_non_trigger_event(monkeypatch, isolated_paths):
    stop = threading.Event()
    mqtt = RecordingMqtt()

    line = json.dumps({"event_type": "sample", "value": 123})
    fake_serial = FakeSerial([line], stop)

    monkeypatch.setattr(serial_bridge.serial, "Serial", lambda *args, **kwargs: fake_serial)

    def should_not_run(_mq):
        raise AssertionError("handle_trigger should not have been called")

    monkeypatch.setattr(serial_bridge.detection, "handle_trigger", should_not_run)

    serial_bridge.run_serial_bridge(
        mqtt_service=mqtt,
        stop_event=stop,
        serial_port="/dev/fake",
        baudrate=115200,
        timeout=0.1,
    )

    assert any(evt[0] == "arduino_serial_received" for evt in mqtt.events)
    assert not any(evt[0] == "detection_result" for evt in mqtt.events)


def test_run_serial_bridge_bad_json_publishes_error(monkeypatch, isolated_paths):
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