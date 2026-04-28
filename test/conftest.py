"""Shared pytest fixtures.

Every fixture redirects module-level paths (DB file, artifacts/) into a temp
dir so tests never clobber real state and can run in parallel.
"""

from __future__ import annotations

import pytest

from src.gateway import config


@pytest.fixture
def isolated_paths(monkeypatch, tmp_path):
    """Redirect config.* paths (DB, artifacts) into tmp_path."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "whitelist.sqlite")
    monkeypatch.setattr(config, "ARTIFACTS_DIR", artifacts)
    return tmp_path
