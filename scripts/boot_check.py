"""Boot check: verifies the FastAPI app can be created and DB migrated.

Usage: python scripts/boot_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as config_mod  # noqa: E402
from app import db as db_mod  # noqa: E402


def main() -> int:
    # Reset any cached state so we validate the real env config.
    config_mod.get_config.cache_clear()
    db_mod._db = None
    from app.main import create_app

    app = create_app()
    # Touch the lifespan via TestClient if available, else just build app.
    try:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            res = client.get("/api/health")
            assert res.status_code == 200
        print(f"boot OK — health 200, {len(app.routes)} routes, DB at {app.state.config.db_path}")
    except Exception as exc:
        print(f"app built but lifespan check failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
