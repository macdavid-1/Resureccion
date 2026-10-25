"""Regressions for the recurring NS_ERROR_PROXY_CONNECTION_REFUSED incident.

The owner hit this for days on BOTH sign-in URLs (KDSpy member portal and
Amazon /ap/signin):

    browser launch failed: navigation failed:
    Page.goto: NS_ERROR_PROXY_CONNECTION_REFUSED

Two independent defects enabled it:

1. FLAPPING-PROXY GAP — a rate-limited CONNECT bridge answers every
   functional probe with a clean handshake yet refuses the browser's real
   tunnels. `_recover_dead_proxy` saw the probe pass and returned "transient
   blip"; the single spaced retry failed; the raw refusal surfaced to the
   owner with NO escalation to direct egress. Ever. Fix: consecutive
   browser-level refusals (no successful navigation in between) outrank the
   probe — after 2, egress is force-demoted to direct, the context is
   relaunched WITHOUT the proxy, and the next launch's health gate honors
   the demotion instead of probing the refused proxy back in.

2. STALE PROFILE LOCK — a browser crash/kill left `lock` (symlink to
   ``host:+pid``) + `.parentlock` in the persistent profile; every later
   launch died with "Camoufox is already running, but is not responding"
   until the files were deleted by hand. Fix: before launch, clear the lock
   only when no live process holds it.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import browser_manager as bm


# ----------------------------------------------------------------- fakes
def _cfg(**over) -> SimpleNamespace:
    """Minimal BrowserManager config fake (only what these tests touch)."""
    base = {
        "browser_engine": "camoufox",
        "browser_proxy": "127.0.0.1:59999",  # arbitrary; probes are monkeypatched
        "relay_allow_direct": True,
        "browser_headless": True,
        "browser_profiles_dir": Path("/tmp/rx-test-profiles"),
        "browser_navigation_timeout_seconds": 5,
        "browser_default_timeout_seconds": 5,
        "browser_interactive_viewport_w": 390,
        "browser_interactive_viewport_h": 844,
        "browser_viewport_w": 1920,
        "browser_viewport_h": 1080,
        "browser_max_open_pages": 4,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _kdspy_fake() -> SimpleNamespace:
    return SimpleNamespace(
        firefox_addon_is_configured=lambda: False,
        firefox_addon_args=lambda: [],
        firefox_addon_state=lambda: None,
        record_firefox_runtime_validated=lambda *_: None,
    )


def _mgr(**over) -> bm.BrowserManager:
    cfg = _cfg(**over)
    mgr = bm.BrowserManager(cfg, kdspy=_kdspy_fake(), evidence=SimpleNamespace(), relay=None)
    mgr.profile_dir = SimpleNamespace(mkdir=lambda **kw: None)
    mgr._egress_mode = "proxy"
    mgr._engine_kind = "camoufox"
    return mgr


def _proxy_refused_page(calls: dict) -> SimpleNamespace:
    async def goto(url, **kw):
        calls["goto"] += 1
        raise bm.PlaywrightError("Page.goto: NS_ERROR_PROXY_CONNECTION_REFUSED")

    return SimpleNamespace(goto=goto, close=...)


def _healthy_probe(monkeypatch) -> None:
    async def fake_probe(_spec):
        return True, "http 127.0.0.1 CONNECT OK"

    monkeypatch.setattr(bm, "probe_proxy", fake_probe)


def _no_sleep(monkeypatch) -> None:
    async def fake_sleep(_s):
        return None

    monkeypatch.setattr(bm.asyncio, "sleep", fake_sleep)


# ------------------------------------------------------------- escalation
@pytest.mark.asyncio
async def test_flapping_proxy_escalates_to_direct_after_two_refusals(monkeypatch) -> None:
    """THE incident: probe keeps passing, browser keeps getting refused.

    Refusal #1 → transient spaced retry (proxy "healthy"). Refusal #2 → the
    streak proves the probe wrong: force-demote to direct, relaunch without
    the proxy, retry on a FRESH page. The owner must never see the refusal.
    """
    mgr = _mgr()
    calls = {"goto": 0}
    page = _proxy_refused_page(calls)

    _healthy_probe(monkeypatch)
    _no_sleep(monkeypatch)

    relaunch = {"n": 0}

    async def fake_launch_camoufox(relay_addr):
        # Runs through the REAL launch() so the health gate consumes
        # _egress_force_direct_reason exactly as in production.
        relaunch["n"] += 1
        mgr._context = SimpleNamespace(pages=[], browser=None, on=lambda *_: None)
        mgr._engine_kind = "camoufox"

    fresh_pages = {"n": 0}

    async def fake_new_page(*, interactive=False):
        fresh_pages["n"] += 1

        async def goto_ok(url, **kw):
            return None

        return SimpleNamespace(goto=goto_ok, close=..., set_default_timeout=lambda *_: None)

    async def fake_close_page(_p):
        return None

    monkeypatch.setattr(mgr, "_launch_camoufox", fake_launch_camoufox)
    monkeypatch.setattr(mgr, "new_page", fake_new_page)
    monkeypatch.setattr(mgr, "close_page", fake_close_page)

    returned = await mgr.navigate(page, "https://www.publishingaltitude.com/member/")

    # goto #1 refused, #2 (transient retry) refused, #3 (fresh page on
    # direct egress) must have succeeded — hence only 2 refusals recorded.
    assert calls["goto"] == 2
    assert relaunch["n"] == 1
    assert fresh_pages["n"] == 1
    assert returned is not page
    assert mgr._egress_mode == "direct"
    assert "refused" in (mgr._egress_note or "")
    assert "direct egress" in (mgr._egress_note or "")
    # The next launch gate must not probe the refused proxy back in.
    assert mgr._egress_force_direct_reason is None  # consumed by the relaunch


@pytest.mark.asyncio
async def test_single_refusal_does_not_escalate(monkeypatch) -> None:
    """One refusal with a passing probe is the transient retry's job."""
    mgr = _mgr()
    calls = {"goto": 0}

    async def goto(url, **kw):
        calls["goto"] += 1
        if calls["goto"] == 1:
            raise bm.PlaywrightError("Page.goto: NS_ERROR_PROXY_CONNECTION_REFUSED")
        return None

    page = SimpleNamespace(goto=goto, close=...)

    _healthy_probe(monkeypatch)
    _no_sleep(monkeypatch)

    relaunch = {"n": 0}

    async def fake_launch(force=False):  # pragma: no cover - must not run
        relaunch["n"] += 1

    monkeypatch.setattr(mgr, "launch", fake_launch)

    returned = await mgr.navigate(page, "https://www.amazon.com/")

    assert calls["goto"] == 2
    assert relaunch["n"] == 0
    assert returned is page  # same page — context never torn down
    assert mgr._egress_mode == "proxy"
    assert mgr._proxy_refusal_streak == 0  # success reset the streak


@pytest.mark.asyncio
async def test_escalation_respects_relay_allow_direct_false(monkeypatch) -> None:
    """RELAY_ALLOW_DIRECT=false: refusals surface, but never direct egress."""
    mgr = _mgr(relay_allow_direct=False)
    page = _proxy_refused_page({"goto": 0})

    _healthy_probe(monkeypatch)
    _no_sleep(monkeypatch)

    relaunch = {"n": 0}

    async def fake_launch(force=False):  # pragma: no cover - must not run
        relaunch["n"] += 1

    monkeypatch.setattr(mgr, "launch", fake_launch)

    with pytest.raises(bm.BrowserManagerError, match="navigation failed"):
        await mgr.navigate(page, "https://www.amazon.com/")

    assert relaunch["n"] == 0
    assert mgr._egress_mode == "proxy"  # policy preserved


@pytest.mark.asyncio
async def test_escalation_only_on_proxy_egress(monkeypatch) -> None:
    """Direct/relay egress must never take the escalation path."""
    mgr = _mgr(browser_proxy="")
    mgr._egress_mode = "direct"
    page = _proxy_refused_page({"goto": 0})

    _healthy_probe(monkeypatch)
    _no_sleep(monkeypatch)

    relaunch = {"n": 0}

    async def fake_launch(force=False):  # pragma: no cover - must not run
        relaunch["n"] += 1

    monkeypatch.setattr(mgr, "launch", fake_launch)

    with pytest.raises(bm.BrowserManagerError, match="navigation failed"):
        await mgr.navigate(page, "https://www.amazon.com/")
    assert relaunch["n"] == 0
    assert mgr._egress_mode == "direct"


@pytest.mark.asyncio
async def test_streak_resets_on_success_and_after_gap(monkeypatch) -> None:
    """A success (or a >120s gap) clears the consecutive-refusal streak, so
    a single old refusal can never ambush an unrelated navigation."""
    mgr = _mgr()
    _no_sleep(monkeypatch)

    mgr._note_refusal()
    mgr._note_refusal()
    assert mgr._proxy_refusal_streak == 2  # two consecutive: would escalate

    # A successful navigation resets it.
    async def goto_ok(url, **kw):
        return None

    await mgr._goto_resilient(SimpleNamespace(goto=goto_ok), "https://www.amazon.com/")
    assert mgr._proxy_refusal_streak == 0

    # Simulate a long gap between refusals: the streak restarts.
    mgr._proxy_refusal_last -= bm._PROXY_REFUSAL_STREAK_WINDOW + 1
    mgr._note_refusal()
    assert mgr._proxy_refusal_streak == 1


@pytest.mark.asyncio
async def test_launch_gate_honors_force_direct_without_probing(monkeypatch) -> None:
    """After a mid-session demotion, the next launch must NOT re-probe the
    refused proxy back into the browser (that rewiring is how the refusal
    kept returning after every recovery)."""
    mgr = _mgr()
    probes = {"n": 0}

    async def counting_probe(_spec):
        probes["n"] += 1
        return True, "http CONNECT OK"  # flapping bridge: probe passes now

    monkeypatch.setattr(bm, "probe_proxy", counting_probe)

    async def fake_launch_camoufox(relay_addr):
        mgr._context = SimpleNamespace(pages=[], browser=None, on=lambda *_: None)
        mgr._engine_kind = "camoufox"

    monkeypatch.setattr(mgr, "_launch_camoufox", fake_launch_camoufox)

    mgr._egress_force_direct_reason = "Browser kept getting refused; using direct egress"
    await mgr.launch()

    assert probes["n"] == 0  # verdict honored, not re-litigated
    assert mgr._egress_mode == "direct"
    assert "refused" in (mgr._egress_note or "")


# ------------------------------------------------------------- stale locks
def _unused_pid() -> int:
    pid = 3_000_000
    while os.path.exists(f"/proc/{pid}"):
        pid += 1
    return pid


def test_stale_profile_lock_is_cleared(tmp_path: Path) -> None:
    """A lock held by a DEAD pid (crash/kill) is removed before launch."""
    profile = tmp_path / "kdspy"
    profile.mkdir()
    stale_pid = _unused_pid()
    os.symlink(f"172.20.0.58:+{stale_pid}", profile / "lock")
    (profile / ".parentlock").write_bytes(b"")

    removed = bm._cleanup_stale_profile_lock(profile)

    assert removed is not None and str(stale_pid) in removed
    assert not (profile / "lock").exists()
    assert not (profile / ".parentlock").exists()


def test_live_pid_lock_is_preserved(tmp_path: Path) -> None:
    """A lock held by a LIVE process (this test process itself) is kept."""
    profile = tmp_path / "kdspy"
    profile.mkdir()
    os.symlink(f"localhost:+{os.getpid()}", profile / "lock")
    (profile / ".parentlock").write_bytes(b"")

    removed = bm._cleanup_stale_profile_lock(profile)

    assert removed is None
    assert (profile / "lock").is_symlink()
    assert (profile / ".parentlock").exists()


def test_unparseable_lock_is_cleared(tmp_path: Path) -> None:
    """A lock we cannot attribute to any live pid would brick the launch —
    clear it rather than preserve an unprovable lock."""
    profile = tmp_path / "kdspy"
    profile.mkdir()
    os.symlink("totally-bogus-target", profile / "lock")

    removed = bm._cleanup_stale_profile_lock(profile)

    assert removed is not None
    assert not (profile / "lock").exists()


def test_parentlock_only_is_cleared(tmp_path: Path) -> None:
    profile = tmp_path / "kdspy"
    profile.mkdir()
    (profile / ".parentlock").write_bytes(b"")

    assert bm._cleanup_stale_profile_lock(profile) == ".parentlock"
    assert not (profile / ".parentlock").exists()


def test_cleanup_never_raises_on_plain_dir(tmp_path: Path) -> None:
    """No lock files → no-op; weird profile objects must not break launch."""
    assert bm._cleanup_stale_profile_lock(tmp_path) is None
    # Non-path object (as some tests stub profile_dir): best-effort no-op.
    assert bm._cleanup_stale_profile_lock(SimpleNamespace()) is None


def test_pid_parsing_from_lock_target(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    os.symlink("172.20.0.58:+1181", lock)
    assert bm._pid_from_firefox_lock(lock) == 1181
    lock.unlink()
    os.symlink("+42", lock)
    assert bm._pid_from_firefox_lock(lock) == 42
    lock.unlink()
    os.symlink("172.20.0.58:1181", lock)  # port, not pid — unparseable
    assert bm._pid_from_firefox_lock(lock) is None


# ------------------------------------------------- direct-launch reset
@pytest.mark.asyncio
async def test_fresh_launch_resets_refusal_streak(monkeypatch) -> None:
    """A new context gives the proxy a clean slate (new streak)."""
    mgr = _mgr()
    mgr._proxy_refusal_streak = 2
    mgr._proxy_refusal_last = asyncio.get_event_loop().time() if False else 1.0

    _healthy_probe(monkeypatch)

    async def fake_launch_camoufox(relay_addr):
        mgr._context = SimpleNamespace(pages=[], browser=None, on=lambda *_: None)
        mgr._engine_kind = "camoufox"

    monkeypatch.setattr(mgr, "_launch_camoufox", fake_launch_camoufox)
    await mgr.launch()

    assert mgr._proxy_refusal_streak == 0
