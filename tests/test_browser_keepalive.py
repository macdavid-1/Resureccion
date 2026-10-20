"""Browser keepalive: hold the egress path open while the model is thinking.

A research iteration is dominated by the LLM call. Nothing loads in the
browser during that window, so the connection to the current host idles and a
middlebox can reap it — the next navigation then hangs instead of failing
fast. The keepalive sends one invisible same-origin HEAD request per interval.

These tests pin the two properties that matter: it actually pings while the
caller is inside the block, and it can never disturb the research run.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.browser_manager import BrowserManager
from tests.test_research_runner import session  # noqa: F401 — pytest fixture reuse


class _FakePage:
    def __init__(self, fail: bool = False) -> None:
        self.pings = 0
        self.fail = fail
        self.last_js = ""

    async def evaluate(self, js: str) -> None:
        if self.fail:
            raise RuntimeError("page went away")
        self.pings += 1
        self.last_js = js


class _FakeBrowserConn:
    is_connected = True


class _FakeContext:
    """Minimal persistent-context stand-in: `.browser` and `.pages`."""

    def __init__(self, pages: list) -> None:
        self.pages = pages
        # `browser` None => BrowserManager.is_running treats the context as live
        # without consulting a connection (mirrors a closed-over driver).
        self.browser = None


@pytest.fixture()
def cfg(tmp_path):
    from app.config import Config

    c = Config()
    c.data_dir = tmp_path / "data"
    c.browser_profiles_dir = c.data_dir / "profiles"
    c.browser_keepalive_seconds = 45
    return c


def _manager(cfg, pages: list) -> BrowserManager:
    mgr = BrowserManager(cfg, kdspy=None, evidence=None)  # type: ignore[arg-type]
    mgr._context = _FakeContext(pages)
    return mgr


class TestKeepalivePing:
    async def test_pings_on_the_configured_interval(self, cfg):
        page = _FakePage()
        mgr = _manager(cfg, [page])
        async with mgr.keepalive_during(interval=0.05):
            await asyncio.sleep(0.22)
        assert page.pings >= 2, f"expected repeated pings, got {page.pings}"

    async def test_ping_is_a_real_same_origin_head_request(self, cfg):
        """Must be genuine network traffic — not timers, not synthetic input."""
        page = _FakePage()
        mgr = _manager(cfg, [page])
        async with mgr.keepalive_during(interval=0.01):
            await asyncio.sleep(0.05)
        assert page.pings >= 1
        js = page.last_js
        assert "location.href" in js, "must target the page's own origin"
        assert "HEAD" in js, "must be a lightweight HEAD"
        assert "keepalive" in js
        # Page-local motion cannot hold a connection open; it must not be used.
        assert "mouse" not in js and "scroll" not in js

    async def test_disabled_at_zero_pings_nothing(self, cfg):
        cfg.browser_keepalive_seconds = 0
        page = _FakePage()
        mgr = _manager(cfg, [page])
        async with mgr.keepalive_during():
            await asyncio.sleep(0.15)
        assert page.pings == 0

    async def test_no_browser_pings_nothing_and_does_not_launch(self, cfg):
        mgr = BrowserManager(cfg, kdspy=None, evidence=None)  # type: ignore[arg-type]
        launched = False

        async def _no_launch(*a, **k):
            nonlocal launched
            launched = True

        mgr.launch = _no_launch  # type: ignore[method-assign]
        mgr._context = None
        async with mgr.keepalive_during(interval=0.01):
            await asyncio.sleep(0.05)
        assert launched is False, "keepalive must never launch a browser"

    async def test_dead_page_stops_cleanly_and_never_raises(self, cfg):
        """A crashed page stops pinging; the research run is never disturbed."""
        page = _FakePage(fail=True)
        mgr = _manager(cfg, [page])
        async with mgr.keepalive_during(interval=0.01):
            await asyncio.sleep(0.08)  # must not raise out of this block
        # It gave up rather than spinning on a dead page.
        await asyncio.sleep(0.05)
        assert page.pings == 0

    async def test_pings_stop_immediately_on_exit(self, cfg):
        """The block's exit must cancel the loop — no stray traffic after."""
        page = _FakePage()
        mgr = _manager(cfg, [page])
        async with mgr.keepalive_during(interval=0.02):
            await asyncio.sleep(0.07)
        seen = page.pings
        await asyncio.sleep(0.12)
        assert page.pings == seen, "keepalive kept running after the block exited"

    async def test_no_stale_task_left_behind(self, cfg):
        page = _FakePage()
        mgr = _manager(cfg, [page])
        before = {t for t in asyncio.all_tasks()}
        async with mgr.keepalive_during(interval=0.02):
            await asyncio.sleep(0.06)
        leaked = [
            t for t in asyncio.all_tasks()
            if t not in before and not t.done() and "keepalive" in (t.get_name() + str(t.get_coro()))
        ]
        assert not leaked, f"keepalive task leaked: {leaked}"


class TestConfigKnob:
    def test_default_is_well_inside_firefox_keepalive(self, cfg):
        """The default must beat the tightest real limit (~115s Firefox)."""
        from app.config import Config

        assert 0 < Config().browser_keepalive_seconds < 115

    def test_clamps_a_silly_value(self, monkeypatch, tmp_path):
        from app import config as config_mod
        from app.config import Config

        config_mod.get_config.cache_clear()
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
        monkeypatch.setenv("BROWSER_KEEPALIVE_SECONDS", "99999")
        config_mod.get_config.cache_clear()
        assert Config().browser_keepalive_seconds == 600

        monkeypatch.setenv("BROWSER_KEEPALIVE_SECONDS", "-5")
        config_mod.get_config.cache_clear()
        assert Config().browser_keepalive_seconds == 0, "negative means disabled"

        # A typo must be a loud boot error, never a silent fallback — the
        # same contract BROWSER_CONTENT_PROCESSORS documents.
        monkeypatch.setenv("BROWSER_KEEPALIVE_SECONDS", "not-a-number")
        config_mod.get_config.cache_clear()
        with pytest.raises(RuntimeError, match="must be an integer"):
            Config()


class TestRunnerWiresKeepalive:
    async def test_model_call_runs_inside_the_keepalive(self, db, config, session):
        """Integration: the runner must open the keepalive around each call."""
        from tests.test_research_runner import (
            PHASE_OUTPUTS,
            FakeBrowser,
            ScriptedModel,
            make_runner,
        )

        entered = {"n": 0}

        class _RecordingBrowser(FakeBrowser):
            @asynccontextmanager
            async def keepalive_during(self, interval=None):
                entered["n"] += 1
                yield self

        runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
        runner.browser = _RecordingBrowser()  # type: ignore[assignment]

        await runner.run(session)
        assert entered["n"] > 0, "the runner never opened a keepalive around the model"

    async def test_runner_falls_back_when_browser_lacks_keepalive(self, db, config, session):
        """Duck-typed: test doubles and probes without the method still run."""
        from tests.test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

        runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
        assert not hasattr(runner.browser, "keepalive_during")
        await runner.run(session)  # must not raise
