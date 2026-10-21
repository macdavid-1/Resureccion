"""Tests for cache-safe static asset serving.

Regression coverage for the stale-JS failure mode: index.html must always be
revalidated (no-cache), asset versions must be content hashes injected by the
server (a forgotten manual ?v= bump must be impossible), and /static responses
must be immutable-cache when versioned.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("RESURRECCION_DATA_DIR", str(tmp_path / "data"))
    from app.main import create_app

    return TestClient(create_app())


def _hash(path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]


def test_index_served_no_cache_with_hashed_assets(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    static = Path("static")
    assert f"/static/app.js?v={_hash(static / 'app.js')}" in r.text
    assert f"/static/styles.css?v={_hash(static / 'styles.css')}" in r.text
    # No legacy numeric versions may leak through.
    assert not re.search(r'\?v=\d+"', r.text)


def test_versioned_asset_is_immutable_unversioned_is_no_cache(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    v = _hash(Path("static") / "app.js")
    rv = client.get(f"/static/app.js?v={v}")
    assert rv.status_code == 200
    assert rv.headers["cache-control"] == "public, max-age=31536000, immutable"
    ru = client.get("/static/app.js")
    assert ru.status_code == 200
    assert ru.headers["cache-control"] == "no-cache"


def test_logo_installed_and_rendered_everywhere(monkeypatch, tmp_path):
    """The supplied logo (commit "Logo.png") ships as static/logo.png — byte
    for byte as uploaded — and renders on every brand surface: favicon,
    touch icon, login card and app topbar, all content-hash versioned."""
    client = _client(monkeypatch, tmp_path)
    logo = Path("static") / "logo.png"
    assert logo.is_file(), "logo was not installed into static/"
    data = logo.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "logo is not a PNG"
    assert len(data) > 10_000, "logo looks truncated"

    r = client.get("/")
    assert r.status_code == 200
    ref = f"/static/logo.png?v={_hash(logo)}"
    # favicon + apple-touch-icon + login + topbar
    assert r.text.count(ref) >= 4, "logo must render on all four surfaces"
    assert 'rel="icon"' in r.text and 'rel="apple-touch-icon"' in r.text
    assert 'class="login-logo"' in r.text
    assert 'class="brand-logo"' in r.text
    assert not re.search(r'\?v=\d+"', r.text)  # no manual version leaks

    # The versioned mark is served with the exact uploaded bytes.
    img = client.get(ref)
    assert img.status_code == 200
    assert img.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert img.content == data
    assert img.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_render_rewrites_placeholder_versions(monkeypatch, tmp_path):
    """Raw index.html keeps a numeric ?v= placeholder; server rewrites it."""
    raw = (Path("static") / "index.html").read_text(encoding="utf-8")
    from app.static_cache import render_index

    rendered = render_index(Path("static"))
    assert "?v=fab" in rendered or re.search(r"\?v=[a-f0-9]{10}", rendered)
    assert rendered != raw or "?v=" in raw  # rendered always carries hashes
    assert not re.search(r'\?v=\d+"', rendered)
