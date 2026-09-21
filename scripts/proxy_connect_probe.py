"""Proxy-side CONNECT test: does the configured BROWSER_PROXY allow
CONNECT to Cloudflare's challenge hosts? Isolates proxy-side blocks from
browser-side problems. Reads BROWSER_PROXY from the environment; never
prints credentials.

Run: .venv/bin/python scripts/proxy_connect_probe.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, ".")

HOSTS = [
    "challenges.cloudflare.com",
    "brunhild.challenges.cloudflare.com",
    "api.ipify.org",
    "publishingaltitude.com",
]


async def probe_one(host: str) -> str:
    from app.relay import _dial_via_proxy

    spec = os.environ.get("BROWSER_PROXY", "")
    try:
        reader, writer = await asyncio.wait_for(
            _dial_via_proxy(spec, host, 443), timeout=15
        )
        writer.close()
        return f"  OK    {host} — proxy allowed the CONNECT"
    except Exception as exc:
        return f"  FAIL  {host}: {type(exc).__name__}: {exc}"


async def main() -> int:
    if not os.environ.get("BROWSER_PROXY"):
        print("BROWSER_PROXY not set")
        return 1
    print("Proxy CONNECT probe (credentials hidden):")
    results = [await probe_one(h) for h in HOSTS]
    for line in results:
        print(line)
    ok = all("OK" in line for line in results)
    print("PROXY PROBE:", "all hosts allowed" if ok else "some hosts REFUSED by the proxy")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
