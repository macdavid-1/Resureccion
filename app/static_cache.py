"""Cache-safe static asset serving for the Resurrección UI.

The failure mode this prevents: the browser heuristically caches index.html,
which pins an old ``app.js?v=N`` on the owner's phone, so newly shipped
interactive features silently do not exist client-side ("buttons do nothing",
"taps do nothing") even though the server code is current.

Rules enforced here:

- The HTML document is always served with ``Cache-Control: no-cache``:
  it may be stored but must be revalidated before use, so every page load
  sees the current asset versions.
- Asset versions are **content hashes injected server-side** into the HTML,
  so a stale ``?v=`` reference after an app.js/styles.css change is
  structurally impossible.
- Versioned asset URLs (``/static/<file>?v=<hash>``) are served with
  ``Cache-Control: public, max-age=31536000, immutable`` — safe because the
  content hash changes whenever the bytes change.
- Unversioned asset requests get ``no-cache`` so they always revalidate.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

from fastapi import Request
from fastapi import Response

_ASSET_REF = re.compile(
    r'(?P<lead>src="/static/|href="/static/)(?P<file>[a-zA-Z0-9._-]+)(\?v=[a-zA-Z0-9]+)?"'
)

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"


def _content_version(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest[:10]


@lru_cache(maxsize=64)
def _cached_version(resolved: str, mtime_ns: int, size: int) -> str:
    # Keyed on stat signature so edits invalidate the cache naturally.
    return _content_version(Path(resolved))


def asset_version(static_dir: Path, filename: str) -> str:
    """Content-hash version for a static file (cached until it changes)."""
    path = static_dir / filename
    stat = path.stat()
    return _cached_version(str(path), stat.st_mtime_ns, stat.st_size)


def render_index(static_dir: Path) -> str:
    """Return index.html with every /static/ reference rewritten to ?v=<hash>."""
    html = (static_dir / "index.html").read_text(encoding="utf-8")

    def _sub(match: re.Match[str]) -> str:
        file = match.group("file")
        target = static_dir / file
        if not target.is_file():
            return match.group(0)
        return f'{match.group("lead")}{file}?v={asset_version(static_dir, file)}"'

    return _ASSET_REF.sub(_sub, html)


def index_response(static_dir: Path) -> Response:
    """Serve the rendered index.html; always revalidatable (no-cache)."""
    return Response(
        content=render_index(static_dir),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": NO_CACHE},
    )
