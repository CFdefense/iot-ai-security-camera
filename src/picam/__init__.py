"""Pi camera façade: capture pipelines and embeddings."""

from __future__ import annotations

from .imaging import capture_image, capture_registration_jpeg, embed_face, embed_face_bytes

__all__ = [
    "capture_image",
    "capture_registration_jpeg",
    "embed_face",
    "embed_face_bytes",
]
