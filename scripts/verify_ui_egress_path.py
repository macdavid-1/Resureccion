"""Verifies the Settings → Test egress action end-to-end.

Drives the REAL FastAPI app through TestClient with BROWSER_PROXY pointed at
the instrumented demo proxy (Webshare stand-in). Proves the exact UI action
reports the proxy path rather than the direct datacenter path.

Usage: .venv/bin/python scripts/verify_ui_egress_path.py <proxy_host:port>

The owner password comes from the environment (RESURRECCION_OWNER_PW),
never hardcoded.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, ".")


def main() -> int:
    hostport = sys.argv[1]
    owner_pw = os.environ.get("RESURRECCION_OWNER_PW", "")
    if not owner_pw:
        print("RESURRECCION_OWNER_PW not set; cannot authenticate")
        return 2
    os.environ["BROWSER_PROXY"] = f"wsdemo-user:wsdemo-pass@{hostport}"
    os.environ["DATA_DIR"] = "/tmp/ws_ui_check_data"

    from app import config as cm

    cm.get_config.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as client:
        r = client.post("/api/auth/login", json={"username": "owner", "password": owner_pw})
        tok = r.json()["token"]
        H = {"X-Auth-Token": tok}

        st = client.get("/api/relay/status", headers=H).json()["relay"]
        print("relay enabled:", st["enabled"], "| device:", st["device_connected"])
        eg = client.post("/api/relay/test-egress", headers=H).json()
        print("UI Test-egress path:", eg["path"])
        print("UI Test-egress egress_ip:", eg["egress_ip"])
        print("results:", [(r_["host"], r_["ok"]) for r_ in eg["results"]])
        print("stats:", eg["stats"])
        assert eg["stats"]["fallback_proxy"] >= 1, eg["stats"]
        assert eg["path"] != "direct (server IP)"
        print("UI-PATH VERIFICATION OK")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
