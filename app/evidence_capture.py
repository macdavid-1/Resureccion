"""Evidence-capture abstraction.

The browser layer must collect only information *relevant to research* while
preserving enough context for verification — never blindly saving whole pages.

`EvidenceCollector` extracts structured, redacted records from live pages:
- search results: rank, ASIN, title, price, rating, review count,
- product pages: ASIN, title, price, rating, review count, category path, BSR,
- generic page evidence: title/url/timestamp/marketplace,
- KDSpy-derived data: when the extension is present, its rendered panel data
  is read from the page DOM (the extension itself computes values) — we only
  read what the extension chose to render; nothing is fabricated.

Every capture stores: url (scrubbed), title, marketplace, kind, structured
data, timestamp, and optionally a screenshot artifact. All outgoing data
passes through `redact()` (see app/redact.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.artifacts import ArtifactStore
from app.browser_manager import (
    BrowserManager,
    BrowserManagerError,
    _overlay_present,
    dismiss_amazon_overlays,
)
from app.browser_store import BrowserEvidence, BrowserEvidenceStore
from app.marketplace import Marketplace, marketplace_from_url
from app.redact import redact, scrub_url
from app.search_totals import parse_results_total

ASIN_RE = re.compile(r"\b(B0[0-9A-Z]{8}|[0-9]{9}(?:X|[0-9]))\b")
PRICE_RE = re.compile(r"(?:US\s?)?[$€£¥₹]\s?\d[\d,]*(?:\.\d{2})?")


class EvidenceCaptureError(Exception):
    pass


@dataclass
class CapturedEvidence:
    record: BrowserEvidence
    screenshot_artifact_id: str | None

    def to_dict(self) -> dict[str, Any]:
        d = self.record.to_dict()
        d["has_screenshot"] = self.screenshot_artifact_id is not None
        return d


class EvidenceCollector:
    def __init__(
        self,
        browser: BrowserManager,
        evidence_store: BrowserEvidenceStore,
        artifacts: ArtifactStore,
        *,
        live_view: Any = None,
        live_view_session_provider: Any = None,
    ) -> None:
        self.browser = browser
        self.evidence_store = evidence_store
        self.artifacts = artifacts
        # Most recent PNG bytes captured from the browser — used by the runner
        # to attach visual ground truth to model calls (vision models).
        self.last_screenshot: bytes | None = None
        # Optional live-view hook: (store, session_provider). When wired, every
        # screenshot also refreshes the bounded live-view ring so the owner can
        # watch research from their phone. Purely additive; never raises.
        self._live_view = live_view
        self._live_session = live_view_session_provider

    # ------------------------------------------------------------ extraction
    async def capture_search_results(
        self,
        page: Any,
        *,
        session_id: str,
        marketplace: Marketplace,
        query: str,
        max_items: int = 20,
        screenshot: bool = True,
    ) -> list[CapturedEvidence]:
        """Extract organic results (rank/ASIN/title/price/rating/reviews).

        Two different numbers live here and they mean different things:

        - ``result_count``  the TOTAL Amazon itself printed on the page
          ("1-16 of over 30,000 results for …") — the real competition
          figure, in every storefront language and separator style. It is
          ``None`` when no printed total could be read: a first page's row
          count is NEVER published under this key (that conflation made the
          model report 16 results for a 30,000-result market).
        - ``results_captured``  how many result rows this capture pulled
          (capped by ``max_items``) — a snapshot size, never market size.

        ``total_results``/``results_text`` mirror the total for the report
        and for older readers; the shipping-country overlay is dismissed
        first because it covers the printed total.
        """
        # The overlay pops AFTER load and covers the printed total; dismiss
        # it right before reading so `total_results` is captured even when
        # the navigation-time dismissal ran too early.
        try:
            await dismiss_amazon_overlays(page)
        except Exception:
            pass
        # The printed total is only a MEASUREMENT when nothing covers it. When
        # the notice survives the dismissal, the count is recorded as UNCLEAR
        # instead: reading a total through an overlay is how a page with 958
        # results got reported as 95, and a wrong competition figure is worse
        # than a missing one.
        covered = await _overlay_present(page)
        items = await _extract_search_items(page, max_items)
        data: dict[str, Any] = {
            "query": query,
            "result_count": None,          # Amazon's overall total; None = unknown
            "results_captured": len(items),  # rows in THIS snapshot only
            "results": items,
            "results_count_clear": not covered,
        }
        if covered:
            data["results_count_note"] = (
                "the shipping-country notice was still covering the page when "
                "this search was read; any printed total is unverified — "
                "dismiss it (click \"Dismiss\") and capture again"
            )
        else:
            summary = await _extract_results_summary(page)
            data.update(summary)
            total = summary.get("total_results")
            if isinstance(total, int):
                data["result_count"] = total
            else:
                data["results_count_note"] = (
                    "Amazon printed no overall result total on this page "
                    f"({len(items)} rows were captured). The captured row count "
                    "is NOT market size: never quote it as competition. Scroll "
                    "the results (or open page 2) and capture again to read the "
                    "printed total."
                )
        return [await self._persist(
            page,
            session_id=session_id,
            marketplace=marketplace,
            kind="search_results",
            data=data,
            screenshot=screenshot,
        )]

    async def capture_product_page(
        self,
        page: Any,
        *,
        session_id: str,
        marketplace: Marketplace,
        screenshot: bool = True,
    ) -> CapturedEvidence:
        """Extract product metadata from a product page."""
        data = await _extract_product(page)
        return await self._persist(
            page,
            session_id=session_id,
            marketplace=marketplace,
            kind="product_page",
            data=data,
            screenshot=screenshot,
        )

    async def capture_kdspy_panel(
        self,
        page: Any,
        *,
        session_id: str,
        marketplace: Marketplace,
        screenshot: bool = True,
    ) -> CapturedEvidence:
        """Read whatever the KDSpy extension rendered on/near the page.

        We query the extension's injected panel DOM. If the extension isn't
        loaded or hasn't rendered anything, this raises — the research flow
        treats KDSpy absence as a real condition, never fabricates data.
        """
        panel = await _extract_kdspy_panel(page)
        if panel is None:
            raise EvidenceCaptureError(
                "KDSpy panel not present on page — extension may be missing, "
                "unauthenticated, or not yet rendered"
            )
        return await self._persist(
            page,
            session_id=session_id,
            marketplace=marketplace,
            kind="kdspy_panel",
            data=panel,
            screenshot=screenshot,
        )

    async def capture_page_state(
        self,
        page: Any,
        *,
        session_id: str,
        note: str = "",
    ) -> CapturedEvidence:
        """Minimal context capture of the current page (no scraping)."""
        url = scrub_url(page.url or "")
        (await _safe_title(page)) or ""
        marketplace = marketplace_from_url(url)
        return await self._persist(
            page,
            session_id=session_id,
            marketplace=marketplace,
            kind="page_state",
            data={"note": note},
            screenshot=True,
        )

    # -------------------------------------------------------------- plumbing
    async def _persist(
        self,
        page: Any,
        *,
        session_id: str,
        marketplace: Marketplace | None,
        kind: str,
        data: dict[str, Any],
        screenshot: bool,
    ) -> CapturedEvidence:
        url = scrub_url(page.url or "")
        title = (await _safe_title(page)) or ""
        mkt = marketplace or marketplace_from_url(url)
        screenshot_artifact_id: str | None = None
        if screenshot:
            try:
                png = await self.browser.screenshot(page)
                self.last_screenshot = png
                self._maybe_feed_live_view(page)
                artifact = self.artifacts.save_bytes(
                    session_id,
                    kind="screenshot",
                    filename=f"evidence-{kind}-{_stamp()}.png",
                    data=png,
                    meta={"evidence_kind": kind, "marketplace": mkt.code if mkt else ""},
                )
                screenshot_artifact_id = artifact.id
            except BrowserManagerError:
                screenshot_artifact_id = None  # evidence still persists without image
        record = self.evidence_store.create(
            session_id=session_id,
            marketplace=mkt.code if mkt else "",
            kind=kind,
            url=url,
            title=title[:300],
            data=redact(data),
            screenshot_artifact_id=screenshot_artifact_id,
        )
        return CapturedEvidence(record=record, screenshot_artifact_id=screenshot_artifact_id)

    # ------------------------------------------------------------ live view
    def _maybe_feed_live_view(self, page: Any) -> None:
        """Push the newest screenshot into the bounded live-view ring.
        Optional by design: unwired or failing, research is unaffected."""
        if self._live_view is None or self.last_screenshot is None:
            return
        try:
            from app.live_view import downscale_jpeg

            jpeg = downscale_jpeg(self.last_screenshot)
            if jpeg is None:
                return
            sid = ""
            try:
                sid = str(self._live_session() or "")
            except Exception:
                sid = ""
            if not sid:
                return
            self._live_view.put(
                sid, jpeg,
                url=str(getattr(page, "url", "") or "")[:500],
                title=str(getattr(page, "_lv_title", "") or "")[:200],
            )
        except Exception:
            pass


# ------------------------------------------------------------- DOM extraction
# Amazon's own "1-16 of over 30,000 results for <query>" line — the competition
# measurement the whole low-competition analysis rests on. It is read from the
# results info bar first (that is where Amazon prints it) and from the page
# text only as a fallback, then parsed by app.search_totals (every storefront
# language, every thousands-separator style).
_RESULTS_INFO_SELECTORS: tuple[str, ...] = (
    "[data-component-type='s-result-info-bar']",
    "#s-result-info-bar",
    "div.s-desktop-toolbar",
    "[data-csa-c-content-id='aps/result-header']",
)


async def _extract_results_summary(page: Any) -> dict[str, Any]:
    """Read Amazon's printed total-result line, if the page shows one.

    Returns an empty dict when no line is found — absence is recorded as
    absence, never as a zero ("no results" and "count not visible" are
    different facts).
    """
    summary: dict[str, Any] = {}
    for selector in _RESULTS_INFO_SELECTORS:
        try:
            el = await page.query_selector(selector)
        except Exception:
            el = None
        if el is None:
            continue
        try:
            text = await el.inner_text()
        except Exception:
            continue
        summary = parse_results_total(text or "")
        if summary:
            return summary
    try:
        text = await page.inner_text("body")
    except Exception:
        return summary
    return parse_results_total(text or "") or summary


async def _extract_search_items(page: Any, max_items: int) -> list[dict[str, Any]]:
    """Pull structured data from Amazon search result DOM (best-effort)."""
    items: list[dict[str, Any]] = []
    try:
        cards = await page.query_selector_all("div[data-component-type='s-search-result']")
    except Exception:
        cards = []
    rank = 0
    for card in cards[: max_items * 2]:
        if len(items) >= max_items:
            break
        asin = (await _attr(card, "data-asin") or "").strip()
        if not asin or not ASIN_RE.match(asin):
            continue
        rank += 1
        title = (await _text_of_inner(card, "h2 span, h2")) or ""
        price = (await _text_of_inner(card, ".a-price .a-offscreen")) or ""
        rating = (await _text_of_inner(card, ".a-icon-alt")) or ""
        reviews = (await _text_of_inner(card, "span[aria-label*='stars'] ~ span a, .s-link-style .s-underline-text")) or ""
        items.append(
            {
                "rank": rank,
                "asin": asin,
                "title": title.strip()[:250],
                "price": _first_price(price),
                "rating": _parse_rating(rating),
                "review_count": _parse_int(reviews),
            }
        )
    return items


async def _extract_product(page: Any) -> dict[str, Any]:
    url = scrub_url(page.url or "")
    asin_m = ASIN_RE.search(url)
    data: dict[str, Any] = {
        "asin": asin_m.group(1) if asin_m else None,
        "title": (await _text_of(page, "#productTitle")) or "",
        "price": _first_price((await _text_of(page, ".a-price .a-offscreen, #corePrice_feature_div .a-offscreen")) or ""),
        "rating": _parse_rating((await _text_of(page, "#acrPopover .a-icon-alt, span[data-hook='rating-out-of-text']")) or ""),
        "review_count": _parse_int((await _text_of(page, "#acrCustomerReviewText")) or ""),
        "best_seller_rank": None,
        "category_path": [],
    }
    # Publication data, formats and every BSR the page prints. The report
    # reasons about market entry recency and format/price positioning, so these
    # observations are captured as data instead of being left in prose the
    # report stage can never see.
    detail_text = (
        await _text_of(
            page,
            "#detailBullets_feature_div, #productDetails_detailBullets_sections1, "
            "#productDetails_feature_div, #SalesRank",
        )
    ) or ""
    data.update(parse_product_details(detail_text))
    data["formats"] = parse_format_prices(
        (await _text_of(page, "#tmmSwatches, #formats, #mediaTabs_tabSet")) or ""
    )
    # Reviews already rendered on the page: the strongest evidence of what
    # readers like and dislike about the incumbents.
    try:
        data["reviews"] = await _extract_review_snippets(page, max_items=4)
    except Exception:
        data["reviews"] = []
    # BSR: look in product details sections.
    bsr_text = detail_text
    m = re.search(r"#?\s?([\d,]+)\s+in\s+([^\n(]+)", bsr_text)
    if m and data.get("best_seller_rank") is None:
        data["best_seller_rank"] = _parse_int(m.group(1))
        data["best_seller_category"] = m.group(2).strip()[:120]
    # Category breadcrumbs.
    crumbs = []
    try:
        nodes = await page.query_selector_all("#wayfinding-breadcrumbs_feature_div li a")
        for n in nodes[:8]:
            t = ((await n.inner_text()) or "").strip()
            if t:
                crumbs.append(t[:80])
    except Exception:
        pass
    data["category_path"] = crumbs
    return {k: v for k, v in data.items() if v not in (None, "", [])}


# --------------------------------------------------- product detail parsing
# Amazon renders product details two ways: alternated label/value bullets and
# a table of rows. Both reduce to "label: value" lines, so one pure parser
# covers them (and is unit-testable without a browser).
_DETAIL_LABELS = (
    "publisher", "publication date", "language", "print length", "pages",
    "paperback", "hardcover", "board book", "mass market paperback",
    "spiral-bound", "loose leaf", "pamphlet", "isbn-10", "isbn-13",
    "item weight", "dimensions", "best sellers rank", "reading age",
    "lexile measure", "audio", "edition",
)
_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)
_DATE_IN_PARENS_RE = re.compile(
    r"\(\s*((?:" + "|".join(_MONTHS) + r")\s+\d{1,2},\s+\d{4})\s*\)"
)
_DATE_RE = re.compile(
    r"((?:" + "|".join(_MONTHS) + r")\s+\d{1,2},\s+\d{4})"
)
_RANK_RE = re.compile(r"#\s?([\d,]+)\s+in\s+([^#\n]+)")
_FORMAT_WORDS = (
    "Kindle Edition", "Kindle", "Paperback", "Hardcover", "Audible Audiobook",
    "Audiobook", "Board book", "Mass Market Paperback", "Spiral-bound",
    "Loose Leaf", "Pamphlet",
)


def _clean_detail_lines(text: str) -> list[str]:
    out: list[str] = []
    for raw in str(text or "").replace("\u200b", "\n").replace("\u00a0", " ").splitlines():
        line = " ".join(raw.split()).strip()
        if line:
            out.append(line)
    return out


def parse_product_details(text: str) -> dict[str, Any]:
    """Publication date, format/page count, publisher and every BSR printed.

    Pure function: the same logic runs on Amazon's bullet list and on its
    detail table. Absent labels stay absent — nothing is invented.
    """
    lines = _clean_detail_lines(text)
    values: dict[str, str] = {}
    for i, line in enumerate(lines):
        low = line.lower().rstrip(":")
        label = None
        inline = ""
        if ":" in line:
            head, _, tail = line.partition(":")
            if head.strip().lower() in _DETAIL_LABELS:
                label, inline = head.strip().lower(), tail.strip()
        if label is None and low in _DETAIL_LABELS:
            label = low
            if i + 1 < len(lines) and lines[i + 1].lower().rstrip(":") not in _DETAIL_LABELS:
                inline = lines[i + 1]
        if label and inline and label not in values:
            if label == "best sellers rank":
                # Amazon prints the primary rank and then one line per
                # category; all of them are real observations.
                extra: list[str] = []
                j = i + 1
                while j < len(lines) and lines[j].lstrip().startswith("#"):
                    extra.append(lines[j])
                    j += 1
                inline = " ".join([inline, *extra])
            values[label] = inline

    out: dict[str, Any] = {}
    publisher = values.get("publisher", "")
    if publisher:
        out["publisher"] = publisher[:160]
    published = values.get("publication date", "")
    if not published and publisher:
        m = _DATE_IN_PARENS_RE.search(publisher) or _DATE_RE.search(publisher)
        published = m.group(1) if m else ""
    if published:
        out["publication_date"] = published[:40]
    if values.get("language"):
        out["language"] = values["language"][:60]
    for key in ("print length", "paperback", "hardcover", "board book",
                "mass market paperback", "spiral-bound", "loose leaf", "pamphlet"):
        if values.get(key):
            out["print_length"] = values[key][:60]
            break
    for key in ("isbn-13", "isbn-10"):
        if values.get(key):
            out["isbn"] = values[key][:40]
            break
    if values.get("edition"):
        out["edition"] = values["edition"][:80]

    rank_text = values.get("best sellers rank", "")
    ranks: list[dict[str, Any]] = []
    for m in _RANK_RE.finditer(rank_text):
        rank = _parse_int(m.group(1))
        category = " ".join(m.group(2).split())[:120]
        # "(See Top 100 in Books)" is navigation, not part of the category.
        category = re.sub(
            r"\(.*?See Top 100.*?\)", "", category, flags=re.IGNORECASE
        ).strip(" -·")
        if rank is not None and category and {
            "rank": rank, "category": category
        } not in ranks:
            ranks.append({"rank": rank, "category": category})
    if ranks:
        out["best_seller_ranks"] = ranks[:6]
        out.setdefault("best_seller_rank", ranks[0]["rank"])
        out.setdefault("best_seller_category", ranks[0]["category"])
    return out


def parse_format_prices(text: str) -> list[dict[str, str]]:
    """Every format Amazon offers with its price, from the format swatches."""
    lines = _clean_detail_lines(text)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        fmt = next((f for f in _FORMAT_WORDS if line.lower() == f.lower()), None)
        if fmt is None:
            continue
        if fmt in seen:
            continue
        price = ""
        for nxt in lines[i + 1 : i + 4]:
            if next((f for f in _FORMAT_WORDS if nxt.lower() == f.lower()), None):
                break
            price = _first_price(nxt) or price
            if price:
                break
        seen.add(fmt)
        out.append({"format": fmt, "price": price})
    return out[:8]


async def _extract_review_snippets(page: Any, max_items: int = 4) -> list[dict[str, Any]]:
    """Read the review text Amazon already rendered on a product page.

    Used by the finalist deep dive: the same product pages it opens to read
    category breadcrumbs also carry real customer reviews, so the report's
    "how to beat the incumbents" section is grounded in what buyers actually
    wrote instead of a separate, expensive navigation pass. Best-effort by
    design — no reviews on the page simply yields an empty list.
    """
    out: list[dict[str, Any]] = []
    try:
        blocks = await page.query_selector_all("div[data-hook='review']")
    except Exception:
        blocks = []
    asin_m = ASIN_RE.search(scrub_url(page.url or ""))
    asin = asin_m.group(1) if asin_m else ""
    for block in blocks[: max(1, int(max_items)) * 2]:
        if len(out) >= max_items:
            break
        body = " ".join(
            (
                await _text_of_inner(
                    block, "[data-hook='review-body'] span, [data-hook='review-body']"
                )
            ).split()
        )
        if not body:
            continue
        title = " ".join(
            (
                await _text_of_inner(
                    block, "[data-hook='review-title'] span, [data-hook='review-title']"
                )
            ).split()
        )
        rating_txt = await _text_of_inner(
            block, "i[data-hook='review-star-rating'] span, [data-hook='review-star-rating']"
        )
        out.append(
            {
                "asin": asin,
                "rating": _parse_rating(rating_txt),
                "title": title[:160],
                "body": body[:600],
            }
        )
    return out


async def _extract_kdspy_panel(page: Any) -> dict[str, Any] | None:
    """Read the KDSpy panel the extension rendered, if present.

    KDSpy injects UI into Amazon pages. Selectors are intentionally
    defensive: we look for common extension iframe/panel patterns and only
    return data actually rendered. Returns None when nothing is found.
    """
    # 1. Extension iframes. The engine is Firefox, so live frames use the
    #    moz-extension:// scheme — the Chrome-era chrome-extension:// check
    #    alone never matched and the agent was blind to rendered panels.
    frames = []
    try:
        frames = page.frames
    except Exception:
        frames = []
    for frame in frames:
        url = getattr(frame, "url", "") or ""
        scheme = next((s for s in ("moz-extension://", "chrome-extension://") if s in url), None)
        if scheme is None:
            continue
        body = ""
        try:
            body = (await frame.inner_text("body"))[:8000]
        except Exception:
            body = ""
        if body.strip():
            return {
                "source": "extension_frame",
                "frame_url_host": url.split(scheme)[-1].split("/")[0],
                "text_excerpt": body[:2000],
            }
    # 2. Injected shadow/host panels with kdspy-ish hints.
    for sel in ("[class*='kdspy' i]", "[id*='kdspy' i]", "[data-kdspy]"):
        try:
            el = await page.query_selector(sel)
        except Exception:
            el = None
        if el is not None:
            text = ""
            try:
                text = (await el.inner_text())[:4000]
            except Exception:
                pass
            return {"source": "injected_panel", "selector": sel, "text_excerpt": text[:2000]}
    return None


# ------------------------------------------------------------------ utilities
async def _safe_title(page: Any) -> str:
    try:
        return await page.title()
    except Exception:
        return ""


async def _text_of(page: Any, selector: str) -> str:
    try:
        el = await page.query_selector(selector)
        if el is None:
            return ""
        return (await el.inner_text()) or ""
    except Exception:
        return ""


async def _text_of_inner(card: Any, selector: str) -> str:
    try:
        el = await card.query_selector(selector)
        if el is None:
            return ""
        return (await el.inner_text()) or ""
    except Exception:
        return ""


async def _attr(el: Any, name: str) -> str | None:
    try:
        return await el.get_attribute(name)
    except Exception:
        return None


def _first_price(text: str) -> str | None:
    m = PRICE_RE.search(text or "")
    return m.group(0) if m else None


def _parse_rating(text: str) -> float | None:
    m = re.search(r"(\d(?:\.\d)?)\s*(?:out of|/)\s*5", text or "")
    if m:
        return float(m.group(1))
    m = re.match(r"^\s*(\d(?:\.\d)?)\s*$", text or "")
    return float(m.group(1)) if m else None


def _parse_int(text: str) -> int | None:
    m = re.search(r"[\d,]+", (text or "").replace("\u00a0", " "))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _stamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
