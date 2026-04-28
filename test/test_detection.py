import pytest

from src.persistence import db
from src.picam import imaging
from src.services import proximity


class RecordingMqtt:
    """Drop-in for MqttService that records publish calls without networking."""

    def __init__(self, *, enabled: bool = True):
        self._enabled = enabled
        self.events: list[tuple[str, dict]] = []
        self.alerts: list[dict] = []

    @property
    def detection_enabled(self) -> bool:
        """Return whether handle_trigger should act on a trigger."""
        return self._enabled

    def publish_event(self, event_type, data=None):
        """Record an events-topic publish for assertion in tests."""
        self.events.append((event_type, data or {}))

    def publish_alert(self, *, event_type, confidence, image_ref, extra=None):
        """Record an alerts-topic publish for assertion in tests."""
        self.alerts.append(
            {
                "event_type": event_type,
                "confidence": confidence,
                "image_ref": str(image_ref),
                "extra": extra or {},
            }
        )

    def set_detection(self, enabled: bool) -> None:
        """Satisfy MqttPublisher; proximity.handle_trigger never calls this."""
        self._enabled = bool(enabled)


@pytest.fixture(autouse=True)
def _isolate(isolated_paths):
    # every test in this module runs with its own tmp DB + captures dir
    return isolated_paths


def test_trigger_ignored_when_detection_disabled():
    """When the user has toggled detection off, a trigger must short-circuit with no alert."""
    mq = RecordingMqtt(enabled=False)
    result = proximity.handle_trigger(mq)
    assert result == {"status": "ignored", "reason": "detection_disabled"}
    assert mq.events[0][0] == "trigger_ignored"
    assert not mq.alerts


def test_trigger_with_unknown_face_publishes_alert(monkeypatch):
    """Embedding below threshold against an empty whitelist must publish unknown_face_detected on alerts."""
    monkeypatch.setattr(imaging, "embed_face", lambda _p: [0.0] * 128)
    mq = RecordingMqtt()
    result = proximity.handle_trigger(mq)
    assert result["status"] == "unknown"
    assert mq.alerts[0]["event_type"] == "unknown_face_detected"
    # proximity event must also fire before the alert, for monitoring
    assert mq.events[0][0] == "proximity_detected"


def test_trigger_with_known_face_grants_access(monkeypatch):
    """Embedding that exactly matches a whitelisted user must emit access_granted, not an alert."""
    known = [0.1] * 128
    monkeypatch.setattr(imaging, "embed_face", lambda _p: known)
    with db.connect() as conn:
        db.add_user(conn, "alice", known)

    mq = RecordingMqtt()
    result = proximity.handle_trigger(mq)
    assert result["status"] == "granted"
    assert result["user"] == "alice"
    assert mq.events[-1][0] == "access_granted"
    assert mq.events[-1][1]["user"] == "alice"
    assert not mq.alerts


def test_trigger_with_low_quality_capture_logs_event(monkeypatch):
    """A ValueError from embed_face (e.g. blurry image) must be logged as low_quality_capture with no alert."""

    def bad_embed(_p):
        raise ValueError("no face detected")

    monkeypatch.setattr(imaging, "embed_face", bad_embed)
    mq = RecordingMqtt()
    result = proximity.handle_trigger(mq)
    assert result["status"] == "low_quality"
    assert any(e[0] == "low_quality_capture" for e in mq.events)
    assert not mq.alerts
