"""Session-based dashboard for whitelist management."""

from __future__ import annotations

import json
import logging
import queue
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)

from ..camera.services.register_user import capture_embed_and_save
from ..core import config
from ..core.component_status import build_dashboard_status
from ..data import db

log = logging.getLogger("web_ui")
_EVENTS_LOG = config.ARTIFACTS_DIR / "mqtt_published.jsonl"


def init_template_filters(app: Flask) -> None:
    """Jinja helpers for timestamps stored as Unix ints in SQLite."""

    @app.template_filter("registered_date")
    def _registered_date(ts: int | None) -> str:
        if ts is None:
            return ""
        try:
            sec = float(ts)
        except (TypeError, ValueError):
            return str(ts)
        dt = datetime.fromtimestamp(sec, tz=UTC)
        return dt.strftime("%b %d, %Y · %I:%M %p UTC")


def _login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login_form"))
        return view(*args, **kwargs)

    return wrapped


def init_app(app: Flask) -> None:
    """Register browser routes (/login, /dashboard) on the Flask app."""

    def _load_recent_notifications(limit: int = 40) -> list[dict]:
        """Read recently published MQTT events for initial bell history."""
        path = Path(_EVENTS_LOG)
        if not path.exists():
            return []
        out: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        item = json.loads(raw)
                    except Exception:
                        continue
                    if not isinstance(item, dict):
                        continue
                    topic = item.get("topic")
                    if topic != config.TOPIC_EVENTS:
                        continue
                    out.append({"topic": topic, "payload": item})
        except Exception:
            log.debug("failed loading recent notifications", exc_info=True)
            return []
        return out[-limit:][::-1]

    secret = config.SESSION_SECRET or "unset-use-SESSION_SECRET"
    app.secret_key = secret
    init_template_filters(app)

    def _render_dashboard(error: str | None = None, *, status_code: int = 200):
        with db.connect() as conn:
            users = db.list_users(conn)
            detection_alerts = db.list_recent_detection_alerts(conn, limit=40)
        mq = app.config.get("mqtt")
        detection_enabled = mq.detection_enabled if mq is not None else None
        return (
            render_template(
                "dashboard.html",
                users=users,
                detection_alerts=detection_alerts,
                detection_enabled=detection_enabled,
                error=error,
                mqtt_stream_url=url_for("dashboard_events_stream"),
                status_json_url=url_for("dashboard_status_json"),
                status_topic=config.TOPIC_STATUS,
            ),
            status_code,
        )

    def _wants_json() -> bool:
        return request.headers.get("X-Requested-With") == "XMLHttpRequest"

    @app.get("/")
    def root():
        if session.get("logged_in"):
            return redirect(url_for("dashboard"))
        return redirect(url_for("login_form"))

    @app.get("/login")
    def login_form():
        if session.get("logged_in"):
            return redirect(url_for("dashboard"))
        if not config.API_KEY:
            return (
                render_template(
                    "login.html",
                    error="Server misconfigured: set API_KEY in the environment before using the dashboard.",
                ),
                503,
            )
        return render_template("login.html")

    @app.post("/login")
    def login_submit():
        if not config.API_KEY:
            return (
                render_template(
                    "login.html",
                    error="Server misconfigured: set API_KEY in the environment before using the dashboard.",
                ),
                503,
            )

        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        if not email:
            return render_template("login.html", error="Email is required"), 400

        with db.connect() as conn:
            ok = db.verify_dashboard_login(conn, email, password)
        if not ok:
            return render_template("login.html", error="Invalid credentials"), 401

        session.clear()
        session["logged_in"] = True
        session["email"] = email
        return redirect(url_for("dashboard"))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login_form"))

    @app.get("/dashboard/events/stream")
    @_login_required
    def dashboard_events_stream():
        """SSE feed of payloads published on home/security/events only (same process MQTT client)."""
        hub = app.config.get("event_hub")
        if hub is None:
            return Response("event hub not configured", status=503, mimetype="text/plain")

        def generate():
            q = hub.subscribe()
            yield ": stream open\n\n"
            try:
                while True:
                    try:
                        line = q.get(timeout=25.0)
                        yield f"data: {line}\n\n"
                    except queue.Empty:
                        yield ": ping\n\n"
            finally:
                hub.unsubscribe(q)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/dashboard/status.json")
    @_login_required
    def dashboard_status_json():
        """MQTT status snapshot JSON for the authenticated dashboard."""
        return jsonify(build_dashboard_status(app))

    @app.get("/dashboard/events/recent.json")
    @_login_required
    def dashboard_recent_events_json():
        """Recent MQTT events snapshot used to prefill bell notifications."""
        return jsonify({"events": _load_recent_notifications(limit=40)})

    @app.get("/dashboard/fragments.json")
    @_login_required
    def dashboard_fragments_json():
        """HTML fragments + counts for live dashboard aside panels (users + detection alerts)."""
        with db.connect() as conn:
            users = db.list_users(conn)
            detection_alerts = db.list_recent_detection_alerts(conn, limit=40)
        return jsonify(
            {
                "users_html": render_template(
                    "partials/dashboard_users_inner.html", users=users
                ),
                "alerts_html": render_template(
                    "partials/dashboard_alerts_inner.html",
                    detection_alerts=detection_alerts,
                ),
                "users_count": len(users),
                "alerts_count": len(detection_alerts),
            }
        )

    @app.get("/dashboard")
    @_login_required
    def dashboard():
        body, code = _render_dashboard()
        if code == 200:
            return body
        return body, code

    @app.post("/dashboard/register")
    @_login_required
    def dashboard_register():
        name = (request.form.get("name") or "").strip()
        if not name:
            if _wants_json():
                return jsonify({"error": "Name is required"}), 400
            return _render_dashboard("Name is required", status_code=400)

        try:
            user_id, _jpeg = capture_embed_and_save(name)
        except ValueError as e:
            log.warning("dashboard register rejected: %s", e)
            if _wants_json():
                return jsonify({"error": str(e)}), 422
            return _render_dashboard(str(e), status_code=422)
        except Exception as e:
            log.exception("dashboard register capture failed")
            if _wants_json():
                return jsonify({"error": f"capture failed: {e}"}), 500
            return _render_dashboard(f"capture failed: {e}", status_code=500)

        mq = app.config.get("mqtt")
        if mq is not None:
            mq.publish_event(
                "user_registered",
                {"user_id": user_id, "name": name},
            )

        if _wants_json():
            return jsonify({"ok": True, "user_id": user_id})
        return redirect(url_for("dashboard"))

    @app.post("/dashboard/detection/toggle")
    @_login_required
    def dashboard_detection_toggle():
        enabled_value = (request.form.get("enabled") or "").strip().lower()
        if enabled_value not in {"on", "off"}:
            return _render_dashboard("Invalid detection toggle value", status_code=400)
        mq = app.config.get("mqtt")
        if mq is None:
            return _render_dashboard("MQTT service not initialized", status_code=503)
        mq.set_detection(enabled_value == "on")
        return redirect(url_for("dashboard"))

    @app.post("/dashboard/users/<int:user_id>/unregister")
    @_login_required
    def dashboard_unregister_user(user_id: int):
        with db.connect() as conn:
            user_name = db.get_user_name(conn, user_id)
            deleted = db.delete_user(conn, user_id)
        if not deleted:
            if _wants_json():
                return jsonify({"error": "User not found"}), 404
            return _render_dashboard("User not found", status_code=404)

        mq = app.config.get("mqtt")
        if mq is not None:
            mq.publish_event("user_unregistered", {"user_id": user_id, "name": user_name})

        if _wants_json():
            return jsonify({"ok": True, "user_id": user_id})
        return redirect(url_for("dashboard"))

    @app.get("/dashboard/users/<int:user_id>/photo")
    @_login_required
    def user_registration_photo(user_id: int):
        with db.connect() as conn:
            raw = db.get_registration_image(conn, user_id)
        if not raw:
            return Response("No registration photo stored.", status=404, mimetype="text/plain")

        return Response(raw, mimetype="image/jpeg")

    @app.get("/dashboard/alerts/<int:alert_id>/photo")
    @_login_required
    def dashboard_alert_photo(alert_id: int):
        with db.connect() as conn:
            raw = db.get_detection_alert_image(conn, alert_id)
        if not raw:
            return Response("No alert image stored.", status=404, mimetype="text/plain")
        return Response(raw, mimetype="image/jpeg")

    @app.post("/dashboard/alerts/<int:alert_id>/delete")
    @_login_required
    def dashboard_delete_alert(alert_id: int):
        with db.connect() as conn:
            deleted = db.delete_detection_alert(conn, alert_id)
        if not deleted:
            if _wants_json():
                return jsonify({"error": "Detection event not found"}), 404
            return _render_dashboard("Detection event not found", status_code=404)
        if _wants_json():
            return jsonify({"ok": True, "alert_id": alert_id})
        return redirect(url_for("dashboard"))
