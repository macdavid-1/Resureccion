"""Tests for the marketplace abstraction."""
from __future__ import annotations

import pytest

from app.marketplace import (
    AMAZON_MARKETPLACES,
    MarketplaceError,
    get_marketplace,
    marketplace_from_url,
    resolve_explicit,
    resolve_plan,
    select_auto,
)


def test_catalog_has_required_marketplaces() -> None:
    for code in ("us", "uk", "ca", "au", "de", "fr", "it", "es", "jp", "in"):
        assert code in AMAZON_MARKETPLACES


def test_lookup_by_code_and_domain() -> None:
    assert get_marketplace("us").domain == "amazon.com"
    assert get_marketplace("amazon.co.uk").code == "uk"
    assert get_marketplace("Amazon.DE").code == "de"


def test_lookup_unknown_raises() -> None:
    with pytest.raises(MarketplaceError):
        get_marketplace("mars.amazon")
    with pytest.raises(MarketplaceError):
        get_marketplace("")


def test_explicit_resolution_obeyed_exactly() -> None:
    resolved = resolve_explicit(["de", "amazon.co.jp", "DE"])
    assert [m.code for m in resolved] == ["de", "jp"]  # deduped, order kept
    with pytest.raises(MarketplaceError):
        resolve_explicit(["nowhere"])


def test_auto_selection_core_spread() -> None:
    picked = select_auto("")
    codes = [m.code for m in picked]
    assert 4 <= len(codes) <= 5
    assert "us" in codes and "uk" in codes


def test_auto_selection_language_hints() -> None:
    picked = select_auto("japanese bullet journals and anime coloring books")
    codes = [m.code for m in picked]
    assert "jp" in codes
    picked2 = select_auto("deutsche bücher für hundebesitzer", "germany market")
    assert "de" in [m.code for m in picked2]


def test_resolve_plan_explicit_vs_auto() -> None:
    mode, m_list = resolve_plan(["us"])
    assert mode == "explicit" and len(m_list) == 1
    mode2, m_list2 = resolve_plan(None)
    assert mode2 == "auto" and 4 <= len(m_list2) <= 5
    mode3, _ = resolve_plan([])
    assert mode3 == "auto"


def test_marketplace_from_url() -> None:
    m = marketplace_from_url("https://www.amazon.de/dp/B0ABC12345?ref=something")
    assert m is not None and m.code == "de"
    assert marketplace_from_url("https://example.com") is None


def test_auto_selection_respects_max_count() -> None:
    """Regression: max_count clamps to [4,5] and is honored exactly."""
    assert len(select_auto("", max_count=4)) == 4
    assert len(select_auto("", max_count=5)) == 5
    assert len(select_auto("", max_count=2)) == 4  # floor of 4
    assert len(select_auto("", max_count=20)) == 5  # ceiling of 5
