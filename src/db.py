"""SQLite whitelist for known-user face embeddings.

The schema matches the Architecture: each row stores a user name plus a
128-dimensional face embedding (serialized as JSON for portability). Cosine
similarity is computed in Python rather than SQL.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    embedding  TEXT NOT NULL,
    created_ts INTEGER NOT NULL
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
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def add_user(conn: sqlite3.Connection, name: str, embedding: Iterable[float]) -> int:
    """Insert a whitelist user and return the new row id.

    Args:
        conn: Open SQLite connection from :func:`connect`.
        name: Human-readable user label (e.g. ``"christian"``).
        embedding: Iterable of exactly 128 floats (face_recognition encoding).

    Raises:
        ValueError: If ``embedding`` is not 128-dimensional.

    Returns:
        The auto-incremented ``users.id`` of the new row.
    """
    vec = [float(x) for x in embedding]
    if len(vec) != 128:
        raise ValueError(f"expected 128-d embedding, got {len(vec)}")
    cur = conn.execute(
        "INSERT INTO users (name, embedding, created_ts) VALUES (?, ?, ?)",
        (name, json.dumps(vec), int(time.time())),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_users(conn: sqlite3.Connection) -> list[dict]:
    """Return all whitelisted users without their embeddings.

    Args:
        conn: Open SQLite connection from :func:`connect`.

    Returns:
        A list of dicts with ``id``, ``name`` and ``created_ts`` keys.
    """
    rows = conn.execute("SELECT id, name, created_ts FROM users ORDER BY id").fetchall()
    return [dict(r) for r in rows]


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
