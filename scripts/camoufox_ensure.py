"""Boot helper: ensure the Camoufox browser binary is present.

A fresh VPS has no ``~/.cache/camoufox``, so the very first browser launch
fails with "official/stable is not installed. Please run `camoufox fetch`".
``run.sh`` calls this before starting uvicorn so the owner never sees that
error. It is idempotent and fast when the binary already exists.

Exit codes: 0 = browser present (or just fetched), 1 = unavailable.

Run:  python scripts/camoufox_ensure.py [--fetch]
      --fetch  download even if a present install was found (upgrade path)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.camoufox_engine import CAMOUFOX_AVAILABLE, camoufox_cache_present  # noqa: E402


def main(argv: list[str]) -> int:
    if not CAMOUFOX_AVAILABLE:
        print(
            "[camoufox] python package not installed — "
            "run: pip install 'camoufox[geoip]'",
            file=sys.stderr,
        )
        return 1

    force = "--fetch" in argv
    if not force and camoufox_cache_present():
        print("[camoufox] browser binary present")
        return 0

    print("[camoufox] browser binary missing — fetching (one-time, ~80 MB)")
    try:
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "camoufox", "fetch"],
            check=False,
        )
    except OSError as exc:
        print(f"[camoufox] could not run `camoufox fetch`: {exc}", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(
            f"[camoufox] `camoufox fetch` failed (exit {proc.returncode})",
            file=sys.stderr,
        )
        return 1
    if not camoufox_cache_present():
        print(
            "[camoufox] fetch reported success but no browser was found",
            file=sys.stderr,
        )
        return 1
    print("[camoufox] browser binary ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
