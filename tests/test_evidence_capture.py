"""Tests for evidence-capture extraction utilities (pure functions)."""
from __future__ import annotations

import pytest

from app.evidence_capture import (
    EvidenceCaptureError,
    EvidenceCollector,
    _extract_kdspy_panel,
    _first_price,
    _parse_int,
    _parse_rating,
    _stamp,
)
from app.marketplace import AMAZON_MARKETPLACES


def test_price_extraction() -> None:
    assert _first_price("$12.99") == "$12.99"
    assert _first_price("US $1,299.00") == "US $1,299.00"
    assert _first_price("€45,90") == "€45,90"
    assert _first_price("£9.99 now") == "£9.99"
    assert _first_price("no price here") is None


def test_rating_extraction() -> None:
    assert _parse_rating("4.5 out of 5 stars") == 4.5
    assert _parse_rating("4.7/5") == 4.7
    assert _parse_rating(" 4.0 ") == 4.0
    assert _parse_rating("nope") is None


def test_int_extraction() -> None:
    assert _parse_int("12,345 ratings") == 12345
    assert _parse_int("#42 in Books") == 42
    assert _parse_int("") is None


def test_stamp_shape() -> None:
    s = _stamp()
    assert len(s) == 16 and s.endswith("Z") and "T" in s


class _FakeFrame:
    def __init__(self, url: str, body: str) -> None:
        self.url = url
        self._body = body

    async def inner_text(self, _sel: str) -> str:
        return self._body


class _FakePage:
    def __init__(self, frames: list[_FakeFrame]) -> None:
        self.frames = frames

    async def query_selector(self, _sel: str):
        return None


async def test_extract_panel_finds_moz_extension_frames() -> None:
    """The engine is Firefox: extension frames are moz-extension://, and the
    Chrome-era chrome-extension-only check left the agent blind to panels."""
    page = _FakePage(
        [
            _FakeFrame("https://www.amazon.com/s?k=x", ""),
            _FakeFrame("moz-extension://abcd-1234/pages/panel.html", "KDSpy panel data"),
        ]
    )
    out = await _extract_kdspy_panel(page)
    assert out is not None
    assert out["source"] == "extension_frame"
    assert out["frame_url_host"] == "abcd-1234"
    assert "KDSpy panel data" in out["text_excerpt"]


async def test_extract_panel_returns_none_without_extension_frames() -> None:
    assert await _extract_kdspy_panel(_FakePage([])) is None


class _SearchPage:
    """A search results page whose printed total is the one under test."""

    def __init__(self, body: str) -> None:
        self.url = "https://www.amazon.com/s?k=x"
        self._body = body

    async def inner_text(self, _sel: str) -> str:
        return self._body

    async def query_selector_all(self, _sel: str) -> list:
        return []


async def _capture_search(monkeypatch, *, covered: bool) -> dict:
    from app import evidence_capture

    async def fake_present(_page):
        return covered

    async def fake_dismiss(_page, **_kwargs):
        return []

    monkeypatch.setattr(evidence_capture, "_overlay_present", fake_present)
    monkeypatch.setattr(evidence_capture, "dismiss_amazon_overlays", fake_dismiss)
    collector = EvidenceCollector(None, None, None)

    async def fake_persist(_page, **kwargs):
        record = type("Record", (), {"id": "e1", "data": kwargs["data"]})()
        return type("Captured", (), {"record": record})()

    monkeypatch.setattr(collector, "_persist", fake_persist)
    out = await collector.capture_search_results(
        _SearchPage('1-16 of over 958 results for "x"'),
        session_id="s1", marketplace=AMAZON_MARKETPLACES["us"],
        query="x", screenshot=False,
    )
    return out[0].record.data


@pytest.mark.asyncio
async def test_search_count_is_unclear_while_the_notice_covers_it(monkeypatch) -> None:
    """A total read through the ship-to notice is a guess (958 reported as 95).
    When the notice survives the dismissal the capture says the count is
    UNCLEAR instead of quoting whatever the page printed underneath it."""
    data = await _capture_search(monkeypatch, covered=True)
    assert data["results_count_clear"] is False
    assert "total_results" not in data, "a covered page must not yield a total"
    assert "958" not in str(data), "the covered count leaked into the evidence"
    assert "Dismiss" in data["results_count_note"]


@pytest.mark.asyncio
async def test_search_count_is_trusted_once_the_notice_is_gone(monkeypatch) -> None:
    data = await _capture_search(monkeypatch, covered=False)
    assert data["results_count_clear"] is True
    assert data["total_results"] == 958


def test_collector_requires_real_page_for_kdspy() -> None:
    """_extract_kdspy_panel returns None when no extension frame/panel exists.

    The full method is async and needs a live page; here we verify the
    class wires up and that the guard types exist.
    """
    assert issubclass(EvidenceCaptureError, Exception)
    assert EvidenceCollector is not None
