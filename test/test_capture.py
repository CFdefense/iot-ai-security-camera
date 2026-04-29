"""Unit tests for imaging (capture / SFace path mocked — no hardware required)."""

from __future__ import annotations

import hashlib

import numpy as np

from src.camera.picam import imaging


def _deterministic_embedding_stub(raw: bytes) -> list[float]:
    """Shape-compatible 128-float vector from bytes (mirrors old hash-embedding tests)."""
    digest = hashlib.sha512(raw).digest()
    vals: list[float] = []
    while len(vals) < 128:
        digest = hashlib.sha512(digest).digest()
        for b in digest:
            vals.append((b / 255.0) * 2.0 - 1.0)
            if len(vals) >= 128:
                break
    return vals


def test_capture_frame_jpeg_returns_jpeg_bytes(monkeypatch):
    """``capture_frame_jpeg`` delegates to Picamera2; here we assert JPEG markers on mocked output."""
    jpeg = b"\xff\xd8\xff\xdb" + bytes(range(32)) + b"\xff\xd9"

    monkeypatch.setattr(imaging, "_capture_jpeg_via_picamera2", lambda: jpeg)
    raw = imaging.capture_frame_jpeg()
    assert raw.startswith(b"\xff\xd8")
    assert raw.endswith(b"\xff\xd9")
    assert len(raw) > 8


def test_embed_face_returns_128_floats_in_range(tmp_path, monkeypatch):
    """Embedding output is 128 floats in [-1, 1] (same DB shape as SFace vectors after stub)."""
    monkeypatch.setattr(imaging, "embed_face_bytes", _deterministic_embedding_stub)

    img = tmp_path / "fake.jpg"
    img.write_bytes(b"binary image contents")
    emb = imaging.embed_face(img)
    assert len(emb) == 128
    assert all(isinstance(x, float) for x in emb)
    assert all(-1.0 <= x <= 1.0 for x in emb)


def test_embed_face_is_deterministic_for_same_bytes(tmp_path, monkeypatch):
    """Same image bytes must yield the same embedding for stable registration + match."""
    monkeypatch.setattr(imaging, "embed_face_bytes", _deterministic_embedding_stub)

    img = tmp_path / "x.jpg"
    img.write_bytes(b"hello world")
    assert imaging.embed_face(img) == imaging.embed_face(img)


def test_embed_face_differs_for_different_bytes(tmp_path, monkeypatch):
    """Different images must produce different embeddings."""
    monkeypatch.setattr(imaging, "embed_face_bytes", _deterministic_embedding_stub)

    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")
    assert imaging.embed_face(a) != imaging.embed_face(b)


def test_embed_face_bytes_real_decode_path_imports(monkeypatch):
    """JPEG decode + ``embed_face_bgr_uint8`` combine to a 128-D vector."""
    fake_bgr = np.zeros((10, 10, 3), dtype=np.uint8)

    monkeypatch.setattr(imaging.cv2, "imdecode", lambda _a, _f: fake_bgr)

    def fake_embed_bgr(img: np.ndarray) -> list[float]:
        assert img.shape == (10, 10, 3)
        return [0.25] * 128

    monkeypatch.setattr(imaging, "embed_face_bgr_uint8", fake_embed_bgr)

    out = imaging.embed_face_bytes(b"\xff\xd8\xff\xd9")
    assert len(out) == 128
    assert all(abs(x - 0.25) < 1e-9 for x in out)
