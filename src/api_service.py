"""Flask REST API for the Smart Security Camera.

Exposes the private control plane described in docs/Architecture.pdf:

  POST /users/register    capture a photo, embed the face, store in the
                           SQLite whitelist (requires X-API-Key header)
  POST /detection/toggle   enable/disable the detection loop and announce
                           the change on home/security/events
  GET  /users              list whitelisted users (no embeddings returned)
  GET  /healthz            unauthenticated liveness probe

The server binds to config.API_HOST (default 127.0.0.1) and requires a
pre-shared key on every non-health route. Requests missing or providing a
wrong key get a 401, matching the "Security / Safety Note" in the doc.

Run:  python -m src.api_service
"""

from __future__ import annotations

import logging
import threading
from functools import wraps

from flask import Flask, jsonify, request

from . import capture, config, db
from .mqtt_service import MqttService

log = logging.getLogger("api_service")


def _require_api_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not config.API_KEY:
            return jsonify({"error": "server misconfigured: API_KEY not set"}), 500
        supplied = request.headers.get(config.API_KEY_HEADER, "")
        if supplied != config.API_KEY:
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper


def create_app(mqtt_service: MqttService | None = None) -> Flask:
    """Build and configure the Flask app.

    Args:
        mqtt_service: Optional already-connected :class:`MqttService`. Tests
            can pass ``None`` (detection-toggle routes will return 503).

    Returns:
        A ready-to-run Flask application with the private camera routes.
    """
    app = Flask(__name__)
    app.config["mqtt"] = mqtt_service

    @app.get("/healthz")
    def healthz():
        mq: MqttService | None = app.config.get("mqtt")
        return jsonify(
            {
                "ok": True,
                "detection_enabled": mq.detection_enabled if mq else None,
            }
        )

    @app.post("/users/register")
    @_require_api_key
    def register_user():
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name is required"}), 400

        try:
            image_path = capture.capture_image()
            embedding = capture.embed_face(image_path)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        except Exception as e:  # camera / hardware failure
            log.exception("capture failed")
            return jsonify({"error": f"capture_failed: {e}"}), 500

        with db.connect() as conn:
            user_id = db.add_user(conn, name, embedding)

        mq: MqttService | None = app.config.get("mqtt")
        if mq is not None:
            mq.publish_event(
                "user_registered",
                {"user_id": user_id, "name": name, "image_ref": str(image_path)},
            )

        return (
            jsonify({"id": user_id, "name": name, "image_ref": str(image_path)}),
            201,
        )

    @app.get("/users")
    @_require_api_key
    def list_users():
        with db.connect() as conn:
            return jsonify({"users": db.list_users(conn)})

    @app.post("/detection/toggle")
    @_require_api_key
    def toggle_detection():
        body = request.get_json(silent=True) or {}
        if "enabled" not in body:
            return jsonify({"error": "body must include 'enabled' (bool)"}), 400
        enabled = bool(body["enabled"])
        mq: MqttService | None = app.config.get("mqtt")
        if mq is None:
            return jsonify({"error": "mqtt service not initialized"}), 503
        mq.set_detection(enabled)
        return jsonify({"detection_enabled": enabled})

    return app


def main() -> None:
    """Run the REST API plus an in-process MQTT client + heartbeat thread."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not config.API_KEY:
        log.warning(
            "API_KEY is empty; /users/register and /detection/toggle will return 500. "
            "Set the API_KEY environment variable before starting the service."
        )

    mqtt_svc = MqttService()
    mqtt_svc.start()

    stop_event = threading.Event()
    hb_thread = threading.Thread(
        target=mqtt_svc.run_heartbeat,
        kwargs={"stop_event": stop_event},
        daemon=True,
    )
    hb_thread.start()

    app = create_app(mqtt_svc)
    try:
        app.run(host=config.API_HOST, port=config.API_PORT, debug=False, use_reloader=False)
    finally:
        stop_event.set()
        mqtt_svc.stop()


if __name__ == "__main__":
    main()
