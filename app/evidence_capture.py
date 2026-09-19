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
from app.browser_manager import BrowserManager, BrowserManagerError
from app.browser_store import BrowserEvidence, BrowserEvidenceStore
from app.marketplace import Marketplace, marketplace_from_url
from app.redact import redact, scrub_url

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
        """Extract organic results (rank/ASIN/title/price/rating/reviews)."""
        items = await _extract_search_items(page, max_items)
        data = {
            "query": query,
            "result_count": len(items),
            "results": items,
        }
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
        title = (await _safe_title(page)) or ""
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
    # BSR: look in product details sections.
    bsr_text = (await _text_of(page, "#productDetails_detailBullets_sections1, #detailBullets_feature_div, #SalesRank")) or ""
    m = re.search(r"#?\s?([\d,]+)\s+in\s+([^\n(]+)", bsr_text)
    if m:
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


async def _extract_kdspy_panel(page: Any) -> dict[str, Any] | None:
    """Read the KDSpy panel the extension rendered, if present.

    KDSpy injects UI into Amazon pages. Selectors are intentionally
    defensive: we look for common extension iframe/panel patterns and only
    return data actually rendered. Returns None when nothing is found.
    """
    # 1. Extension iframes (chrome-extension:// scheme).
    frames = []
    try:
        frames = page.frames
    except Exception:
        frames = []
    for frame in frames:
        url = getattr(frame, "url", "") or ""
        if "chrome-extension://" in url:
            body = ""
            try:
                body = (await frame.inner_text("body"))[:8000]
            except Exception:
                body = ""
            if body.strip():
                return {
                    "source": "extension_frame",
                    "frame_url_host": url.split("chrome-extension://")[-1].split("/")[0],
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
