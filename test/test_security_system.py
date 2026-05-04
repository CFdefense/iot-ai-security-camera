import pytest

from src import security_system
from src.camera.picam import imaging
from src.core import config
from src.data import db


class FakeMqtt:
    """Structural stand-in for MqttService used by the Flask app.

    Implements just the three members ``security_system`` touches (``detection_enabled``,
    ``publish_event``, ``set_detection``) and records every call into
    ``self.events`` so tests can assert what the API asked the MQTT layer to do.
    """

    def __init__(self):
        # Mirrors MqttService: detection starts enabled on boot.
        self._enabled = True
        # Append-only log of (event_type, data) tuples for assertions.
        self.events: list[tuple[str, dict]] = []

    @property
    def detection_enabled(self) -> bool:
        """Return the current detection flag (read by /healthz and /detection/toggle)."""
        return self._enabled

    def publish_event(self, event_type, data=None):
        """Record a publish_event call instead of hitting a real MQTT broker."""
        self.events.append((event_type, data or {}))

    def publish_alert(self, *, event_type, confidence, image_ref, extra=None):
        """Record an alert publish; REST handlers don't call this but the protocol requires it."""
        self.events.append(
            (
                "ALERT:" + event_type,
                {"confidence": confidence, "image_ref": str(image_ref), "extra": extra or {}},
            )
        )

    def set_detection(self, enabled: bool):
        """Flip the detection flag and record the toggle, matching the real MqttService behavior."""
        self._enabled = bool(enabled)
        self.events.append(("detection_toggle", {"enabled": self._enabled}))

    def publish_component_status(self, component: str, *, state: str):
        """No-op; real broker publishes retained component rows for the dashboard."""
        pass

    def dashboard_status_bundle(self):
        """Thin snapshot for /dashboard/status.json tests."""
        return {
            "last_status_at": "2026-04-01T00:00:00Z",
            "status_topic": "home/security/status",
        }


@pytest.fixture
def fake_mqtt():
    """Fresh FakeMqtt per test so event history doesn't leak between tests."""
    return FakeMqtt()


@pytest.fixture(autouse=True)
def _stub_camera_and_embed(monkeypatch):
    """No Picamera2 or ONNX models in test env — stub JPEG capture and 128-D embedding."""
    tiny = b"\xff\xd8\xff\xdb" + bytes(range(16)) + b"\xff\xd9"
    monkeypatch.setattr(imaging, "capture_frame_jpeg", lambda: tiny)
    monkeypatch.setattr(imaging, "embed_face_bytes", lambda _b: [0.0] * 128)


@pytest.fixture
def client(monkeypatch, isolated_paths, fake_mqtt):
    """Flask test client wired up with a valid API key and the fake MQTT publisher."""
    monkeypatch.setattr(config, "API_KEY", "test-key")
    app = security_system.create_app(fake_mqtt)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_dashboard_events_stream_requires_login(monkeypatch, isolated_paths):
    """SSE endpoint is session-guarded like the rest of the dashboard."""
    monkeypatch.setattr(config, "API_KEY", "test-key")
    app = security_system.create_app(FakeMqtt())
    app.config["TESTING"] = True
    with app.test_client() as c:
        assert c.get("/dashboard/events/stream").status_code == 302


def test_dashboard_status_json_requires_login(monkeypatch, isolated_paths):
    """Health snapshot requires a browser session."""
    monkeypatch.setattr(config, "API_KEY", "test-key")
    app = security_system.create_app(FakeMqtt())
    app.config["TESTING"] = True
    with app.test_client() as c:
        assert c.get("/dashboard/status.json").status_code == 302


def test_dashboard_status_json_ok_when_logged_in(monkeypatch, isolated_paths, fake_mqtt):
    """GET /dashboard/status.json returns component rows once session is established."""
    monkeypatch.setattr(config, "API_KEY", "device-secret")
    monkeypatch.setenv("USER_EMAIL", "u@example.local")
    monkeypatch.setenv("USER_PASSWORD", "pw")

    app = security_system.create_app(fake_mqtt)
    app.config["TESTING"] = True
    with app.test_client() as c:
        assert c.post(
            "/login",
            data={"email": "u@example.local", "password": "pw"},
        ).status_code in (302, 200)

        r = c.get("/dashboard/status.json")

    assert r.status_code == 200
    body = r.get_json()
    assert body.get("last_status_at") == "2026-04-01T00:00:00Z"
    assert body.get("status_topic") == "home/security/status"


def test_healthz_is_public_and_reports_detection_state(client):
    """/healthz has no API-key check (so kube/monitoring can probe it) and reports the detection flag."""
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["detection_enabled"] is True


def test_register_without_api_key_is_unauthorized(client):
    """Requests missing the X-API-Key header must be rejected with 401 per the Architecture's Security Note."""
    r = client.post("/users/register", json={"name": "a"})
    assert r.status_code == 401


def test_register_with_wrong_api_key_is_unauthorized(client):
    """A wrong key must also return 401 (not 403) so clients see a uniform auth-failure response."""
    r = client.post(
        "/users/register",
        json={"name": "a"},
        headers={"X-API-Key": "wrong"},
    )
    assert r.status_code == 401


def test_register_returns_500_when_server_has_no_api_key(monkeypatch, isolated_paths):
    """If the server is misconfigured (empty API_KEY env) we fail closed with 500, never silently allow."""
    monkeypatch.setattr(config, "API_KEY", "")
    app = security_system.create_app(FakeMqtt())
    with app.test_client() as c:
        r = c.post(
            "/users/register",
            json={"name": "a"},
            headers={"X-API-Key": "anything"},
        )
        assert r.status_code == 500


def test_register_happy_path_returns_201_and_publishes_event(client, fake_mqtt):
    """Valid POST /users/register returns 201 with the new user and publishes a user_registered event."""
    r = client.post(
        "/users/register",
        json={"name": "christian"},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 201
    body = r.get_json()
    assert body["id"] == 1
    assert body["name"] == "christian"
    assert body["registration_image_stored"] is True
    assert fake_mqtt.events[-1][0] == "user_registered"


def test_register_missing_name_is_400(client):
    """Missing required field 'name' is a client error (400), not a server crash."""
    r = client.post(
        "/users/register",
        json={},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 400


def test_register_capture_hardware_failure_is_500(client, monkeypatch):
    """Unexpected camera/hw failures surface as 500 with a 'capture_failed' error hint."""

    def boom(_name):
        raise RuntimeError("camera offline")

    monkeypatch.setattr(security_system, "capture_embed_and_save", boom)
    r = client.post(
        "/users/register",
        json={"name": "x"},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 500
    assert "capture_failed" in r.get_json()["error"]


def test_register_value_error_from_embed_is_422(client, monkeypatch):
    """A ValueError from embed_face (e.g. 'no face detected') is a 422 unprocessable-entity, not 500."""

    def no_face(_name):
        raise ValueError("no face detected")

    monkeypatch.setattr(security_system, "capture_embed_and_save", no_face)
    r = client.post(
        "/users/register",
        json={"name": "x"},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 422


def test_list_users_does_not_leak_embeddings(client):
    """GET /users returns names + timestamps but never raw face embeddings (privacy)."""
    client.post(
        "/users/register",
        json={"name": "alice"},
        headers={"X-API-Key": "test-key"},
    )
    r = client.get("/users", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    users = r.get_json()["users"]
    assert len(users) == 1
    assert users[0]["name"] == "alice"
    assert "embedding" not in users[0]


def test_toggle_detection_updates_flag_and_publishes(client, fake_mqtt):
    """POST /detection/toggle both mutates the MQTT service's flag and triggers a detection_toggle event."""
    r = client.post(
        "/detection/toggle",
        json={"enabled": False},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 200
    assert r.get_json() == {"detection_enabled": False}
    assert fake_mqtt.detection_enabled is False
    assert fake_mqtt.events[-1][0] == "detection_toggle"


def test_toggle_detection_requires_enabled_field(client):
    """Body without 'enabled' is a 400; we refuse to guess the intended state."""
    r = client.post(
        "/detection/toggle",
        json={},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 400


def test_toggle_detection_returns_503_without_mqtt(monkeypatch):
    """If the MQTT service wasn't injected (tests/degraded start) toggling returns 503 instead of crashing."""
    monkeypatch.setattr(config, "API_KEY", "test-key")
    app = security_system.create_app(mqtt_service=None)
    with app.test_client() as c:
        r = c.post(
            "/detection/toggle",
            json={"enabled": False},
            headers={"X-API-Key": "test-key"},
        )
        assert r.status_code == 503


def test_dashboard_login_uses_session_not_user_api_key(monkeypatch, isolated_paths, fake_mqtt):
    """POST /login accepts email/password only; REST API_KEY is enforced server-side elsewhere."""
    monkeypatch.setattr(config, "API_KEY", "device-secret")
    monkeypatch.setenv("USER_EMAIL", "u@example.local")
    monkeypatch.setenv("USER_PASSWORD", "pw")

    app = security_system.create_app(fake_mqtt)
    app.config["TESTING"] = True
    with app.test_client() as c:
        r = c.post(
            "/login",
            data={"email": "u@example.local", "password": "wrong"},
        )
        assert r.status_code == 401

        r2 = c.post(
            "/login",
            data={"email": "u@example.local", "password": "pw"},
        )
        assert r2.status_code == 302
        assert "/dashboard" in (r2.headers.get("Location") or "")


def test_dashboard_alert_photo_requires_login(monkeypatch, isolated_paths):
    """Alert photo route is session-protected like other dashboard endpoints."""
    monkeypatch.setattr(config, "API_KEY", "test-key")
    app = security_system.create_app(FakeMqtt())
    app.config["TESTING"] = True
    with app.test_client() as c:
        assert c.get("/dashboard/alerts/1/photo").status_code == 302


def test_dashboard_alert_photo_ok_when_logged_in(monkeypatch, isolated_paths, fake_mqtt):
    """Logged-in dashboard users can fetch stored JPEG bytes for detection alerts."""
    monkeypatch.setattr(config, "API_KEY", "device-secret")
    monkeypatch.setenv("USER_EMAIL", "u@example.local")
    monkeypatch.setenv("USER_PASSWORD", "pw")
    with db.connect() as conn:
        alert_id = db.record_detection_alert(
            conn,
            event_type="unknown_face_detected",
            outcome="unknown",
            image_ref="inline:test",
            capture_image=b"\xff\xd8sample\xff\xd9",
        )

    app = security_system.create_app(fake_mqtt)
    app.config["TESTING"] = True
    with app.test_client() as c:
        c.post("/login", data={"email": "u@example.local", "password": "pw"})
        r = c.get(f"/dashboard/alerts/{alert_id}/photo")
    assert r.status_code == 200
    assert r.mimetype == "image/jpeg"


def test_dashboard_delete_alert_requires_login(monkeypatch, isolated_paths):
    """Delete detection event endpoint is session-protected."""
    monkeypatch.setattr(config, "API_KEY", "test-key")
    app = security_system.create_app(FakeMqtt())
    app.config["TESTING"] = True
    with app.test_client() as c:
        assert c.post("/dashboard/alerts/1/delete").status_code == 302


def test_dashboard_delete_alert_ok_when_logged_in(monkeypatch, isolated_paths, fake_mqtt):
    """Logged-in dashboard users can delete persisted detection alerts."""
    monkeypatch.setattr(config, "API_KEY", "device-secret")
    monkeypatch.setenv("USER_EMAIL", "u@example.local")
    monkeypatch.setenv("USER_PASSWORD", "pw")
    with db.connect() as conn:
        alert_id = db.record_detection_alert(
            conn,
            event_type="unknown_face_detected",
            outcome="unknown",
            image_ref="inline:test",
            capture_image=b"\xff\xd8sample\xff\xd9",
        )

    app = security_system.create_app(fake_mqtt)
    app.config["TESTING"] = True
    with app.test_client() as c:
        c.post("/login", data={"email": "u@example.local", "password": "pw"})
        r = c.post(
            f"/dashboard/alerts/{alert_id}/delete",
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        )
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    with db.connect() as conn:
        rows = db.list_recent_detection_alerts(conn, limit=40)
    assert all(int(row["id"]) != int(alert_id) for row in rows)
