"""Boot helper: ensure Camoufox is usable — Python package AND browser binary.

Two distinct things can be missing, and both break every browser path (Amazon
sign-in, the KDSpy add-ons, and all research runs — there is no fallback
engine):

1. The ``camoufox`` Python package. A virtualenv built by hand, or half-
   rebuilt after the ``ensurepip is not available`` failure, can have
   fastapi/uvicorn but miss just this one dependency — the app then boots
   fine and only dies at the first browser launch with "camoufox is not
   installed". This script installs the package into the interpreter running
   it, so the box heals itself at boot instead of requiring shell surgery.
2. The browser binary. A fresh VPS has no ``~/.cache/camoufox``, so the first
   launch would fail with "official/stable is not installed. Please run
   `camoufox fetch`". ``run.sh`` calls this before starting uvicorn so the
   owner never sees that either.

Idempotent and fast when both are already present.

Exit codes: 0 = browser present (or just fetched), 1 = unavailable.

Run:  python scripts/camoufox_ensure.py [--fetch]
      --fetch  download even if a present install was found (upgrade path)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.camoufox_engine import CAMOUFOX_AVAILABLE, camoufox_cache_present  # noqa: E402

INSTALL_SPEC = "camoufox[geoip]"


def _install_package() -> int:
    """Install camoufox into the interpreter running this script (one-time)."""
    print(
        f"[camoufox] python package missing — installing {INSTALL_SPEC} "
        "(one-time, needs network)",
        file=sys.stderr,
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pip", "install", "--no-input", "--quiet", INSTALL_SPEC],
        check=False,
    )
    return 0 if proc.returncode == 0 else 1


def main(argv: list[str]) -> int:
    if not CAMOUFOX_AVAILABLE:
        # Single attempt: CAMOUFOX_AVAILABLE was computed at import time, so
        # a freshly installed package is only importable in a re-exec'd run.
        if os.environ.get("CAMOUFOX_ENSURE_INSTALLED") == "1":
            print(
                "[camoufox] the package was installed but still does not import — "
                f"check that pip and python are the same interpreter ({sys.executable}); "
                "browser launches will fail until fixed",
                file=sys.stderr,
            )
            return 1
        if _install_package() != 0:
            print(
                "[camoufox] could not install the package — browser launches will fail "
                f"until it is installed: {sys.executable} -m pip install '{INSTALL_SPEC}'",
                file=sys.stderr,
            )
            return 1
        env = {**os.environ, "CAMOUFOX_ENSURE_INSTALLED": "1"}
        os.execve(
            sys.executable,
            [sys.executable, str(Path(__file__).resolve()), *argv],
            env,
        )

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
