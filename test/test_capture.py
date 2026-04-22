from src import capture


def test_capture_image_creates_jpg_in_captures_dir(isolated_paths):
    """capture_image writes a .jpg under config.CAPTURES_DIR so MQTT publishers have a valid image_ref."""
    path = capture.capture_image()
    assert path.exists()
    assert path.suffix == ".jpg"
    assert path.parent == isolated_paths / "captures"


def test_embed_face_returns_128_floats_in_range(tmp_path):
    """Stub embedding must still look shape-compatible with face_recognition output (128 floats in [-1, 1])."""
    img = tmp_path / "fake.jpg"
    img.write_bytes(b"binary image contents")
    emb = capture.embed_face(img)
    assert len(emb) == 128
    assert all(isinstance(x, float) for x in emb)
    assert all(-1.0 <= x <= 1.0 for x in emb)


def test_embed_face_is_deterministic_for_same_bytes(tmp_path):
    """Same image bytes must yield the same embedding so the same user registers+matches consistently."""
    img = tmp_path / "x.jpg"
    img.write_bytes(b"hello world")
    assert capture.embed_face(img) == capture.embed_face(img)


def test_embed_face_differs_for_different_bytes(tmp_path):
    """Different images must produce different embeddings, otherwise best_match is meaningless."""
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")
    assert capture.embed_face(a) != capture.embed_face(b)
