"""API contract check: every path the dashboard can call must exist.

`static/app.js` reaches the backend two ways — the `api(path, opts)` wrapper
and raw `fetch(...)`. This extracts every literal path those call, normalizes
dynamic `${...}` segments, and reports any call with no matching FastAPI
route. A mismatch means the UI would 404 at runtime while everything else
looks healthy.

DOM-id and button-handler coverage live in `frontend_binding_check.py`; this
script owns the *network* contract only.

Run:  python scripts/frontend_contract_check.py
Exit: 0 = no missing routes, 1 = at least one call has no backend route.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
ROUTES = ROOT / "app" / "routes"


def normalize(p: str) -> str:
    """Collapse a path to a comparable form: strip query, `{param}` -> `{}`."""
    p = p.strip().split("?")[0]
    p = re.sub(r"\{[^}]+\}", "{}", p)
    p = re.sub(r"/+", "/", p)
    return p.rstrip("/") or "/"


def backend_routes() -> set[str]:
    """`METHOD /path` for every route declared in app/routes/*.py."""
    routes: set[str] = set()
    for f in sorted(ROUTES.glob("*.py")):
        text = f.read_text(encoding="utf-8")
        prefix = ""
        m = re.search(r"APIRouter\([^)]*prefix\s*=\s*[\"']([^\"']+)[\"']", text, re.S)
        if m:
            prefix = m.group(1)
        for dm in re.finditer(
            r"@(?:router|app)\.(get|post|put|patch|delete)\(\s*[\"']([^\"']*)[\"']", text
        ):
            routes.add(f"{dm.group(1).upper()} {normalize(prefix + dm.group(2))}")
    return routes


def _template_path(expr: str) -> str:
    """`/x/${v}/y` -> /x/{} ; a dynamic tail truncates to its static prefix."""
    path = re.sub(r"\$\{[^}]+\}", "{}", expr)
    if "{" in path:
        path = path.split("{")[0]
    return normalize(path)


def frontend_calls() -> set[tuple[str, str]]:
    """(METHOD, normalized-path) pairs the frontend can issue."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    calls: set[tuple[str, str]] = set()
    for wrapper in (r"fetch", r"\bapi"):
        for m in re.finditer(
            rf"{wrapper}\(\s*([`\"'])(.+?)\1\s*(,\s*\{{(.{{0,300}}?)\}})?", js, re.S
        ):
            opts = m.group(4) or ""
            mm = re.search(r"method\s*:\s*[\"'](\w+)[\"']", opts, re.I)
            method = (mm.group(1) if mm else "GET").upper()
            calls.add((method, _template_path(m.group(2))))
    return calls


def matches(method: str, path: str, routes: set[str]) -> bool:
    """Exact, or any route under a truncated dynamic prefix."""
    if f"{method} {path}" in routes:
        return True
    return any(
        r.startswith(f"{method} {path.rstrip('/')}/") for r in routes
    )


def main() -> int:
    routes = backend_routes()
    calls = frontend_calls()
    missing = sorted(c for c in calls if not matches(c[0], c[1], routes))

    print(f"backend routes: {len(routes)}   frontend calls: {len(calls)}")
    print("\n=== frontend calls with NO backend route ===")
    if missing:
        for m, p in missing:
            print(f"  {m:6s} {p}")
    else:
        print("  (none)")

    # Routes nothing in the UI reaches — informative only: several are
    # legitimately driven by scripts, the relay stream, or settings toggles
    # built from a ternary the regex cannot see.
    called = {c[1] for c in calls}
    unused = sorted(
        r
        for r in routes
        if r.split(" ", 1)[1] not in called
        and not any(
            c != "/" and r.split(" ", 1)[1].startswith(c.rstrip("/")) for c in called
        )
    )
    print("\n=== backend routes the UI does not call (info) ===")
    for r in unused:
        print(f"  {r}")
    if not unused:
        print("  (none)")

    print(f"\nRESULT: {len(missing)} frontend calls with no backend route")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
