"""Register a whitelist user from camera capture."""

from __future__ import annotations

from ...data import db
from ..picam import imaging


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

    return user_id, stored_jpeg
