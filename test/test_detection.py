import pytest

from src.camera.picam import imaging
from src.camera.services import proximity
from src.data import db


@pytest.fixture(autouse=True)
def _isolate(isolated_paths):
    # every test in this module runs with its own tmp DB + artifacts dir
    return isolated_paths


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


def test_trigger_ignored_when_detection_disabled():
    """When the user has toggled detection off, a trigger must short-circuit with no alert."""
    mq = RecordingMqtt(enabled=False)
    result = proximity.handle_trigger(mq)
    assert result == {"status": "ignored", "reason": "detection_disabled"}
    assert mq.events[0][0] == "trigger_ignored"
    assert not mq.alerts


def test_trigger_with_unknown_face_publishes_alert(monkeypatch):
    """Embedding below threshold against an empty whitelist must publish unknown_face_detected on alerts."""
    monkeypatch.setattr(imaging, "embed_face_bytes", lambda _b: [0.0] * 128)
    mq = RecordingMqtt()
    result = proximity.handle_trigger(mq)
    assert result["status"] == "unknown"
    assert mq.alerts[0]["event_type"] == "unknown_face_detected"
    # proximity event must also fire before the alert, for monitoring
    assert mq.events[0][0] == "proximity_detected"
    with db.connect() as conn:
        rows = db.list_recent_detection_alerts(conn, limit=5)
        assert rows[0]["event_type"] == "unknown_face_detected"
        assert rows[0]["outcome"] == "unknown"
        assert rows[0]["has_capture_image"] is True


def test_trigger_with_known_face_grants_access(monkeypatch):
    """Embedding that exactly matches a whitelisted user must emit access_granted, not an alert."""
    known = [0.1] * 128
    monkeypatch.setattr(imaging, "embed_face_bytes", lambda _b: known)
    with db.connect() as conn:
        db.add_user(conn, "alice", known)

    mq = RecordingMqtt()
    result = proximity.handle_trigger(mq)
    assert result["status"] == "granted"
    assert result["user"] == "alice"
    assert mq.events[-1][0] == "access_granted"
    assert mq.events[-1][1]["user"] == "alice"
    assert not mq.alerts
    with db.connect() as conn:
        rows = db.list_recent_detection_alerts(conn, limit=5)
        assert rows[0]["event_type"] == "access_granted"
        assert rows[0]["matched_user_name"] == "alice"
        raw = db.get_detection_alert_image(conn, int(rows[0]["id"]))
        assert raw is not None


def test_trigger_with_low_quality_capture_logs_event(monkeypatch):
    """A ValueError from embed_face_bytes must be logged as low_quality_capture with no alert."""

    def bad_embed(_b):
        raise ValueError("no face detected")

    monkeypatch.setattr(imaging, "embed_face_bytes", bad_embed)
    mq = RecordingMqtt()
    result = proximity.handle_trigger(mq)
    assert result["status"] == "low_quality"
    assert any(e[0] == "low_quality_capture" for e in mq.events)
    assert not mq.alerts
    with db.connect() as conn:
        rows = db.list_recent_detection_alerts(conn, limit=5)
        assert rows[0]["event_type"] == "low_quality_capture"
        assert rows[0]["reason"] == "no face detected"


def test_trigger_without_person_detection_records_ignored_alert(monkeypatch):
    """Object detections that are not confident people are stored as ignored proximity alerts."""
    raw = b"\xff\xd8not-person\xff\xd9"
    capture = imaging.CapturedFrame(
        jpeg=raw,
        detections=[
            imaging.ObjectDetection(label="dog", confidence=0.99, bbox=(1, 2, 3, 4), category=18),
            imaging.ObjectDetection(label="person", confidence=0.59, bbox=(5, 6, 7, 8), category=0),
        ],
    )
    monkeypatch.setattr(proximity.config, "CAMERA_DETECTION_THRESHOLD", 0.6)
    monkeypatch.setattr(imaging, "capture_frame_with_detections", lambda: capture)

    mq = RecordingMqtt()
    result = proximity.handle_trigger(mq)

    assert result["status"] == "ignored"
    assert result["reason"] == "no person detected"
    assert any(event == "proximity_alert" for event, _payload in mq.events)
    assert not mq.alerts
    with db.connect() as conn:
        rows = db.list_recent_detection_alerts(conn, limit=5)
        assert rows[0]["event_type"] == "proximity_alert"
        assert rows[0]["outcome"] == "ignored"
        assert rows[0]["reason"] == "no person detected"
        assert db.get_detection_alert_image(conn, int(rows[0]["id"])) == raw


def test_trigger_embeds_each_confident_person_bbox_and_grants_match(monkeypatch):
    """Each confident person crop is embedded with its bbox until a known user matches."""
    raw = b"\xff\xd8two-people\xff\xd9"
    known = [0.2] * 128
    first_bbox = (1, 2, 3, 4)
    second_bbox = (5, 6, 7, 8)
    capture = imaging.CapturedFrame(
        jpeg=raw,
        detections=[
            imaging.ObjectDetection(label="person", confidence=0.91, bbox=first_bbox, category=0),
            imaging.ObjectDetection(label="person", confidence=0.88, bbox=second_bbox, category=0),
            imaging.ObjectDetection(label="person", confidence=0.1, bbox=(9, 10, 11, 12), category=0),
            imaging.ObjectDetection(label="cat", confidence=0.99, bbox=(13, 14, 15, 16), category=17),
        ],
    )
    with db.connect() as conn:
        db.add_user(conn, "bob", known)

    calls = []

    def fake_embed(jpeg, bbox=None):
        calls.append((jpeg, bbox))
        if bbox == first_bbox:
            return [0.0] * 128
        if bbox == second_bbox:
            return known
        raise AssertionError(f"unexpected embedding call for bbox={bbox!r}")

    monkeypatch.setattr(proximity.config, "CAMERA_DETECTION_THRESHOLD", 0.6)
    monkeypatch.setattr(imaging, "capture_frame_with_detections", lambda: capture)
    monkeypatch.setattr(imaging, "embed_face_bytes", fake_embed)

    mq = RecordingMqtt()
    result = proximity.handle_trigger(mq)

    assert calls == [(raw, first_bbox), (raw, second_bbox)]
    assert result["status"] == "granted"
    assert result["user"] == "bob"
    assert mq.events[-1][0] == "access_granted"
    assert mq.events[-1][1]["user"] == "bob"
    assert not mq.alerts


def test_trigger_reports_all_person_embedding_errors_when_no_crop_encodes(monkeypatch):
    """When every person crop fails face embedding, all error messages are persisted."""
    raw = b"\xff\xd8bad-crops\xff\xd9"
    capture = imaging.CapturedFrame(
        jpeg=raw,
        detections=[
            imaging.ObjectDetection(label="person", confidence=0.91, bbox=(1, 2, 3, 4), category=0),
            imaging.ObjectDetection(label="person", confidence=0.88, bbox=(5, 6, 7, 8), category=0),
        ],
    )

    def bad_embed(_jpeg, bbox=None):
        raise ValueError(f"no face in bbox {bbox[0]}")

    monkeypatch.setattr(proximity.config, "CAMERA_DETECTION_THRESHOLD", 0.6)
    monkeypatch.setattr(imaging, "capture_frame_with_detections", lambda: capture)
    monkeypatch.setattr(imaging, "embed_face_bytes", bad_embed)

    mq = RecordingMqtt()
    result = proximity.handle_trigger(mq)

    assert result["status"] == "low_quality"
    assert any(payload.get("reason") == "no face in bbox 1; no face in bbox 5" for _event, payload in mq.events)
    assert not mq.alerts
    with db.connect() as conn:
        rows = db.list_recent_detection_alerts(conn, limit=5)
        assert rows[0]["event_type"] == "low_quality_capture"
        assert rows[0]["reason"] == "no face in bbox 1; no face in bbox 5"
