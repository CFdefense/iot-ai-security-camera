import pytest

from src import api_service, capture, config


class FakeMqtt:
    """Structural stand-in for MqttService used by the Flask app.

    Implements just the three members api_service touches (``detection_enabled``,
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
        """Record an alert publish; api_service doesn't call this but the protocol requires it."""
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


@pytest.fixture
def fake_mqtt():
    """Fresh FakeMqtt per test so event history doesn't leak between tests."""
    return FakeMqtt()


@pytest.fixture
def client(monkeypatch, isolated_paths, fake_mqtt):
    """Flask test client wired up with a valid API key and the fake MQTT publisher."""
    monkeypatch.setattr(config, "API_KEY", "test-key")
    app = api_service.create_app(fake_mqtt)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


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
    app = api_service.create_app(FakeMqtt())
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
    assert body["image_ref"].endswith(".jpg")
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

    def boom():
        raise RuntimeError("camera offline")

    monkeypatch.setattr(capture, "capture_image", boom)
    r = client.post(
        "/users/register",
        json={"name": "x"},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 500
    assert "capture_failed" in r.get_json()["error"]


def test_register_value_error_from_embed_is_422(client, monkeypatch):
    """A ValueError from embed_face (e.g. 'no face detected') is a 422 unprocessable-entity, not 500."""

    def no_face(_p):
        raise ValueError("no face detected")

    monkeypatch.setattr(capture, "embed_face", no_face)
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
    app = api_service.create_app(mqtt_service=None)
    with app.test_client() as c:
        r = c.post(
            "/detection/toggle",
            json={"enabled": False},
            headers={"X-API-Key": "test-key"},
        )
        assert r.status_code == 503
