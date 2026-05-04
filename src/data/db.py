"""SQLite whitelist for known-user face embeddings.

The schema matches the Architecture: each row stores a user name plus a
128-dimensional face embedding (serialized as JSON for portability). Cosine
similarity is computed in Python rather than SQL.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from ..core import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    embedding  TEXT NOT NULL,
    created_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dashboard_users (
    email          TEXT PRIMARY KEY,
    password_hash  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS detection_alerts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type         TEXT NOT NULL,
    outcome            TEXT NOT NULL,
    confidence         REAL,
    image_ref          TEXT NOT NULL,
    capture_image      BLOB,
    matched_user_name  TEXT,
    reason             TEXT,
    created_ts         INTEGER NOT NULL
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open (and if needed create) the SQLite whitelist database.

    Args:
        db_path: Override path for the SQLite file. Defaults to ``config.DB_PATH``.

    Returns:
        A configured ``sqlite3.Connection`` with ``Row`` row factory and the
        ``users`` table guaranteed to exist.
    """
    path = Path(db_path) if db_path else config.DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate_users_registration_image(conn)
    _migrate_detection_alerts_capture_image(conn)
    conn.commit()
    return conn


def _migrate_users_registration_image(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "registration_image" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN registration_image BLOB")


def _migrate_detection_alerts_capture_image(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(detection_alerts)").fetchall()}
    if cols and "capture_image" not in cols:
        conn.execute("ALTER TABLE detection_alerts ADD COLUMN capture_image BLOB")


def _password_hash(password: str) -> str:
    """Deterministic hash for dashboard password storage (SHA-256 hex)."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def reset_dashboard_credentials_from_env(conn: sqlite3.Connection) -> None:
    """Clear ``dashboard_users`` and re-seed from ``USER_EMAIL`` / ``USER_PASSWORD``.

    Deletes all dashboard login rows so only the administrator defined in ``.env``
    exists. If ``USER_EMAIL`` is empty, the table is left empty until the next reset.
    """
    conn.execute("DELETE FROM dashboard_users")
    email = os.environ.get("USER_EMAIL", "").strip()
    pwd = os.environ.get("USER_PASSWORD", "")
    if email:
        conn.execute(
            "INSERT INTO dashboard_users (email, password_hash) VALUES (?, ?)",
            (email, _password_hash(pwd)),
        )
    conn.commit()


def verify_dashboard_login(conn: sqlite3.Connection, email: str, password: str) -> bool:
    """Return True when email/password match a dashboard_users row."""
    row = conn.execute(
        "SELECT password_hash FROM dashboard_users WHERE email = ?",
        (email.strip(),),
    ).fetchone()
    if row is None:
        return False
    return secrets.compare_digest(_password_hash(password), row["password_hash"])


def add_user(
    conn: sqlite3.Connection,
    name: str,
    embedding: Iterable[float],
    registration_image: bytes | None = None,
) -> int:
    """Insert a whitelist user and return the new row id.

    Args:
        conn: Open SQLite connection from :func:`connect`.
        name: Human-readable user label (e.g. ``"christian"``).
        embedding: Iterable of exactly 128 floats (OpenCV SFace feature vector).
        registration_image: Optional JPEG bytes stored in the DB (not on disk).

    Raises:
        ValueError: If ``embedding`` is not 128-dimensional.

    Returns:
        The auto-incremented ``users.id`` of the new row.
    """
    vec = [float(x) for x in embedding]
    if len(vec) != 128:
        raise ValueError(f"expected 128-d embedding, got {len(vec)}")
    cur = conn.execute(
        "INSERT INTO users (name, embedding, created_ts, registration_image) VALUES (?, ?, ?, ?)",
        (name, json.dumps(vec), int(time.time()), registration_image),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_users(conn: sqlite3.Connection) -> list[dict]:
    """Return all whitelisted users without their embeddings.

    Args:
        conn: Open SQLite connection from :func:`connect`.

    Returns:
        A list of dicts with ``id``, ``name``, ``created_ts``, and
        ``has_registration_image`` (bool).
    """
    rows = conn.execute(
        """
        SELECT id, name, created_ts,
               CASE WHEN registration_image IS NOT NULL THEN 1 ELSE 0 END AS has_registration_image
        FROM users ORDER BY id
        """
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["has_registration_image"] = bool(d.pop("has_registration_image", 0))
        out.append(d)
    return out


def get_registration_image(conn: sqlite3.Connection, user_id: int) -> bytes | None:
    """Return stored registration JPEG bytes for a user, or None."""
    row = conn.execute(
        "SELECT registration_image FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if row is None or row["registration_image"] is None:
        return None
    return bytes(row["registration_image"])


def delete_user(conn: sqlite3.Connection, user_id: int) -> bool:
    """Delete one registered user by id.

    Returns:
        True when a row was deleted, else False.
    """
    cur = conn.execute("DELETE FROM users WHERE id = ?", (int(user_id),))
    conn.commit()
    return int(cur.rowcount or 0) > 0


def get_user_name(conn: sqlite3.Connection, user_id: int) -> str | None:
    """Return user name for id, or None when missing."""
    row = conn.execute("SELECT name FROM users WHERE id = ?", (int(user_id),)).fetchone()
    if row is None:
        return None
    return str(row["name"])


def record_detection_alert(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    outcome: str,
    image_ref: str,
    confidence: float | None = None,
    capture_image: bytes | None = None,
    matched_user_name: str | None = None,
    reason: str | None = None,
) -> int:
    """Store one trigger outcome row and return the new alert id."""
    cur = conn.execute(
        """
        INSERT INTO detection_alerts (
            event_type, outcome, confidence, image_ref, capture_image,
            matched_user_name, reason, created_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_type,
            outcome,
            float(confidence) if confidence is not None else None,
            image_ref,
            capture_image,
            matched_user_name,
            reason,
            int(time.time()),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_recent_detection_alerts(conn: sqlite3.Connection, *, limit: int = 40) -> list[dict]:
    """Newest-first list of persisted detection outcomes for dashboard UI."""
    lim = max(1, int(limit))
    rows = conn.execute(
        """
        SELECT
            id, event_type, outcome, confidence, image_ref, matched_user_name, reason, created_ts,
            CASE WHEN capture_image IS NOT NULL THEN 1 ELSE 0 END AS has_capture_image
        FROM detection_alerts
        ORDER BY id DESC
        LIMIT ?
        """,
        (lim,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["has_capture_image"] = bool(d.pop("has_capture_image", 0))
        out.append(d)
    return out


def get_detection_alert_image(conn: sqlite3.Connection, alert_id: int) -> bytes | None:
    """Return raw JPEG bytes for a detection alert capture, if stored."""
    row = conn.execute(
        "SELECT capture_image FROM detection_alerts WHERE id = ?",
        (alert_id,),
    ).fetchone()
    if row is None or row["capture_image"] is None:
        return None
    return bytes(row["capture_image"])


def delete_detection_alert(conn: sqlite3.Connection, alert_id: int) -> bool:
    """Delete one detection alert row by id."""
    cur = conn.execute("DELETE FROM detection_alerts WHERE id = ?", (int(alert_id),))
    conn.commit()
    return int(cur.rowcount or 0) > 0


def _cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b, strict=True))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(y * y for y in b))
    if da == 0.0 or db == 0.0:
        return 0.0
    return num / (da * db)


def best_match(conn: sqlite3.Connection, embedding: Iterable[float]) -> tuple[str | None, float]:
    """Return (name, similarity) of the closest whitelist entry.

    Callers compare the similarity against config.MATCH_THRESHOLD to decide
    whether to grant access or publish an unknown_face_detected alert.
    """
    vec = [float(x) for x in embedding]
    best_name: str | None = None
    best_sim = 0.0
    for row in conn.execute("SELECT name, embedding FROM users").fetchall():
        sim = _cosine(vec, json.loads(row["embedding"]))
        if sim > best_sim:
            best_sim, best_name = sim, row["name"]
    return best_name, best_sim
