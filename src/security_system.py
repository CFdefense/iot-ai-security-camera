"""Combined edge service for the Smart Security Camera.

This module is the long-running process on the Pi. It hosts three things in
one place so they can share a single broker connection and a single copy of
the ``detection_enabled`` flag:

  1. The Flask REST control plane described in ``docs/Architecture.pdf``:

       POST /users/register    capture a photo, embed the face, store in the
                                SQLite whitelist (requires X-API-Key header)
       POST /detection/toggle   enable/disable the detection loop and
                                announce the change on home/security/events
       GET  /users              list whitelisted users (no embeddings)
       GET  /healthz            unauthenticated liveness probe

  2. The MQTT publisher for ``home/security/{alerts,events,status}``.
  3. The 60-second heartbeat thread.

The server binds to ``config.API_HOST`` (default 127.0.0.1) and requires a
pre-shared key on every non-health route. Requests missing or providing a
wrong key get a 401, matching the "Security / Safety Note" in the doc.

Run: ``uv run security-system`` (or: ``python -m src.security_system``).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request

from .camera.picam import imx500_person_gate as imx500_gate
from .camera.picam.face_embed import ensure_face_models
from .camera.services.register_user import capture_embed_and_save
from .core import config
from .core.event_hub import EventHub
from .core.startup_banner import log_banner
from .core.status_publish import publish_initial_edge_components, run_camera_status_refresh_loop
from .core.task_logging import TASK_LEVEL, setup_logging
from .data import db
from .integrations.serial_bridge import run_serial_bridge
from .mqtt import MqttPublisher, MqttService
from .web import web_ui

log = logging.getLogger("security_system")


def _prefetch_face_models() -> None:
    """Download YuNet + SFace ONNX to ``FACE_MODEL_DIR`` (or ``picam/models/``) if missing."""
    try:
        yunet, sface = ensure_face_models()
        log.info("face ONNX models ready: %s, %s", yunet.name, sface.name)
    except Exception as e:
        log.warning(
            "face model prefetch failed (offline or disk issue); will retry on first face use: %s",
            e,
        )


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


def create_app(mqtt_service: MqttPublisher | None = None, *, event_hub: EventHub | None = None) -> Flask:
    """Build and configure the Flask app.

    Args:
        mqtt_service: Anything satisfying :class:`MqttPublisher` (the real
            :class:`MqttService` or a test fake). Pass ``None`` to disable
            MQTT-backed routes (they will return 503).
        event_hub: Shared :class:`EventHub` for dashboard live MQTT view; if omitted, a new hub is used.

    Returns:
        A ready-to-run Flask application with the private camera routes and dashboard UI.
    """
    with db.connect() as conn:
        db.reset_dashboard_credentials_from_env(conn)

    pkg = Path(__file__).resolve().parent / "web"
    app = Flask(
        __name__,
        template_folder=str(pkg / "templates"),
        static_folder=str(pkg / "static"),
        static_url_path="/static",
    )
    app.config["mqtt"] = mqtt_service
    app.config["event_hub"] = event_hub if event_hub is not None else EventHub()

    @app.get("/healthz")
    def healthz():
        mq: MqttPublisher | None = app.config.get("mqtt")
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
            user_id, _jpeg = capture_embed_and_save(name)
        except ValueError as e:
            return jsonify({"error": str(e)}), 422
        except Exception as e:  # camera / hardware failure
            log.exception("capture failed")
            return jsonify({"error": f"capture_failed: {e}"}), 500

        mq: MqttPublisher | None = app.config.get("mqtt")
        if mq is not None:
            mq.publish_event(
                "user_registered",
                {"user_id": user_id, "name": name},
            )

        return (
            jsonify(
                {
                    "id": user_id,
                    "name": name,
                    "registration_image_stored": True,
                }
            ),
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
        mq: MqttPublisher | None = app.config.get("mqtt")
        if mq is None:
            return jsonify({"error": "mqtt service not initialized"}), 503
        mq.set_detection(enabled)
        return jsonify({"detection_enabled": enabled})

    web_ui.init_app(app)
    return app


def main() -> None:
    """Run the REST API plus an in-process MQTT client + heartbeat thread."""
    # Suppress verbose native libcamera INFO chatter in normal TASK monitoring.
    os.environ.setdefault("LIBCAMERA_LOG_LEVELS", config.LIBCAMERA_LOG_LEVELS)
    setup_logging()
    log.log(TASK_LEVEL, "startup: initializing security-system services")

    imx_reason = imx500_gate.startup_check_failed_reason()
    if imx_reason:
        log.error("security-system will not start: %s", imx_reason)
        sys.exit(2)

    _prefetch_face_models()

    if not config.API_KEY:
        log.warning(
            "API_KEY is empty; guarded REST routes return 500 and GET/POST /login return 503. "
            "Set API_KEY (e.g. in .env) before starting the service."
        )

    hub = EventHub()
    mqtt_svc = MqttService(on_publish=hub.emit)
    mqtt_svc.start()
    log.log(TASK_LEVEL, "startup: mqtt loop started (%s:%s)", mqtt_svc.host, mqtt_svc.port)

    stop_event = threading.Event()
    hb_thread = threading.Thread(
        target=mqtt_svc.run_heartbeat,
        kwargs={"stop_event": stop_event},
        daemon=True,
    )
    hb_thread.start()

    def _api_status_heartbeat() -> None:
        while not stop_event.is_set():
            try:
                mqtt_svc.publish_component_status("api", state="up")
            except Exception as e:
                log.debug("api status heartbeat publish failed: %s", e)
            stop_event.wait(20)

    api_status_thread = threading.Thread(target=_api_status_heartbeat, daemon=True)
    api_status_thread.start()

    camera_status_thread = threading.Thread(
        target=run_camera_status_refresh_loop,
        kwargs={"mqtt_service": mqtt_svc, "stop_event": stop_event, "interval_sec": 45.0},
        daemon=True,
    )
    camera_status_thread.start()

    publish_initial_edge_components(mqtt_svc)

    if config.SERIAL_PORT:
        serial_thread = threading.Thread(
            target=run_serial_bridge,
            kwargs={
                "mqtt_service": mqtt_svc,
                "stop_event": stop_event,
                "serial_port": config.SERIAL_PORT,
                "baudrate": config.SERIAL_BAUD,
                "timeout": config.SERIAL_TIMEOUT,
            },
            daemon=True,
        )
        serial_thread.start()
        log.log(TASK_LEVEL, "startup: serial bridge enabled on %s", config.SERIAL_PORT)
    else:
        log.log(TASK_LEVEL, "startup: serial bridge skipped (SERIAL_PORT empty)")

    app = create_app(mqtt_svc, event_hub=hub)
    log_banner(mqtt_svc)
    log.log(TASK_LEVEL, "startup: dashboard available at http://%s:%s", config.API_HOST, config.API_PORT)
    try:
        app.run(host=config.API_HOST, port=config.API_PORT, debug=False, use_reloader=False)
    finally:
        stop_event.set()
        mqtt_svc.stop()


if __name__ == "__main__":
    main()
