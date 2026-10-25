"""Egress health-gate and proxy-death recovery regressions.

The bug: a configured BROWSER_PROXY that refuses connections was wired into
the browser at launch with no health check and no fallback, so EVERY page
navigation failed with NS_ERROR_PROXY_CONNECTION_REFUSED. These tests pin
the contract:

1. launch-time gate — an unreachable OR functionally broken proxy (wrong
   credentials, non-proxy listener) must not be wired into the browser;
   egress demotes to direct (policy permitting) with a note.
2. RELAY_ALLOW_DIRECT=false — a dead proxy must refuse to launch rather
   than silently bypass the owner's egress policy.
3. healthy proxy — egress stays "proxy" with no note.
4. mid-flight death — a proxy that passes the gate but dies later must be
   detected on navigation, demoted, and the navigation retried.
5. non-proxy navigation errors must NEVER trigger the fallback.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import browser_manager as bm
from app.proxy_spec import is_proxy_nav_error, probe_proxy


# ------------------------------------------------------------- test proxies
async def _tiny_connect_proxy() -> tuple[asyncio.AbstractServer, int]:
    """A real (minimal) HTTP CONNECT proxy for functional-gate tests."""

    async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            while True:
                d = await r.read(65536)
                if not d:
                    break
                w.write(d)
                await w.drain()
        except Exception:
            pass

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            if not line.startswith(b"CONNECT"):
                writer.write(b"HTTP/1.1 502 Not-A-Proxy-Target\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            target = line.split()[1].decode()
            host, _, p = target.partition(":")
            try:
                tr, tw = await asyncio.open_connection(host, int(p or 443))
            except Exception:
                writer.write(b"HTTP/1.1 502 Dial-Fail\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(pipe(reader, tw), pipe(tr, writer))
        except Exception:
            pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def _cfg(**over) -> SimpleNamespace:
    """Minimal BrowserManager config fake (only what these tests touch)."""
    base = {
        "browser_engine": "camoufox",
        "browser_proxy": "",
        "relay_allow_direct": True,
        "browser_headless": True,
        "browser_profiles_dir": Path("/tmp/rx-test-profiles"),
    }
    base.update(over)
    return SimpleNamespace(**base)


def _kdspy_fake() -> SimpleNamespace:
    """KDSpy stub: nothing configured, so launch skips its verifications."""
    return SimpleNamespace(
        firefox_addon_is_configured=lambda: False,
        firefox_addon_args=lambda: [],
        firefox_addon_state=lambda: None,
        record_firefox_runtime_validated=lambda *_: None,
    )


def _cfg(**over) -> SimpleNamespace:
    """Minimal BrowserManager config fake (only what these tests touch)."""
    base = {
        "browser_engine": "camoufox",
        "browser_proxy": "",
        "relay_allow_direct": True,
        "browser_headless": True,
        "browser_profiles_dir": Path("/tmp/rx-test-profiles"),
    }
    base.update(over)
    return SimpleNamespace(**base)


def _kdspy_fake() -> SimpleNamespace:
    """KDSpy stub: nothing configured, so launch skips its verifications."""
    return SimpleNamespace(
        firefox_addon_is_configured=lambda: False,
        firefox_addon_args=lambda: [],
        firefox_addon_state=lambda: None,
        record_firefox_runtime_validated=lambda *_: None,
    )


# --------------------------------------------------------------- probe_proxy
def _run_with_proxy(coro_fn):
    """Run ``coro_fn(server, port)`` with a live CONNECT proxy on ONE loop.

    The proxy's handler coroutines belong to the loop that created the
    server — a server made in one asyncio.run() is dead in the next (the
    kernel still accepts into the backlog, but nobody answers). Everything
    must live on a single loop.
    """

    def run():
        async def inner():
            server, port = await _tiny_connect_proxy()
            try:
                return await coro_fn(server, port)
            finally:
                server.close()

        return asyncio.run(inner())

    return run()


def test_probe_proxy_unreachable_port() -> None:
    """A closed local port reports unreachable (the incident's exact shape)."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()  # nothing is listening now

    ok, detail = asyncio.run(probe_proxy(f"127.0.0.1:{free_port}", timeout=1.0))
    assert ok is False
    assert "cannot reach proxy 127.0.0.1" in detail


def test_probe_proxy_reachable_proxy() -> None:
    """A working CONNECT proxy passes the functional gate."""

    async def check(server, port):
        return await probe_proxy(f"127.0.0.1:{port}", timeout=3.0)

    ok, detail = _run_with_proxy(check)
    assert ok is True, detail
    assert "CONNECT OK" in detail


def test_probe_proxy_rejects_non_proxy_listener() -> None:
    """A bare TCP listener (not a proxy) must FAIL the functional gate —
    this is exactly the 'port open, proxy broken' trap the old probe missed."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    host, port = s.getsockname()[:2]
    try:
        ok, detail = asyncio.run(probe_proxy(f"{host}:{port}", timeout=3.0))
    finally:
        s.close()
    assert ok is False


def test_probe_proxy_rejects_bad_credentials() -> None:
    """A proxy that answers 407 to the probe's credentials is unusable."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(reader.readline(), timeout=5)  # request line
            writer.write(
                b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n"
            )
            await writer.drain()
            writer.close()
        except Exception:
            pass

    async def run():
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        ok, detail = await probe_proxy(f"user:pass@127.0.0.1:{port}", timeout=3.0)
        server.close()
        return ok, detail

    ok, detail = asyncio.run(run())
    assert ok is False
    assert "407" in detail


def test_probe_proxy_invalid_spec() -> None:
    ok, detail = asyncio.run(probe_proxy("gopher://h:1"))
    assert ok is False
    assert "invalid proxy spec" in detail


# ----------------------------------------------------------- is_proxy_nav_error
@pytest.mark.parametrize(
    "msg,want",
    [
        ("Page.goto: NS_ERROR_PROXY_CONNECTION_REFUSED", True),
        ("Page.goto: NS_ERROR_UNKNOWN_PROXY_HOST", True),
        ("Page.goto: net::ERR_PROXY_CONNECTION_FAILED", True),
        ("Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://x", True),
        ("net::ERR_CONNECTION_REFUSED", False),  # site down, not proxy
        ("Navigation timeout of 30000 ms exceeded", False),
        ("net::ERR_NAME_NOT_RESOLVED", False),
        ("Execution context was destroyed", False),
    ],
)
def test_is_proxy_nav_error_classification(msg: str, want: bool) -> None:
    assert is_proxy_nav_error(msg) is want


# ---------------------------------------------------------------- launch gate
def _dead_proxy_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _fake_playwright_ctx() -> SimpleNamespace:
    ctx = SimpleNamespace(pages=[], browser=None, on=lambda *_: None)
    return ctx


@pytest.mark.asyncio
async def test_dead_proxy_falls_back_to_direct(monkeypatch) -> None:
    """Launch-time gate: dead proxy → direct egress, never wired in."""
    port = _dead_proxy_port()
    cfg = _cfg(
        browser_proxy=f"127.0.0.1:{port}",
    )
    mgr = bm.BrowserManager(cfg, kdspy=_kdspy_fake(), evidence=SimpleNamespace(), relay=None)
    mgr.profile_dir = SimpleNamespace(mkdir=lambda **kw: None)

    wired: dict = {}

    async def fake_launch_camoufox(relay_addr):
        wired["relay"] = relay_addr
        wired["proxy_arg"] = mgr._egress_mode
        mgr._context = _fake_playwright_ctx()
        mgr._engine_kind = "camoufox"

    monkeypatch.setattr(mgr, "_launch_camoufox", fake_launch_camoufox)

    await mgr.launch()

    assert mgr._egress_mode == "direct"
    assert "unusable" in (mgr._egress_note or "")
    # The launch path must see egress_mode == "direct" so it skips the proxy.
    assert wired["proxy_arg"] == "direct"
    st = mgr.status()
    assert st.egress_mode == "direct"
    assert st.egress_note and "unusable" in st.egress_note


@pytest.mark.asyncio
async def test_dead_proxy_no_direct_policy_refuses_launch(monkeypatch) -> None:
    """RELAY_ALLOW_DIRECT=false: dead proxy must abort launch, not bypass."""
    port = _dead_proxy_port()
    cfg = _cfg(
        browser_proxy=f"127.0.0.1:{port}",
        relay_allow_direct=False,
    )
    mgr = bm.BrowserManager(cfg, kdspy=_kdspy_fake(), evidence=SimpleNamespace(), relay=None)
    mgr.profile_dir = SimpleNamespace(mkdir=lambda **kw: None)

    async def fail_launch(relay_addr):  # pragma: no cover - must not be called
        raise AssertionError("browser must not launch on a dead proxy with direct disabled")

    monkeypatch.setattr(mgr, "_launch_camoufox", fail_launch)

    with pytest.raises(bm.BrowserManagerError) as ei:
        await mgr.launch()
    assert "unusable" in str(ei.value)
    assert "direct egress is disabled" in str(ei.value)


@pytest.mark.asyncio
async def test_healthy_proxy_keeps_proxy_egress(monkeypatch) -> None:
    """A functionally-working proxy stays wired (no regression for working setups)."""
    server, port = await _tiny_connect_proxy()
    try:
        cfg = _cfg(browser_proxy=f"127.0.0.1:{port}")
        mgr = bm.BrowserManager(cfg, kdspy=_kdspy_fake(), evidence=SimpleNamespace(), relay=None)
        mgr.profile_dir = SimpleNamespace(mkdir=lambda **kw: None)

        seen: dict = {}

        async def fake_launch_camoufox(relay_addr):
            seen["mode"] = mgr._egress_mode
            mgr._context = _fake_playwright_ctx()
            mgr._engine_kind = "camoufox"

        monkeypatch.setattr(mgr, "_launch_camoufox", fake_launch_camoufox)
        await mgr.launch()

        assert seen["mode"] == "proxy"
        assert mgr._egress_note is None
        assert mgr.status().egress_mode == "proxy"
    finally:
        server.close()


# ------------------------------------------------------------ mid-flight death
def _proxy_nav_exc() -> Exception:
    return bm.PlaywrightError("Page.goto: NS_ERROR_PROXY_CONNECTION_REFUSED")


@pytest.mark.asyncio
async def test_midflight_proxy_death_recovers_and_retries(monkeypatch) -> None:
    """Proxy healthy at launch, dead later: navigation demotes + retries once."""
    port = _dead_proxy_port()  # nothing listening: the death probe fails
    cfg = _cfg(
        browser_proxy=f"127.0.0.1:{port}",
        browser_navigation_timeout_seconds=5,
        browser_default_timeout_seconds=5,
        browser_interactive_viewport_w=390,
        browser_interactive_viewport_h=844,
        browser_viewport_w=1920,
        browser_viewport_h=1080,
        browser_max_open_pages=4,
    )
    mgr = bm.BrowserManager(cfg, kdspy=_kdspy_fake(), evidence=SimpleNamespace(), relay=None)
    mgr._egress_mode = "proxy"  # passed the gate at launch, then died
    mgr._engine_kind = "camoufox"

    page = SimpleNamespace(
        goto=None,
        close=...,
        set_default_timeout=lambda *_: None,
    )

    calls = {"goto": 0, "relaunch": 0, "fresh_page": 0, "closed": 0}

    async def goto_first_death(url, **kw):
        calls["goto"] += 1
        if calls["goto"] == 1:
            raise _proxy_nav_exc()
        return None  # retry succeeds after recovery

    page.goto = goto_first_death

    async def fake_new_page(*, interactive=False):
        calls["fresh_page"] += 1
        fresh = SimpleNamespace(
            goto=goto_first_death,
            close=...,
            set_default_timeout=lambda *_: None,
        )
        return fresh

    async def fake_close():
        calls["closed"] += 1

    page.close = fake_close

    async def fake_launch(force=False):
        calls["relaunch"] += 1

    monkeypatch.setattr(mgr, "new_page", fake_new_page)
    monkeypatch.setattr(mgr, "launch", fake_launch)

    returned = await mgr.navigate(page, "https://www.amazon.com/")

    # First goto died, context relaunched, retry on a FRESH page succeeded.
    assert calls["goto"] == 2
    assert calls["relaunch"] == 1
    assert calls["fresh_page"] == 1
    assert calls["closed"] == 1
    assert returned is not page  # the fresh page is returned
    assert mgr._egress_mode == "direct"
    assert "went away mid-session" in (mgr._egress_note or "")


@pytest.mark.asyncio
async def test_midflight_non_proxy_error_never_relaunches(monkeypatch) -> None:
    """Site-level failures must NOT trigger egress fallback (conservative)."""
    cfg = _cfg(
        browser_proxy="127.0.0.1:1",
        browser_navigation_timeout_seconds=5,
    )
    mgr = bm.BrowserManager(cfg, kdspy=_kdspy_fake(), evidence=SimpleNamespace(), relay=None)
    mgr._egress_mode = "proxy"

    async def goto(url, **kw):
        raise bm.PlaywrightError("net::ERR_CONNECTION_REFUSED")  # site down

    page = SimpleNamespace(goto=goto, close=...)
    relaunch = {"n": 0}

    async def fake_launch(force=False):
        relaunch["n"] += 1

    monkeypatch.setattr(mgr, "launch", fake_launch)

    with pytest.raises(bm.BrowserManagerError):
        await mgr.navigate(page, "https://down.example.com/")
    assert relaunch["n"] == 0
    assert mgr._egress_mode == "proxy"  # untouched


@pytest.mark.asyncio
async def test_midflight_recovery_requires_proxy_egress(monkeypatch) -> None:
    """No recovery when egress is relay/direct (signature can't mean proxy)."""
    cfg = _cfg(browser_navigation_timeout_seconds=5)
    mgr = bm.BrowserManager(cfg, kdspy=_kdspy_fake(), evidence=SimpleNamespace(), relay=None)
    mgr._egress_mode = "direct"

    async def goto(url, **kw):
        raise bm.PlaywrightError("Page.goto: NS_ERROR_PROXY_CONNECTION_REFUSED")

    page = SimpleNamespace(goto=goto, close=...)
    relaunch = {"n": 0}

    async def fake_launch(force=False):
        relaunch["n"] += 1

    monkeypatch.setattr(mgr, "launch", fake_launch)

    with pytest.raises(bm.BrowserManagerError):
        await mgr.navigate(page, "https://www.amazon.com/")
    assert relaunch["n"] == 0
    assert mgr._egress_mode == "direct"


# ------------------------------------------------ transient proxy throttling
def test_rate_limit_prefs_in_engine_launch_kwargs() -> None:
    """The engine must cap per-proxy tunnel concurrency + pacing prefs.

    A rate-limited CONNECT bridge answers parallel-tunnel bursts with 429;
    Firefox reports that as NS_ERROR_PROXY_CONNECTION_REFUSED. The prefs are
    the structural fix (no burst), the retry is the residual one.
    """
    from app import camoufox_engine

    prefs = camoufox_engine.FIREFOX_USER_PREFS
    assert prefs["network.http.max-persistent-connections-per-proxy"] <= 6
    assert prefs["network.http.pacing.requests.enabled"] is True
    assert prefs["network.http.speculative-parallel-limit"] == 0
    assert prefs["network.predictor.enabled"] is False
    assert prefs["network.prefetch-next"] is False


@pytest.mark.asyncio
async def test_transient_proxy_refusal_is_retried_once(monkeypatch) -> None:
    """Proxy refusal with the proxy STILL HEALTHY: one spaced retry on the
    same page succeeds (the rate-limited 429 shape, not a proxy death)."""
    cfg = _cfg(
        browser_proxy="127.0.0.1:59999",  # arbitrary; probe is monkeypatched
        browser_navigation_timeout_seconds=5,
    )
    mgr = bm.BrowserManager(cfg, kdspy=_kdspy_fake(), evidence=SimpleNamespace(), relay=None)
    mgr._egress_mode = "proxy"

    calls = {"goto": 0}

    async def goto(url, **kw):
        calls["goto"] += 1
        if calls["goto"] == 1:
            raise bm.PlaywrightError("Page.goto: NS_ERROR_PROXY_CONNECTION_REFUSED")
        return None

    page = SimpleNamespace(goto=goto, close=...)

    async def fake_probe(_spec):
        return True, "http 127.0.0.1 CONNECT OK"  # proxy is alive

    sleeps = {"n": 0}

    async def fake_sleep(_s):
        sleeps["n"] += 1

    monkeypatch.setattr(bm, "probe_proxy", fake_probe)
    monkeypatch.setattr(bm.asyncio, "sleep", fake_sleep)

    returned = await mgr.navigate(page, "https://www.amazon.com/ap/signin?x=1")

    assert calls["goto"] == 2  # one failure, one retry
    assert sleeps["n"] == 1  # spaced, not immediate
    assert returned is page  # same page — the context never died
    assert mgr._egress_mode == "proxy"  # no demotion for a live proxy


@pytest.mark.asyncio
async def test_proxy_refusal_with_dead_proxy_still_relaunches(monkeypatch) -> None:
    """Refusal + dead probe → the mid-flight death path (relaunch, fresh page),
    NOT the transient retry (the retry must never mask a real outage)."""
    cfg = _cfg(
        browser_proxy="127.0.0.1:59999",
        browser_navigation_timeout_seconds=5,
        browser_max_open_pages=4,
    )
    mgr = bm.BrowserManager(cfg, kdspy=_kdspy_fake(), evidence=SimpleNamespace(), relay=None)
    mgr._egress_mode = "proxy"
    mgr._engine_kind = "camoufox"

    async def goto(url, **kw):
        raise bm.PlaywrightError("Page.goto: NS_ERROR_PROXY_CONNECTION_REFUSED")

    page = SimpleNamespace(goto=goto, close=...)

    async def dead_probe(_spec):
        return False, "cannot reach proxy"

    relaunch = {"n": 0}

    async def fake_launch(force=False):
        relaunch["n"] += 1

    async def fake_new_page(*, interactive=False):
        fresh = SimpleNamespace(
            goto=lambda url, **kw: asyncio.sleep(0), close=..., set_default_timeout=lambda *_: None
        )

        async def _goto_ok(url, **kw):
            return None

        fresh.goto = _goto_ok
        return fresh

    monkeypatch.setattr(bm, "probe_proxy", dead_probe)
    monkeypatch.setattr(mgr, "launch", fake_launch)
    monkeypatch.setattr(mgr, "new_page", fake_new_page)

    returned = await mgr.navigate(page, "https://www.amazon.com/")

    assert relaunch["n"] == 1  # recovery path, not the transient retry
    assert mgr._egress_mode == "direct"
    assert returned is not page


@pytest.mark.asyncio
async def test_transient_retry_never_fires_for_site_errors(monkeypatch) -> None:
    """Non-proxy errors must not be retried (still conservative)."""
    cfg = _cfg(browser_proxy="127.0.0.1:59999", browser_navigation_timeout_seconds=5)
    mgr = bm.BrowserManager(cfg, kdspy=_kdspy_fake(), evidence=SimpleNamespace(), relay=None)
    mgr._egress_mode = "proxy"

    calls = {"goto": 0}

    async def goto(url, **kw):
        calls["goto"] += 1
        raise bm.PlaywrightError("net::ERR_CONNECTION_REFUSED")

    page = SimpleNamespace(goto=goto, close=...)

    async def fail_probe(_spec):  # pragma: no cover - must not be called
        raise AssertionError("probe must not run for non-proxy errors")

    monkeypatch.setattr(bm, "probe_proxy", fail_probe)

    with pytest.raises(bm.BrowserManagerError):
        await mgr.navigate(page, "https://down.example.com/")
    assert calls["goto"] == 1


@pytest.mark.asyncio
async def test_transient_retry_requires_proxy_egress(monkeypatch) -> None:
    """Direct/relay egress must never take the proxy retry path."""
    cfg = _cfg(browser_navigation_timeout_seconds=5)
    mgr = bm.BrowserManager(cfg, kdspy=_kdspy_fake(), evidence=SimpleNamespace(), relay=None)
    mgr._egress_mode = "direct"

    async def goto(url, **kw):
        raise bm.PlaywrightError("Page.goto: NS_ERROR_PROXY_CONNECTION_REFUSED")

    page = SimpleNamespace(goto=goto, close=...)

    async def fail_probe(_spec):  # pragma: no cover - must not be called
        raise AssertionError("probe must not run without proxy egress")

    monkeypatch.setattr(bm, "probe_proxy", fail_probe)

    with pytest.raises(bm.BrowserManagerError):
        await mgr.navigate(page, "https://www.amazon.com/")


# ------------------------------------------------------ camoufox kwarg plumbing
def test_camoufox_proxy_kwargs_skip_dead_proxy(monkeypatch) -> None:
    """egress_mode='direct' must produce NO proxy in camoufox launch kwargs."""
    from app import camoufox_engine

    cfg = SimpleNamespace(
        browser_proxy="user:pass@dead.host:1080",
        camoufox_humanize=False,
        camoufox_geoip=False,
        browser_headless=True,
        browser_profiles_dir=Path("/tmp/rx-camo-fp"),
        browser_content_processors=2,
    )
    # Avoid the fingerprint cache touching disk.
    monkeypatch.setattr(
        camoufox_engine,
        "load_or_generate_fingerprint",
        lambda cfg: SimpleNamespace(),
    )
    kwargs = camoufox_engine.launch_kwargs(cfg, egress_mode="direct")
    assert "proxy" not in kwargs

    kwargs2 = camoufox_engine.launch_kwargs(cfg, egress_mode="proxy")
    assert kwargs2["proxy"]["server"] == "http://dead.host:1080"
    assert kwargs2["proxy"]["username"] == "user"
