"""Live smoke check: Camoufox persistent context launches, navigates, and
reports its spoofed identity. Guards the engine swap before it lands in the
app. Never reads env secrets.

Run: .venv/bin/python scripts/camoufox_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile


def _fix_home() -> str | None:
    """Two environment traps this check must survive:

    1. Firefox refuses to run as root under a non-root $HOME ("Running
       Camoufox as root in a regular user's session is not supported").
    2. The `camoufox fetch` download lives under the *user's* cache dir; when
       the shell runs as root, the package manager looks in /root/.cache and
       reports "not installed".

    Both are solved without re-downloading: locate the fetched binary under
    the owning user's cache and pass it via `executable_path` (Camoufox's
    supported override), while HOME points at a root-owned dir so the root
    check passes.
    """
    exe: str | None = None
    for pat in (
        os.path.expanduser("~/.cache/camoufox/browsers/*/*/camoufox-bin"),
        "/home/*/.cache/camoufox/browsers/*/*/camoufox-bin",
        "/root/.cache/camoufox/browsers/*/*/camoufox-bin",
    ):
        import glob as _g

        hits = sorted(_g.glob(pat))
        if hits:
            exe = hits[-1]
            break
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        os.environ.setdefault("HOME", "/root")
        os.environ.setdefault("XDG_CACHE_HOME", "/root/.cache")
        try:
            os.makedirs("/root/.camoufox", exist_ok=True)
        except OSError:
            pass
    return exe


async def main() -> int:
    exe = _fix_home()
    from camoufox.async_api import AsyncCamoufox

    tmp = tempfile.mkdtemp(prefix="camoufox_smoke_")
    kwargs: dict = dict(
        headless=True,
        persistent_context=True,
        user_data_dir=tmp,
        os="windows",
        block_webrtc=True,
        geoip=False,
        i_know_what_im_doing=True,
    )
    if exe:
        kwargs["executable_path"] = exe
    async with AsyncCamoufox(**kwargs) as ctx:
        page = await ctx.new_page()
        await page.goto("http://example.com", timeout=60000)
        print("title:", await page.title())
        print("UA:", (await page.evaluate("navigator.userAgent"))[:90])
        print("webdriver:", await page.evaluate("navigator.webdriver"))
        print("tz:", await page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone"))
        p2 = await ctx.new_page()
        print("pages:", len(ctx.pages))
        await p2.close()
    print("CAMOUFOX SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
