"""The overall result total, and why it is not the first page's row count.

Amazon prints "1-16 of over 30,000 results"; a capture pulls the 16 rows. Those
are different facts, and publishing the second as ``result_count`` is what made
the model judge a 30,000-result market as "16 results" whenever it regained
context from structured evidence instead of the screenshot.
"""
from __future__ import annotations

import pytest

from app.search_totals import (
    competition_text,
    parse_results_total,
    results_captured,
    search_total,
    total_label,
)


class TestPrintedTotal:
    @pytest.mark.parametrize(
        "text,total,approximate",
        [
            ('Showing 1-16 of over 30,000 results for "grief journal"', 30000, True),
            ("1-16 of 2,345 results", 2345, False),
            ("1–16 of 2.345 results", 2345, False),
            ("1-16 of over 30.000 Ergebnissen", 30000, True),
            ("1-16 sur plus de 30 000 résultats", 30000, True),
            ("1-16 de más de 30.000 resultados", 30000, True),
            ("1-16 di oltre 30.000 risultati", 30000, True),
            ("1-16 van meer dan 30.000 resultaten", 30000, True),
            ("1-16 z ponad 30 000 wyników", 30000, True),
            ("1-16 de mais de 30.000 resultados", 30000, True),
            ("1-24 of over 1,000,000 results for books", 1000000, True),
            ("over 7,000,000 results", 7000000, True),
            # The two storefronts that do not print "of" at all.
            ("1-16 / 958 sonuç", 958, False),
            ("1-16 / 958 adet sonuç", 958, False),
            ("1-16/958 件", 958, False),
            ("958件中1-16件", 958, False),
        ],
    )
    def test_every_storefronts_total_is_read(self, text, total, approximate) -> None:
        out = parse_results_total(text)
        assert out["total_results"] == total
        assert out["total_results_are_approximate"] is approximate

    def test_the_visible_range_never_becomes_the_total(self) -> None:
        out = parse_results_total("1-16 of over 30,000 results")
        assert out["total_results"] == 30000
        assert out["results_text"] == "1-16 of over 30,000 results"

    def test_the_largest_printed_total_wins(self) -> None:
        """Narrower filtered sections print their own, smaller totals."""
        out = parse_results_total(
            "1-16 of 30,000 results in Books; 1-4 of 9 results in Grief & Loss"
        )
        assert out["total_results"] == 30000

    def test_a_unnumbered_marker_never_invents_a_figure(self) -> None:
        out = parse_results_total("1-16 of over results")
        assert "total_results" not in out
        assert out["total_results_are_approximate"] is True

    def test_no_results_line_is_absence_not_zero(self) -> None:
        assert parse_results_total("No results for zzz") == {}
        assert parse_results_total("") == {}


class TestStoredEvidence:
    def test_a_first_page_row_count_is_never_market_size(self) -> None:
        """Legacy rows published the row count under result_count."""
        legacy = {
            "result_count": 16,
            "results": [{"asin": f"B0TEST{i:04d}"} for i in range(16)],
        }
        assert search_total(legacy) is None, "16 rows is not a result total"
        assert results_captured(legacy) == 16
        assert "NOT market size" in competition_text(legacy)

    def test_a_new_capture_publishes_the_printed_total(self) -> None:
        row = {
            "result_count": 30000,
            "total_results": 30000,
            "total_results_are_approximate": True,
            "results_captured": 16,
            "results": [{"asin": f"B0TEST{i:04d}"} for i in range(16)],
        }
        assert search_total(row) == 30000
        assert results_captured(row) == 16
        assert total_label(row) == "over 30,000"
        text = competition_text(row)
        assert "30,000 total results" in text
        assert "16 captured" in text

    def test_correcting_the_total_keeps_the_captured_count(self) -> None:
        row = {"total_results": 2500, "results_captured": 16}
        assert search_total(row) == 2500
        assert results_captured(row) == 16

    def test_absence_stays_absence(self) -> None:
        row = {"result_count": None, "results": [{"asin": "B0TEST0001"}]}
        assert search_total(row) is None
        assert results_captured(row) == 1
        assert total_label(row) == "total not printed"
