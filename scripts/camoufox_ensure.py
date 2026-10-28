"""Boot helper: ensure Camoufox is usable — Python package AND browser binary.

Two distinct things can be missing, and both break every browser path (Amazon
sign-in, the KDSpy add-ons, and all research runs — there is no fallback
engine):

1. The ``camoufox`` Python package and ``browserforge`` (imported directly
   by the engine). A virtualenv built by hand, or interrupted mid-``pip
   install`` (a dropped SSH session leaves dist-info metadata behind, which
   makes the next install report "already satisfied" and exit 0 while the
   code is still missing), boots the app fine and only fails at the first
   browser launch. This script clears any partial install, reinstalls into
   the interpreter running it with pip's config redirects neutralized
   (``--isolated``), and prints the actual import error if the package still
   will not import — so the box heals itself at boot instead of requiring
   shell surgery.
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

from app.camoufox_engine import (  # noqa: E402
    CAMOUFOX_AVAILABLE,
    addons_supported,
    camoufox_cache_present,
)

INSTALL_SPEC = "camoufox[geoip]"
# The engine imports browserforge directly. camoufox declares it as a
# dependency, but naming it here repairs a partial install of EITHER package.
AUX_SPEC = "browserforge"


def _install_package() -> int:
    """Reinstall cleanly into the interpreter running this script.

    Best-effort uninstall first: stale metadata from an interrupted download
    otherwise makes pip report "already satisfied" (exit 0) while the package
    code is absent — the exact "installed but still does not import" state.
    ``--isolated`` ignores PIP_* env vars and pip.conf, which can silently
    redirect an install to a prefix the interpreter never reads.
    """
    print(
        f"[camoufox] python package missing — clearing any partial install, then "
        f"installing {INSTALL_SPEC} {AUX_SPEC} (needs network)",
        file=sys.stderr,
    )
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pip", "uninstall", "-y", "camoufox", AUX_SPEC],
        check=False,
        capture_output=True,
        text=True,
    )
    proc = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--isolated",
            "--no-input",
            INSTALL_SPEC,
            AUX_SPEC,
        ],
        check=False,
    )
    if proc.returncode != 0:
        print(f"[camoufox] pip install failed (exit {proc.returncode})", file=sys.stderr)
        return 1
    return 0


def _diagnose() -> None:
    """Print why a freshly installed package still will not import."""
    probe = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import traceback\n"
            "try:\n"
            "    import browserforge, camoufox, camoufox.async_api\n"
            "except BaseException:\n"
            "    traceback.print_exc()\n",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (probe.stderr or probe.stdout).strip()
    print(
        "[camoufox] the package was installed but still does not import. "
        "Actual import error:",
        file=sys.stderr,
    )
    print(detail or "(import succeeded in a subprocess — environment differs?)", file=sys.stderr)
    pip_ver = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pip", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    import site

    print(
        f"[camoufox] interpreter: {sys.executable} | prefix: {sys.prefix} "
        f"(base {sys.base_prefix})\n"
        f"[camoufox] pip: {(pip_ver.stdout or pip_ver.stderr).strip()}\n"
        f"[camoufox] site-packages: {', '.join(site.getsitepackages())}",
        file=sys.stderr,
    )
    print(
        "[camoufox] browser launches will fail until this is fixed — last resort: "
        "sudo sh scripts/vps_provision.sh rebuilds a damaged virtualenv "
        "(idempotent, never touches your settings), then re-run this script",
        file=sys.stderr,
    )


def main(argv: list[str]) -> int:
    if not CAMOUFOX_AVAILABLE:
        # Single attempt: CAMOUFOX_AVAILABLE was computed at import time, so
        # a freshly installed package is only importable in a re-exec'd run.
        if os.environ.get("CAMOUFOX_ENSURE_INSTALLED") == "1":
            _diagnose()
            return 1
        if _install_package() != 0:
            print(
                "[camoufox] could not install the package — browser launches will fail "
                f"until it is installed: {sys.executable} -m pip install "
                f"'{INSTALL_SPEC}' {AUX_SPEC}",
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
        # A present-but-stale build is still broken for KDSpy: builds without
        # the add-on loader silently ignore the addons config key, so the
        # browser works while the extension never loads. Upgrade in place.
        if addons_supported() is False:
            print(
                "[camoufox] browser build predates add-on support — "
                "upgrading (camoufox sync + fetch, ~80 MB if a newer build exists)",
                file=sys.stderr,
            )
            subprocess.run(  # noqa: S603
                [sys.executable, "-m", "camoufox", "sync"],
                check=False,
                capture_output=True,
                text=True,
            )
            proc = subprocess.run(  # noqa: S603
                [sys.executable, "-m", "camoufox", "fetch"],
                check=False,
            )
            if proc.returncode != 0:
                print(
                    f"[camoufox] `camoufox fetch` failed (exit {proc.returncode})",
                    file=sys.stderr,
                )
            if addons_supported() is False:
                print(
                    "[camoufox] the installed build STILL cannot load add-ons — "
                    "KDSpy will be recorded as failed at launch. Fix: "
                    "python -m camoufox fetch --replace (or remove ~/.cache/camoufox "
                    "and re-run this script)",
                    file=sys.stderr,
                )
                return 0
            print("[camoufox] browser binary upgraded — add-on loading supported")
            return 0
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
