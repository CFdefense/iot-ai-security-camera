from src.camera.picam import imaging


def test_capture_frame_jpeg_returns_minimal_jpeg_bytes():
    """capture_frame_jpeg returns in-memory JPEG bytes (no captures/ directory)."""
    raw = imaging.capture_frame_jpeg()
    assert raw.startswith(b"\xff\xd8")
    assert raw.endswith(b"\xff\xd9")
    assert len(raw) > 8


def test_embed_face_returns_128_floats_in_range(tmp_path):
    """Stub embedding must still look shape-compatible with face_recognition output (128 floats in [-1, 1])."""
    img = tmp_path / "fake.jpg"
    img.write_bytes(b"binary image contents")
    emb = imaging.embed_face(img)
    assert len(emb) == 128
    assert all(isinstance(x, float) for x in emb)
    assert all(-1.0 <= x <= 1.0 for x in emb)


def test_embed_face_is_deterministic_for_same_bytes(tmp_path):
    """Same image bytes must yield the same embedding so the same user registers+matches consistently."""
    img = tmp_path / "x.jpg"
    img.write_bytes(b"hello world")
    assert imaging.embed_face(img) == imaging.embed_face(img)


def test_embed_face_differs_for_different_bytes(tmp_path):
    """Different images must produce different embeddings, otherwise best_match is meaningless."""
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")
    assert imaging.embed_face(a) != imaging.embed_face(b)
