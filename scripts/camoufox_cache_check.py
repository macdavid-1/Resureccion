"""Reproduce + verify the preview-launch cache bug and its fix.

The bug: the preview server process runs with a different HOME than the
account that ran `camoufox fetch`, so camoufox resolves an empty cache and
launch fails with "official/stable is not installed. Please run
`camoufox fetch` to install." The fix: app.camoufox_engine locates the real
cache under /home/*/.cache and pins XDG_CACHE_HOME before camoufox is
imported (see _ensure_camoufox_cache_env).

Run: .venv/bin/python scripts/camoufox_cache_check.py

Both checks run subprocesses with HOME=/root (the preview process's view):

- raw      — imports camoufox directly, no app code. Expected: the exact
             user-facing error (reproduces the bug report).
- fixed    — imports app.camoufox_engine first. Expected: the binary is
             found and the version string prints.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PY = str(Path(sys.executable))

RAW_SNIPPET = (
    "import camoufox.pkgman as p\n"
    "print('INSTALL_DIR:', p.INSTALL_DIR)\n"
    "try:\n"
    "    print('verstr:', p.installed_verstr())\n"
    "except Exception as exc:\n"
    "    print('ERROR:', exc)\n"
    "    raise SystemExit(1)\n"
)

APP_SNIPPET = (
    "import asyncio, tempfile\n"
    "from app import camoufox_engine as ce\n"
    "print('engine available:', ce.CAMOUFOX_AVAILABLE)\n"
    "import camoufox.pkgman as p\n"
    "print('INSTALL_DIR:', p.INSTALL_DIR)\n"
    "print('verstr:', p.installed_verstr())\n"
    "async def _go():\n"
    "    tmp = tempfile.mkdtemp(prefix='cache_check_')\n"
    "    async with ce.AsyncCamoufox(headless=True, persistent_context=True,\n"
    "                                user_data_dir=tmp, os='windows',\n"
    "                                i_know_what_im_doing=True) as ctx:\n"
    "        page = await ctx.new_page()\n"
    "        await page.goto('https://example.com', timeout=60000)\n"
    "        print('launched + navigated:', page.url)\n"
    "asyncio.run(_go())\n"
)


def _run(snippet: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PY, "-c", snippet],
        capture_output=True,
        text=True,
        timeout=120,
        # Simulate the preview server process: root-ish HOME without the
        # downloaded browser in its cache.
        env={"HOME": "/root", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    )


def main() -> int:
    print("== raw camoufox import (pre-fix behavior) ==")
    raw = _run(RAW_SNIPPET)
    print(raw.stdout.strip())
    reproduced = raw.returncode != 0 and "not installed" in (raw.stdout + raw.stderr)
    print("reproduced the reported error:", reproduced)

    print("\n== via app.camoufox_engine (the fix) ==")
    fixed = _run(APP_SNIPPET)
    print(fixed.stdout.strip())
    if fixed.returncode != 0:
        print("stderr:", fixed.stderr.strip()[-400:])
        print("FAIL: the fix did not resolve the install")
        return 1

    print("\nCAMOUFOX CACHE CHECK OK"
          + (" (bug reproduced pre-fix, resolved post-fix)" if reproduced else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
