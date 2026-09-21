"""Pre-purchase probe: is a candidate VPS/proxy IP good for Resurrección?

Run BEFORE buying (provider trial IP, or right after first login):

    .venv/bin/python scripts/vps_ip_probe.py 203.0.113.7
    .venv/bin/python scripts/vps_ip_probe.py host.example.com --proxy user:pass@host:port

Checks (from this sandbox and, when --proxy is given, through the candidate):
  1. Reachability of BOTH address families — IPv4 AND the IPv6-only
     Cloudflare challenge host (the exact failure Webshare had).
  2. IP classification: datacenter ASN vs ISP/residential, org, geo.
  3. Reputation signals: blocklist hits (Spamhaus etc. via DNSBL lookups),
     which predict CAPTCHA hostility.
Exits 0 when the IP is viable, 2 when it fails a critical check — so it can
gate a purchase or a deploy.
"""
from __future__ import annotations

import argparse
import asyncio
import socket
import sys

sys.path.insert(0, ".")

# A host with NO A record (IPv6-only) — the sharp edge that killed Webshare.
IPV6_ONLY_HOST = "brunhild.challenges.cloudflare.com"

DNSBL_ZONES = [
    "zen.spamhaus.org",
    "bl.spamcop.net",
    "b.barracudacentral.org",
]


def resolve(host: str) -> tuple[list[str], list[str]]:
    """Return (ipv4_list, ipv6_list) for host."""
    v4: list[str] = []
    v6: list[str] = []
    try:
        for info in socket.getaddrinfo(host, None):
            af, _, _, _, addr = info
            if af == socket.AF_INET:
                v4.append(addr[0])
            elif af == socket.AF_INET6:
                v6.append(addr[0])
    except socket.gaierror as exc:
        print(f"  DNS FAIL for {host}: {exc}")
    return sorted(set(v4)), sorted(set(v6))


async def tcp_connect(host: str, port: int = 443, timeout: float = 8.0) -> bool:
    try:
        fut = asyncio.open_connection(host, port)
        _, w = await asyncio.wait_for(fut, timeout=timeout)
        w.close()
        return True
    except Exception:
        return False


async def probe_via_proxy(spec: str, host: str, port: int) -> tuple[bool, str]:
    """Return (ok, detail). detail carries the proxy's refusal (407, 502…) so
    auth problems are never misread as routing problems."""
    from app.relay import _dial_via_proxy

    try:
        r, w = await asyncio.wait_for(_dial_via_proxy(spec, host, port), timeout=12)
        w.close()
        return True, "ok"
    except Exception as exc:
        msg = str(exc)
        if "407" in msg:
            return False, "proxy REJECTED the credentials (407)"
        if "502" in msg:
            return False, "proxy 502 (cannot reach that host — often missing IPv6)"
        return False, f"{type(exc).__name__}: {msg[:80]}"


def dnsbl_hits(ip: str) -> list[str]:
    """Reverse the IP and query DNSBL zones; listed == bad reputation signal."""
    if ":" in ip:
        return []  # most DNSBLs are v4-only; v6 reputation handled via org check
    rev = ".".join(reversed(ip.split(".")))
    hits = []
    for zone in DNSBL_ZONES:
        q = f"{rev}.{zone}"
        try:
            socket.gethostbyname(q)
            hits.append(zone)
        except socket.gaierror:
            pass
    return hits


def classify_ip(ip: str) -> dict[str, str]:
    """Best-effort org/geo via ipinfo.io (public data)."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"https://ipinfo.io/{ip}/json", timeout=10) as r:
            d = json.load(r)
        return {
            "org": d.get("org", "?"),
            "country": d.get("country", "?"),
            "city": d.get("city", "?"),
        }
    except Exception as exc:
        return {"org": f"lookup failed ({exc})", "country": "?", "city": "?"}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", help="candidate VPS IP or hostname")
    ap.add_argument(
        "--proxy",
        help="user:pass@host:port — when given, connectivity checks run THROUGH the candidate proxy",
    )
    args = ap.parse_args()

    failures: list[str] = []

    print(f"=== Probe: {args.target} ===")

    # 1. Address families present on the target itself.
    v4, v6 = resolve(args.target)
    print(f"  target A records:    {v4 or 'none'}")
    print(f"  target AAAA records: {v6 or 'none'}")
    if not v4 and not v6:
        return 2

    # 2. Egress connectivity — direct (this sandbox) and/or via the candidate.
    print(f"  connect 1.1.1.1:443        direct={await tcp_connect('1.1.1.1')} ", end="")
    if args.proxy:
        ok4p, d4 = await probe_via_proxy(args.proxy, "1.1.1.1", 443)
        print(f"via-proxy={ok4p} ({d4})")
        if not ok4p:
            failures.append(f"proxy basic connectivity failed: {d4}")
    else:
        print()

    ok6 = await tcp_connect(IPV6_ONLY_HOST)
    print(f"  connect {IPV6_ONLY_HOST}:443 (IPv6-only): direct={ok6}", end="")
    if args.proxy:
        ok6p, d6 = await probe_via_proxy(args.proxy, IPV6_ONLY_HOST, 443)
        print(f" via-proxy={ok6p} ({d6})")
        if not ok6p and "REJECTED" not in d6:
            failures.append(f"candidate proxy CANNOT reach IPv6-only hosts — Turnstile/KDSpy will fail ({d6})")
    else:
        print()
        if not v6:
            failures.append("target has no IPv6 — before buying, confirm the provider actually routes IPv6 from the VPS")

    # 3. Classification.
    probe_ip = v4[0] if v4 else (v6[0] if v6 else args.target)
    info = classify_ip(probe_ip)
    org = info["org"]
    dc_markers = ("leaseweb", "hetzner", "ovh", "digitalocean", "linode", "vultr",
                  "contabo", "aws", "amazon", "google", "azure", "scaleway", "choopa")
    is_dc = any(m in org.lower() for m in dc_markers)
    print(f"  org: {org} · {info['city']}, {info['country']} · class={'datacenter' if is_dc else 'possibly ISP/residential'}")
    if is_dc:
        print("    note: datacenter ASN — acceptable (dedicated IP builds its own reputation), but residential is stronger")

    # 4. Reputation: DNSBL hits.
    hits = dnsbl_hits(probe_ip)
    print(f"  DNSBL: {hits or 'clean'}")
    if hits:
        failures.append(f"IP is blocklisted in: {', '.join(hits)} — expect hostile CAPTCHA scoring")

    print("=== VERDICT:", "VIABLE" if not failures else "PROBLEM FOUND", "===")
    for f in failures:
        print(f"  !! {f}")
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
