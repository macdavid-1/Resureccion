"""Shared test fixtures: isolated temp data dir + wired stores."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("OWNER_USERNAME", "owner")
os.environ.setdefault("OWNER_PASSWORD_HASH", "scrypt$00$00")  # placeholder; tests build their own

from app.config import Config
from app.db import Database


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.db_path = cfg.data_dir / "test.db"
    cfg.artifacts_dir = cfg.data_dir / "artifacts"
    cfg.uploads_dir = cfg.data_dir / "uploads"
    cfg.exports_dir = cfg.data_dir / "exports"
    cfg.reports_dir = cfg.data_dir / "reports"
    cfg.browser_profiles_dir = cfg.data_dir / "browser_profiles"
    cfg.browser_downloads_dir = cfg.data_dir / "browser_downloads"
    cfg.kdspy_extension_path = cfg.data_dir / "extensions" / "kdspy"
    cfg.kdspy_firefox_path = cfg.data_dir / "extensions" / "kdspy-firefox"
    cfg.owner_password_hash = None
    cfg.auth_secret = "test-secret"
    return cfg


@pytest.fixture()
def db(config: Config) -> Database:
    config.ensure_dirs()
    database = Database(config.db_path)
    database.connect()
    yield database
    database.close()


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """Authenticated-ready FastAPI TestClient with fresh DATA_DIR."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OWNER_USERNAME", "owner")
    from app.security import hash_password

    monkeypatch.setenv("OWNER_PASSWORD_HASH", hash_password("hunter2"))
    monkeypatch.setenv("AUTH_SECRET", "unit-test-secret")
    # Force config re-creation for the new env.
    from app import config as config_mod

    config_mod.get_config.cache_clear()
    from app import db as db_mod

    db_mod._db = None

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c
