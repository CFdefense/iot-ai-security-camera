import pytest

from src.data import db


@pytest.fixture
def conn(tmp_path):
    """Open a fresh SQLite whitelist DB in a temp dir for the duration of one test."""
    c = db.connect(tmp_path / "test.sqlite")
    yield c
    c.close()


def test_connect_creates_users_table(conn):
    """connect() must idempotently create the users table on a fresh DB file."""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'").fetchall()
    assert len(rows) == 1


def test_add_user_returns_id_and_persists(conn):
    """add_user returns the new row id and list_users surfaces the name without leaking embeddings."""
    uid = db.add_user(conn, "alice", [0.1] * 128)
    assert uid == 1
    users = db.list_users(conn)
    assert [u["name"] for u in users] == ["alice"]
    assert "embedding" not in users[0]


def test_add_user_rejects_wrong_dim(conn):
    """Embeddings must be 128-d (face_recognition output); anything else is a programmer error."""
    with pytest.raises(ValueError, match="128-d"):
        db.add_user(conn, "bob", [0.1] * 10)


def test_best_match_returns_identical_vector_with_similarity_one(conn):
    """Cosine similarity of a vector with itself is 1.0; best_match must surface that cleanly."""
    vec = [0.1] * 128
    db.add_user(conn, "alice", vec)
    name, sim = db.best_match(conn, vec)
    assert name == "alice"
    assert sim == pytest.approx(1.0)


def test_best_match_picks_closest_of_many(conn):
    """With multiple whitelist entries, best_match must return the one with highest cosine sim."""
    a = [1.0, 0.0] + [0.0] * 126
    b = [0.0, 1.0] + [0.0] * 126
    db.add_user(conn, "alice", a)
    db.add_user(conn, "bob", b)
    query = [0.1, 0.9] + [0.0] * 126
    name, sim = db.best_match(conn, query)
    assert name == "bob"
    assert sim > 0.9


def test_best_match_on_empty_db_returns_none(conn):
    """No users registered yet: best_match must return (None, 0.0) so callers treat it as unknown."""
    name, sim = db.best_match(conn, [0.1] * 128)
    assert name is None
    assert sim == 0.0


def test_list_users_is_ordered_by_id(conn):
    """list_users returns rows in insertion order so UIs can show a stable registration history."""
    db.add_user(conn, "alice", [0.1] * 128)
    db.add_user(conn, "bob", [0.2] * 128)
    names = [u["name"] for u in db.list_users(conn)]
    assert names == ["alice", "bob"]
