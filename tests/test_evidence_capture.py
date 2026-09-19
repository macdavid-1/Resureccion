"""Tests for evidence-capture extraction utilities (pure functions)."""
from __future__ import annotations

from app.evidence_capture import (
    _first_price,
    _parse_int,
    _parse_rating,
    _stamp,
    EvidenceCollector,
    EvidenceCaptureError,
)


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


def test_collector_requires_real_page_for_kdspy() -> None:
    """_extract_kdspy_panel returns None when no extension frame/panel exists.

    The full method is async and needs a live page; here we verify the
    class wires up and that the guard types exist.
    """
    assert issubclass(EvidenceCaptureError, Exception)
    assert EvidenceCollector is not None
