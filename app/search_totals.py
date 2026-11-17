"""What a search page says about how many results exist.

Two very different numbers live on an Amazon search page, and confusing them
corrupts every competition judgement downstream:

- the **total** Amazon prints — "1-16 of over 30,000 results" — the real
  market-size figure the low-competition analysis rests on;
- how many **rows this capture pulled** — the first page's 16-20.

The evidence used to publish both under ``result_count`` (the row count), so
the model could see a screenshot saying 30,000 results and then, whenever it
regained context from structured evidence, read "16" instead. This module owns
the distinction: it parses the printed total in every Amazon locale and every
thousands-separator style, and its readers understand BOTH the new rows (where
``result_count`` IS the total) and the older ones (where it was the row count).

Pure functions only — no browser, no database — so the parsing is unit-testable
and the same semantics are used by the digest, the market memory, the report
and the continuation brief.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# The word Amazon prints after the total, by storefront language. Matched by
# root so declensions/singular/plural all land ("Ergebnisse"/"Ergebnis",
# "résultats"/"résultat", "resultaten"/"resultaat", …).
_RESULT_WORD_ROOTS: tuple[str, ...] = (
    "result",     # en, es (resultados), fr (résultats), nl (resultaten), pt
    "ergebnis",   # de
    "risultat",   # it
    "wynik",      # pl
    "resultat",   # sv/da/nb
    "результ",    # ru
    "sonuç",      # tr
    "kết quả",    # vi
)

# "over", "about", "more than" … — Amazon marks the total as approximate.
_APPROX_WORDS: tuple[str, ...] = (
    "over", "about", "more than", "approximately", "around",
    "über", "ueber", "mehr als", "etwa", "rund",
    "plus de", "environ", "sur",
    "más de", "mas de", "alrededor de",
    "oltre", "più di", "piu di", "circa",
    "mais de", "cerca de",
    "meer dan", "ongeveer",
    "ponad", "około", "okolo",
    "över", "fler än",
    "более", "около",
    "fazla", "üzeri",
)

# The connector between the visible range and the total ("1-16 OF 30,000").
_CONNECTORS: tuple[str, ...] = (
    "of", "von", "sur", "de", "di", "da", "van", "do", "из", "z", "ze",
    "spośród", "av", "af",
)

# Number token: digits with any of the thousands separators Amazon uses
# (comma, dot, space, NBSP, narrow NBSP, apostrophe).
_NUM = r"\d(?:[\d.,\u00a0\u202f\u2009'’ ]*\d)?"


def _approx_pattern() -> str:
    # Longest first so "more than" is not consumed as "more".
    words = sorted(_APPROX_WORDS, key=len, reverse=True)
    return "(?:" + "|".join(re.escape(w) for w in words) + r")\s+"


_APPROX = _approx_pattern()
_WORD = r"(?P<word>[^\W\d_][\w\u00c0-\u024f]*)"

# "1-16 of over 30,000 results" — the canonical form. The visible range is
# mandatory here (the no-range form is the fallback below), and it is never
# read as the total.
_RANGE_RE = re.compile(
    rf"{_NUM}\s*[-\u2013\u2014]\s*{_NUM}\s+"
    rf"(?:(?:{'|'.join(re.escape(c) for c in _CONNECTORS)})\s+)?"
    rf"(?P<approx>{_APPROX})?{_NUM}\s*{_WORD}",
    re.IGNORECASE,
)

# The total with no range and no connector: "over 7,000,000 results".
_BARE_RE = re.compile(
    rf"(?P<approx>{_APPROX})?(?P<num>{_NUM})\s*{_WORD}",
    re.IGNORECASE,
)

# Two storefronts do not print "of": Amazon.com.tr uses a bare slash
# ("1-16 / 958 sonuç") and Amazon.co.jp puts the total FIRST with a counter
# ("958件中1-16件", "1-16/958 件"). Both are supported marketplaces here, so
# both forms are read exactly like the canonical one.
_COUNTER_WORDS: tuple[str, ...] = ("件", "個", "項", "件中")

_SLASH_TOTAL_RE = re.compile(
    rf"{_NUM}\s*[-\u2013\u2014]\s*{_NUM}\s*/\s*"
    rf"(?P<approx>{_APPROX})?(?P<num>{_NUM})(?:\s*(?P<word>\w+))?",
    re.IGNORECASE,
)

_COUNTER_TOTAL_RE = re.compile(
    rf"(?P<num>{_NUM})\s*(?P<word>[\u4e00-\u9fff]{{1,2}})\s*\u4e2d",
)

# "1-16 of over results" — Amazon printed the marker without the figure in
# some regions. The number is missing, but the fact that it was OVER something
# is not: record that, and never invent a digit.
_MISSING_TOTAL_RE = re.compile(
    rf"{_NUM}\s*[-\u2013\u2014]\s*{_NUM}\s+"
    rf"(?:(?:{'|'.join(re.escape(c) for c in _CONNECTORS)})\s+)?"
    rf"{_APPROX}{_WORD}",
    re.IGNORECASE,
)


def _to_int(token: str) -> int | None:
    """'30,000' / '30.000' / '30 000' / '1.234.567' → int."""
    digits = re.sub(r"\D", "", token or "")
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:  # pragma: no cover — regex guarantees digits
        return None


def _fold(text: str) -> str:
    """Lowercase with diacritics removed: 'résultats' → 'resultats'."""
    import unicodedata

    stripped = "".join(
        ch for ch in unicodedata.normalize("NFKD", text or "")
        if not unicodedata.combining(ch)
    )
    return stripped.lower()


def _is_result_word(word: str) -> bool:
    low = _fold(word)
    return any(low.startswith(root) for root in _FOLDED_ROOTS)


_FOLDED_ROOTS: tuple[str, ...] = tuple(_fold(r) for r in _RESULT_WORD_ROOTS)


def parse_results_total(text: str) -> dict[str, Any]:
    """Read the overall total Amazon printed anywhere in ``text``.

    Returns an empty dict when no numbered total is present: absence is
    recorded as absence, never as a zero ("no results" and "the count is not
    visible" are different facts). Keys returned:

    ``results_text``                the sentence as printed, whitespace-normalized
    ``total_results``               the parsed integer
    ``total_results_are_approximate``  True when "over/about" preceded it
    """
    body = (text or "").replace("\u00a0", " ").replace("\u202f", " ")
    if not body.strip():
        return {}
    # A search page can print several "X-Y of Z results" lines (the overall
    # total, then narrower filtered sections). The OVERALL total is the
    # largest one — never whichever happened to appear first.
    best: dict[str, Any] = {}
    for m in _RANGE_RE.finditer(body):
        if not _is_result_word(m.group("word") or ""):
            continue
        # The last number in the match is the one after the connector; the
        # visible range is deliberately never used as the total.
        numbers = re.findall(_NUM, m.group(0))
        total = _to_int(numbers[-1]) if numbers else None
        if total is None:
            continue
        if not best or total > int(best["total_results"]):
            best = {
                "results_text": " ".join(m.group(0).split()),
                "total_results": total,
                "total_results_are_approximate": bool(m.group("approx")),
            }
    # Slash and counter-first storefronts (tr, jp) print the same fact.
    for pattern, mode in ((_SLASH_TOTAL_RE, "slash"), (_COUNTER_TOTAL_RE, "counter")):
        for m in pattern.finditer(body):
            word = m.groupdict().get("word") or ""
            if mode == "counter":
                if word not in _COUNTER_WORDS:
                    continue
            else:
                # Turkish separates the two words ("958 adet sonuç"), so the
                # result word may sit just past the match.
                tail = body[m.end(): m.end() + 30]
                if not (
                    (word and (_is_result_word(word) or word in _COUNTER_WORDS))
                    or any(root in _fold(tail) for root in _FOLDED_ROOTS)
                ):
                    continue
            total = _to_int(m.group("num") or "")
            if total is None:
                continue
            if not best or total > int(best["total_results"]):
                best = {
                    "results_text": " ".join(m.group(0).split()),
                    "total_results": total,
                    "total_results_are_approximate": bool(m.groupdict().get("approx")),
                }
    if best:
        return best
    # Fallback: no visible range on the page ("over 7,000,000 results").
    for m in _BARE_RE.finditer(body):
        if not _is_result_word(m.group("word") or ""):
            continue
        total = _to_int(m.group("num") or "")
        if total is None:
            continue
        # A bare number is only credible as a TOTAL when it is marked
        # approximate or is clearly too large to be a page of rows.
        if not m.group("approx") and total < 100:
            continue
        if not best or total > int(best["total_results"]):
            best = {
                "results_text": " ".join(m.group(0).split()),
                "total_results": total,
                "total_results_are_approximate": bool(m.group("approx")),
            }
    if best:
        return best
    # No figure anywhere, but the page still says it was OVER something.
    for m in _MISSING_TOTAL_RE.finditer(body):
        if not _is_result_word(m.group("word") or ""):
            continue
        return {
            "results_text": " ".join(m.group(0).split()),
            "total_results_are_approximate": True,
        }
    return best


def search_total(data: Mapping[str, Any] | None) -> int | None:
    """The overall result total stored in one evidence row.

    Reads new rows (``total_results``/``result_count`` both hold the total) and
    legacy rows alike. A legacy ``result_count`` that merely counts the rows in
    ``results`` is NOT a total and returns ``None`` — that is exactly the
    confusion this module exists to end.
    """
    if not isinstance(data, Mapping):
        return None
    total = data.get("total_results")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    count = data.get("result_count")
    rows = data.get("results")
    if isinstance(count, int) and not isinstance(count, bool):
        if isinstance(rows, list) and count == len(rows):
            return None  # legacy: the row count was published as result_count
        return count
    return None


def results_captured(data: Mapping[str, Any] | None) -> int:
    """How many result rows this capture actually pulled (never a total)."""
    if not isinstance(data, Mapping):
        return 0
    captured = data.get("results_captured")
    if isinstance(captured, int) and not isinstance(captured, bool) and captured >= 0:
        return captured
    rows = data.get("results")
    if isinstance(rows, list):
        return len(rows)
    count = data.get("result_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return count
    return 0


def total_label(data: Mapping[str, Any] | None) -> str:
    """Human/model-facing competition figure for one search capture."""
    total = search_total(data)
    if total is None:
        return "total not printed"
    approx = isinstance(data, Mapping) and bool(data.get("total_results_are_approximate"))
    return f"{'over ' if approx else ''}{total:,}"


def competition_text(data: Mapping[str, Any] | None) -> str:
    """The full, unambiguous sentence used in digests and reports."""
    total = search_total(data)
    captured = results_captured(data)
    if total is None:
        return (
            f"Amazon printed no overall total; {captured} result(s) captured "
            "(the captured count is NOT market size)"
        )
    return (
        f"{total_label(data)} total results on Amazon "
        f"({captured} captured in this snapshot)"
    )
