"""Shared pytest fixtures.

Every fixture redirects module-level paths (DB file, captures/, artifacts/)
into a temp dir so tests never clobber real state and can run in parallel.
"""

from __future__ import annotations

import pytest

from src import config


@pytest.fixture
def isolated_paths(monkeypatch, tmp_path):
    """Redirect config.* paths (DB, captures, artifacts) into tmp_path."""
    captures = tmp_path / "captures"
    artifacts = tmp_path / "artifacts"
    captures.mkdir()
    artifacts.mkdir()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "whitelist.sqlite")
    monkeypatch.setattr(config, "CAPTURES_DIR", captures)
    monkeypatch.setattr(config, "ARTIFACTS_DIR", artifacts)
    return tmp_path
