import pytest

from src.camera.picam import imaging, imx500_person_gate as imx_gate
from src.camera.services import proximity
from src.core import config
from src.data import db


@pytest.fixture(autouse=True)
def _isolate(isolated_paths):
    # every test in this module runs with its own tmp DB + artifacts dir
    return isolated_paths


@pytest.fixture(autouse=True)
def _stub_imx500_capture(monkeypatch):
    """No Pi hardware in CI — proximity always runs IMX path; stub JPEG + confident person."""
    tiny = b"\xff\xd8\xff\xdb" + bytes(range(16)) + b"\xff\xd9"
    monkeypatch.setattr(imaging, "capture_frame_jpeg", lambda: tiny)
    monkeypatch.setattr(imaging, "persist_detection_capture", lambda _b: None)
    monkeypatch.setattr(imx_gate, "capture_jpeg_and_person_seen", lambda: (tiny, True, 0.9))


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
        assert rows[0]["reason"] == "YuNet/SFace embedding: no face detected"


def test_non_person_detector_result_short_circuits_embedding(monkeypatch):
    """If trigger metadata says a non-person object, skip face embedding entirely."""

    def should_not_run(_b):
        raise AssertionError("embedding should be skipped for non-person triggers")

    monkeypatch.setattr(imaging, "embed_face_bytes", should_not_run)
    mq = RecordingMqtt()
    result = proximity.handle_trigger(
        mq,
        trigger_event={
            "event_type": "obstacle_detected",
            "detection": {"object": "cat", "score": 0.97},
        },
    )
    assert result["status"] == "ignored_non_person"
    assert result["object"] == "cat"
    assert result["score"] == pytest.approx(0.97)
    assert any(e[0] == "non_person_ignored" for e in mq.events)
    assert not mq.alerts


def test_low_person_score_short_circuits_embedding(monkeypatch):
    """If detector reports person below threshold, skip embedding and emit ignored result."""

    def should_not_run(_b):
        raise AssertionError("embedding should be skipped for low-score person triggers")

    monkeypatch.setattr(imaging, "embed_face_bytes", should_not_run)
    monkeypatch.setattr(config, "PERSON_DETECTION_MIN_SCORE", 0.5)
    mq = RecordingMqtt()
    result = proximity.handle_trigger(
        mq,
        trigger_event={
            "event_type": "obstacle_detected",
            "detection": {"object": "person", "score": 0.25},
        },
    )
    assert result["status"] == "ignored_low_person_score"
    assert result["object"] == "person"
    assert result["score"] == pytest.approx(0.25)
    assert result["threshold"] == pytest.approx(0.5)
    assert any(e[0] == "low_person_score_ignored" for e in mq.events)
    assert not mq.alerts


def test_root_level_label_not_used_for_short_circuit(monkeypatch):
    """Only ``detection.{object,score}`` counts; root ``label``/``confidence`` are ignored."""
    monkeypatch.setattr(imaging, "embed_face_bytes", lambda _b: [0.0] * 128)
    mq = RecordingMqtt()
    result = proximity.handle_trigger(
        mq,
        trigger_event={"event_type": "obstacle_detected", "label": "dog", "confidence": 0.88},
    )
    assert result["status"] == "unknown"
    assert not any(e[0] == "non_person_ignored" for e in mq.events)


def test_imx500_gate_blocks_yunet_when_no_person(monkeypatch):
    """When IMX reports no person, YuNet/SFace must not run."""
    tiny = b"\xff\xd8\xff\xdb" + bytes(range(16)) + b"\xff\xd9"

    def should_not_run(_b):
        raise AssertionError("embed_face_bytes must not run when IMX500 blocks")

    monkeypatch.setattr(
        imx_gate,
        "capture_jpeg_and_person_seen",
        lambda: (tiny, False, None),
    )
    monkeypatch.setattr(imaging, "embed_face_bytes", should_not_run)

    mq = RecordingMqtt()
    result = proximity.handle_trigger(mq)
    assert result["status"] == "skipped_no_person"
    assert result["reason"] == "imx500_no_person"
    assert any(e[0] == "imx500_no_person" for e in mq.events)
    assert not mq.alerts
