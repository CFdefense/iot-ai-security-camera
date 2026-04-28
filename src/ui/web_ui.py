"""Session-based dashboard for whitelist management."""

from __future__ import annotations

import logging
import queue
from datetime import UTC, datetime
from functools import wraps

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

from ..core import config
from ..core.component_status import build_dashboard_status
from ..data import db
from ..camera.services.register_user import capture_embed_and_save

log = logging.getLogger("web_ui")


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
    secret = config.SESSION_SECRET or "unset-use-SESSION_SECRET"
    app.secret_key = secret
    init_template_filters(app)

    def _render_dashboard(error: str | None = None, *, status_code: int = 200):
        with db.connect() as conn:
            users = db.list_users(conn)
            detection_alerts = db.list_recent_detection_alerts(conn, limit=40)
        return (
            render_template(
                "dashboard.html",
                users=users,
                detection_alerts=detection_alerts,
                error=error,
                mqtt_stream_url=url_for("dashboard_events_stream"),
                status_json_url=url_for("dashboard_status_json"),
                status_topic=config.TOPIC_STATUS,
            ),
            status_code,
        )

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
            return _render_dashboard("Name is required", status_code=400)

        try:
            user_id, _jpeg = capture_embed_and_save(name)
        except ValueError as e:
            return _render_dashboard(str(e), status_code=422)
        except Exception as e:
            log.exception("dashboard register capture failed")
            return _render_dashboard(f"capture failed: {e}", status_code=500)

        mq = app.config.get("mqtt")
        if mq is not None:
            mq.publish_event(
                "user_registered",
                {"user_id": user_id, "name": name},
            )

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
