"""Register a whitelist user from camera capture."""

from __future__ import annotations

import logging

from ...core.task_logging import TASK_LEVEL
from ...data import db
from ..picam import imaging

log = logging.getLogger("register_user")


def capture_embed_and_save(name: str) -> tuple[int, bytes]:
    """Capture JPEG, embed, persist. Returns (user_id, jpeg_bytes).

    Raises:
        ValueError: From embed Face pipeline (e.g. no face).
    """
    jpeg = imaging.capture_registration_jpeg()
    embedding = imaging.embed_face_bytes(jpeg)
    stored_jpeg = imaging.normalize_stored_jpeg(jpeg)

    with db.connect() as conn:
        user_id = db.add_user(conn, name, embedding, registration_image=stored_jpeg)

    log.log(TASK_LEVEL, "registration: user '%s' captured as id=%s", name, user_id)
    return user_id, stored_jpeg
