"""Tests for evidence-capture extraction utilities (pure functions)."""
from __future__ import annotations

from typing import Any

import pytest

from app.evidence_capture import (
    EvidenceCaptureError,
    EvidenceCollector,
    _extract_kdspy_panel,
    _first_price,
    _parse_int,
    _parse_rating,
    _stamp,
    parse_format_prices,
    parse_product_details,
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


def test_product_details_from_amazons_bullet_list() -> None:
    """Publication date, page count and EVERY BSR the page prints.

    The report reasons about market-entry recency and rank bands, so these are
    captured as data instead of being left in page prose the report never sees.
    """
    details = parse_product_details(
        "Publisher\n\u200b\nIndependently published (March 3, 2023)\n\u200b\n"
        "Language\n\u200b\nEnglish\n\u200b\nPaperback\n\u200b\n210 pages\n\u200b\n"
        "ISBN-13\n\u200b\n979-8387456336\n\u200b\nBest Sellers Rank\n\u200b\n"
        "#12,345 in Books (See Top 100 in Books)\n#45 in Grief & Loss\n"
        "#112 in Self-Help\n"
    )
    assert details["publication_date"] == "March 3, 2023"
    assert details["publisher"].startswith("Independently published")
    assert details["print_length"] == "210 pages"
    assert details["language"] == "English"
    assert details["isbn"] == "979-8387456336"
    assert details["best_seller_rank"] == 12345
    assert details["best_seller_category"] == "Books"
    assert details["best_seller_ranks"] == [
        {"rank": 12345, "category": "Books"},
        {"rank": 45, "category": "Grief & Loss"},
        {"rank": 112, "category": "Self-Help"},
    ]


def test_product_details_from_amazons_table_rows() -> None:
    details = parse_product_details(
        "Publisher: Independently published (March 3, 2023)\n"
        "Publication date: March 3, 2023\n"
        "Language: English\n"
        "Paperback: 210 pages\n"
        "Best Sellers Rank: #9,001 in Books (See Top 100 in Books) #21 in Death & Grief\n"
    )
    assert details["publication_date"] == "March 3, 2023"
    assert details["print_length"] == "210 pages"
    assert details["best_seller_ranks"] == [
        {"rank": 9001, "category": "Books"},
        {"rank": 21, "category": "Death & Grief"},
    ]


def test_product_details_never_invent_missing_fields() -> None:
    assert parse_product_details("") == {}
    assert parse_product_details("Publisher: nothing here") == {
        "publisher": "nothing here"
    }


def test_format_prices_are_captured_per_format() -> None:
    assert parse_format_prices(
        "Kindle\n$0.99\nPaperback\n$9.99\nHardcover\n$19.99\nAudible Audiobook\n$0.00"
    ) == [
        {"format": "Kindle", "price": "$0.99"},
        {"format": "Paperback", "price": "$9.99"},
        {"format": "Hardcover", "price": "$19.99"},
        {"format": "Audible Audiobook", "price": "$0.00"},
    ]
    assert parse_format_prices("no formats rendered") == []


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


async def _capture_search(
    monkeypatch, *, covered: bool, body: str = '1-16 of over 958 results for "x"',
) -> dict:
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
        _SearchPage(body),
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
    assert data["result_count"] is None, "a covered page must not yield a total"
    assert "total_results" not in data, "a covered page must not yield a total"
    assert "958" not in str(data), "the covered count leaked into the evidence"
    assert "Dismiss" in data["results_count_note"]


@pytest.mark.asyncio
async def test_search_count_is_trusted_once_the_notice_is_gone(monkeypatch) -> None:
    data = await _capture_search(monkeypatch, covered=False)
    assert data["results_count_clear"] is True
    assert data["total_results"] == 958


@pytest.mark.asyncio
async def test_search_evidence_publishes_the_overall_total(monkeypatch) -> None:
    """'result_count' IS Amazon's overall total; the rows pulled are separate.

    This is the regression that made the model read 16 for a 30,000-result
    market whenever it regained context from the evidence instead of the
    screenshot."""
    data = await _capture_search(
        monkeypatch, covered=False, body='Showing 1-16 of over 30,000 results for "x"',
    )
    assert data["result_count"] == 30000
    assert data["total_results"] == 30000
    assert data["results_captured"] == 0
    assert data["total_results_are_approximate"] is True


@pytest.mark.asyncio
async def test_search_evidence_without_a_printed_total_never_guesses(monkeypatch) -> None:
    data = await _capture_search(monkeypatch, covered=False, body='Showing 1-16 of results for "x"')
    assert data["result_count"] is None
    assert "total_results" not in data
    assert "NOT market size" in data["results_count_note"]


# ------------------------------------------------------- review research
class _El:
    def __init__(self, text: str = "", attrs: dict[str, str] | None = None) -> None:
        self._text = text
        self._attrs = attrs or {}
        self.clicks = 0

    async def inner_text(self) -> str:
        return self._text

    async def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)

    async def click(self, **_kwargs) -> None:
        self.clicks += 1


class _Review:
    """A review container: each selector returns its own piece of text."""

    def __init__(
        self, *, rating: str, title: str, date: str, body: str,
        verified: bool = True, helpful: str = "", author: str = "A reader",
    ) -> None:
        self._parts = {
            "review-star-rating": _El(rating),
            "review-title": _El(title),
            "review-date": _El(date),
            "review-body": _El(body),
            "helpful-vote-statement": _El(helpful),
            "a-profile-name": _El(author),
        }
        self._verified = _El("Verified Purchase") if verified else None

    async def query_selector(self, sel: str) -> Any:
        if "avp-badge" in sel:
            return self._verified
        for key, el in self._parts.items():
            if key in sel:
                return el
        return None

    async def inner_text(self) -> str:
        return " ".join(e._text for e in self._parts.values())


class _ReviewPage:
    """A page whose review section renders only after it is scrolled to.

    Models the behaviour that made review capture fail in production: the
    containers do not exist until the page has been scrolled, and a
    collapsed body only exposes its opening line until it is expanded.
    """

    url = "https://www.amazon.com/dp/B0TEST1234"

    def __init__(self, reviews: list[_Review], *, histogram: list[str] | None = None) -> None:
        self.reviews = reviews
        self.histogram = histogram or []
        self.scrolled = 0
        self.mounted = False
        self.expand_links = [_El("Read more"), _El("Read more")]

    async def evaluate(self, script: str, *args: Any) -> Any:
        if "scrollBy" in script:
            self.scrolled += 1
            self.mounted = True
            return None
        if "querySelector" in script:
            # The review section exists only once the page has moved.
            return self.mounted
        return None

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def query_selector(self, sel: str) -> Any:
        if "rating-out-of-text" in sel or "acrPopover" in sel:
            return _El("4.6 out of 5 stars")
        if "total-review-count" in sel or "acrCustomerReviewText" in sel:
            return _El("1,204 global ratings")
        return None

    async def query_selector_all(self, sel: str) -> list:
        if "histogram" in sel or "aria-label" in sel:
            return [_El(text) for text in self.histogram]
        if "review-body" in sel and "review" not in sel.replace("review-body", ""):
            return self.expand_links
        if sel.startswith("#customerReviews") or "data-hook='review'" in sel:
            return self.reviews if self.mounted else []
        return []

    async def inner_text(self, _sel: str) -> str:
        return ""


async def _no_summary(_sel: str) -> Any:
    return None


async def _collect(page: Any, monkeypatch, *, max_reviews: int = 40) -> dict:
    from app import evidence_capture

    captured: dict = {}
    collector = EvidenceCollector(None, None, None)

    async def fake_persist(_page, **kwargs):
        captured.update(kwargs["data"])
        record = type("Record", (), {"id": "e1", "data": kwargs["data"]})()
        return type("Captured", (), {"record": record})()

    monkeypatch.setattr(collector, "_persist", fake_persist)
    await collector.capture_reviews(
        page, session_id="s1", marketplace=AMAZON_MARKETPLACES["us"],
        asin="B0TEST1234", max_reviews=max_reviews, screenshot=False,
    )
    assert evidence_capture is not None
    return captured


@pytest.mark.asyncio
async def test_reviews_are_read_after_scrolling_to_the_review_section(monkeypatch) -> None:
    """The regression behind "it never captures any review content": reading
    the DOM before scrolling found nothing, because the review list mounts
    only once the page reaches it."""
    page = _ReviewPage([
        _Review(rating="5.0 out of 5 stars", title="Changed my life",
                date="Reviewed in the United States on March 2, 2026",
                body="I lost my mother last spring and this guided me through it."),
        _Review(rating="2.0 out of 5 stars", title="Too repetitive",
                date="Reviewed in the United Kingdom on January 9, 2026",
                body="The prompts repeat after the first thirty pages."),
    ])
    data = await _collect(page, monkeypatch)
    assert data["reviews_captured"] == 2
    assert page.scrolled > 0, "the collector must scroll to the reviews"
    assert page.expand_links[0].clicks == 1, "collapsed bodies must be expanded"
    assert data["reviews"][0]["title"] == "Changed my life"
    assert data["reviews"][1]["text"].startswith("The prompts repeat")
    assert data["critical_reviews"] == 1
    assert data["positive_reviews"] == 1
    assert data["verified_reviews"] == 2
    # Amazon's own printed average wins over anything derived from the two
    # reviews read; the derived figure is only a fallback and says so.
    assert data["average_rating"] == 4.6
    assert "average_rating_basis" not in data
    assert data["ratings_total"] == 1204
    assert data["review_text_chars"] > 40


@pytest.mark.asyncio
async def test_an_unprinted_average_is_derived_and_labelled(monkeypatch) -> None:
    page = _ReviewPage([
        _Review(rating="5.0 out of 5 stars", title="A", date="2026", body="Loved it."),
        _Review(rating="2.0 out of 5 stars", title="B", date="2026", body="Not for me."),
    ])
    page.query_selector = _no_summary  # type: ignore[method-assign]
    data = await _collect(page, monkeypatch)
    assert data["average_rating"] == 3.5
    assert data["average_rating_basis"] == "computed from the reviews captured on this page"


@pytest.mark.asyncio
async def test_reviews_carry_the_printed_distribution(monkeypatch) -> None:
    page = _ReviewPage(
        [_Review(rating="4.0 out of 5 stars", title="Good", date="2026", body="Solid.")],
        histogram=[
            "5 stars 62% of ratings", "4 stars 21% of ratings",
            "3 stars 9% of ratings", "2 stars 4% of ratings",
            "1 star 4% of ratings",
        ],
    )
    data = await _collect(page, monkeypatch)
    assert data["rating_distribution"] == {"5": 62, "4": 21, "3": 9, "2": 4, "1": 4}
    assert data["rating_distribution_unit"] == "percent of all ratings"


@pytest.mark.asyncio
async def test_a_page_without_reviews_says_why_instead_of_claiming_zero(monkeypatch) -> None:
    page = _ReviewPage([])
    data = await _collect(page, monkeypatch)
    assert data["reviews_captured"] == 0
    assert "no review container matched" in data["blocked_reason"]
    assert "review_region_text" not in data


def test_collector_requires_real_page_for_kdspy() -> None:
    """_extract_kdspy_panel returns None when no extension frame/panel exists.

    The full method is async and needs a live page; here we verify the
    class wires up and that the guard types exist.
    """
    assert issubclass(EvidenceCaptureError, Exception)
    assert EvidenceCollector is not None
