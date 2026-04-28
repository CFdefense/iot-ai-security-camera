"""Session-based dashboard for whitelist management."""

from __future__ import annotations

import logging
from functools import wraps

from flask import (
    Flask,
    Response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import config
from .persistence import db
from .services.register_user import capture_embed_and_save

log = logging.getLogger("web_ui")


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

    @app.get("/dashboard")
    @_login_required
    def dashboard():
        with db.connect() as conn:
            users = db.list_users(conn)
        return render_template("dashboard.html", users=users)

    @app.post("/dashboard/register")
    @_login_required
    def dashboard_register():
        name = (request.form.get("name") or "").strip()
        if not name:
            with db.connect() as conn:
                users = db.list_users(conn)
            return (
                render_template(
                    "dashboard.html",
                    users=users,
                    error="Name is required",
                ),
                400,
            )

        try:
            user_id, _jpeg = capture_embed_and_save(name)
        except ValueError as e:
            with db.connect() as conn:
                users = db.list_users(conn)
            return (
                render_template(
                    "dashboard.html",
                    users=users,
                    error=str(e),
                ),
                422,
            )
        except Exception as e:
            log.exception("dashboard register capture failed")
            with db.connect() as conn:
                users = db.list_users(conn)
            return (
                render_template(
                    "dashboard.html",
                    users=users,
                    error=f"capture failed: {e}",
                ),
                500,
            )

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
