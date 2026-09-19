"""Whitelisted browser-action executor for the research agent.

The model can only REQUEST actions; this module decides what actually runs.
Every action is one of the methodology-approved verbs, executed against the
real persistent Chromium through BrowserManager and EvidenceCollector. Raw
pages are never stored — only structured, redacted evidence.

Auth safety: after any navigation the page is classified (sign-in wall,
CAPTCHA, OTP challenge) using AmazonAuthManager's DOM heuristics. If manual
owner intervention is required, the runner pauses the session safely
(`waiting_recovery` agent state, `paused_auth` runner status) instead of
corrupting research state.

Every executed action is journaled to the event log with redacted detail.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urlparse

from app.amazon_auth import AmazonAuthManager
from app.browser_manager import BrowserManager, BrowserManagerError
from app.evidence_capture import EvidenceCaptureError, EvidenceCollector
from app.events import EventLog
from app.marketplace import (
    AMAZON_MARKETPLACES,
    Marketplace,
    get_marketplace,
    marketplace_from_url,
)
from app.redact import redact, scrub_url

# Search-path templates per marketplace for Amazon search URLs.
_SEARCH_PATH = "/s?k={query}"


class ActionError(Exception):
    pass


class AuthPauseRequired(Exception):
    """Raised when a page requires manual owner intervention (login/CAPTCHA)."""

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


@dataclass
class ActionResult:
    action: str
    ok: bool
    marketplace: str
    url: str
    evidence_ids: list[str]
    summary: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "ok": self.ok,
            "marketplace": self.marketplace,
            "url": self.url,
            "evidence_ids": self.evidence_ids,
            "summary": self.summary,
            "error": self.error,
        }


class ActionExecutor:
    def __init__(
        self,
        config: Any,
        browser: BrowserManager,
        collector: EvidenceCollector,
        amazon_auth: AmazonAuthManager,
        events: EventLog,
    ) -> None:
        self.config = config
        self.browser = browser
        self.collector = collector
        self.amazon_auth = amazon_auth
        self.events = events
        # Marketplace rotation state within a research run.
        self._marketplace_cycle: list[Marketplace] = []
        self._cycle_pos = 0

    # ------------------------------------------------------------------ setup
    def set_marketplaces(self, marketplaces: list[Marketplace]) -> None:
        self._marketplace_cycle = list(marketplaces)
        self._cycle_pos = 0

    def next_marketplace(self) -> Marketplace:
        if not self._marketplace_cycle:
            self._marketplace_cycle = [AMAZON_MARKETPLACES["us"]]
        mkt = self._marketplace_cycle[self._cycle_pos % len(self._marketplace_cycle)]
        self._cycle_pos += 1
        return mkt

    # ------------------------------------------------------------------ guards
    async def _check_auth_wall(self, page: Any) -> None:
        """Pause the research safely if a sign-in/CAPTCHA wall is detected."""
        try:
            mkt = marketplace_from_url(page.url or "") or self.next_marketplace()
            check = await self.amazon_auth._classify_page(page, mkt)
        except Exception:
            return  # classification is best-effort; never break research on it
        if check.status in ("login_required", "captcha_required", "otp_required"):
            raise AuthPauseRequired(
                f"manual authentication required ({check.status})",
                detail=check.to_dict(),
            )

    # ------------------------------------------------------------------ actions
    async def execute(
        self,
        *,
        session_id: str,
        action: str,
        args: dict[str, Any],
    ) -> ActionResult:
        handler = getattr(self, f"_do_{action}", None)
        if handler is None or not action.replace("_", "").isalnum():
            raise ActionError(f"unknown or disallowed action {action!r}")
        try:
            result = await handler(session_id, args)
        except AuthPauseRequired:
            raise
        except (BrowserManagerError, EvidenceCaptureError, ActionError) as exc:
            result = ActionResult(
                action=action, ok=False, marketplace="", url="",
                evidence_ids=[], summary="action failed", error=str(exc),
            )
        self.events.append(
            session_id,
            level="info" if result.ok else "warn",
            actor="browser",
            action=f"action_{action}",
            detail=redact(result.to_dict()),
        )
        return result

    # ---- navigation actions ------------------------------------------------
    async def _do_search_marketplace(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ActionError("search_marketplace requires 'query'")
        mkt = self._marketplace_for(args)
        page = await self.browser.open_marketplace(mkt, _SEARCH_PATH.format(query=quote_plus(query)))
        try:
            await self._check_auth_wall(page)
            captured = await self.collector.capture_search_results(
                page, session_id=session_id, marketplace=mkt, query=query,
                max_items=int(args.get("max_items", 20)),
                screenshot=bool(args.get("screenshot", True)),
            )
            result_count = captured[0].record.data.get("result_count", 0) if captured else 0
            evidence_ids = [c.record.id for c in captured]
            return ActionResult(
                action="search_marketplace", ok=True, marketplace=mkt.code,
                url=scrub_url(page.url), evidence_ids=evidence_ids,
                summary=f"searched {query!r} on {mkt.code}: {result_count} results",
            )
        finally:
            await self.browser.close_page(page)

    async def _do_open_category(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        """Open a bestseller category page (args: node_id or url path)."""
        mkt = self._marketplace_for(args)
        path = str(args.get("path") or args.get("node_id") or "").strip()
        if not path:
            raise ActionError("open_category requires 'path' (category URL path)")
        if not path.startswith("/"):
            path = "/" + path
        page = await self.browser.open_marketplace(mkt, path)
        try:
            await self._check_auth_wall(page)
            captured = await self.collector.capture_page_state(
                page, session_id=session_id, note=f"category {path} on {mkt.code}"
            )
            return ActionResult(
                action="open_category", ok=True, marketplace=mkt.code,
                url=scrub_url(page.url), evidence_ids=[captured.record.id],
                summary=f"opened category {path} on {mkt.code}",
            )
        finally:
            await self.browser.close_page(page)

    async def _do_bestsellers(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        mkt = self._marketplace_for(args)
        page = await self.browser.open_marketplace(mkt, "/gp/bestsellers/books")
        try:
            await self._check_auth_wall(page)
            captured = await self.collector.capture_page_state(
                page, session_id=session_id, note=f"bestsellers {mkt.code}"
            )
            return ActionResult(
                action="bestsellers", ok=True, marketplace=mkt.code,
                url=scrub_url(page.url), evidence_ids=[captured.record.id],
                summary=f"opened bestsellers on {mkt.code}",
            )
        finally:
            await self.browser.close_page(page)

    async def _do_new_releases(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        mkt = self._marketplace_for(args)
        page = await self.browser.open_marketplace(mkt, "/gp/new-releases/books")
        try:
            await self._check_auth_wall(page)
            captured = await self.collector.capture_page_state(
                page, session_id=session_id, note=f"new releases {mkt.code}"
            )
            return ActionResult(
                action="new_releases", ok=True, marketplace=mkt.code,
                url=scrub_url(page.url), evidence_ids=[captured.record.id],
                summary=f"opened new releases on {mkt.code}",
            )
        finally:
            await self.browser.close_page(page)

    async def _do_autocomplete(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        """Amazon search suggestions (completion API via page fetch)."""
        query = str(args.get("query") or "").strip()
        if not query:
            raise ActionError("autocomplete requires 'query'")
        mkt = self._marketplace_for(args)
        page = await self.browser.open_marketplace(mkt, "/")
        try:
            suggestion: list[str] = []
            try:
                # Firefox/Safari block cross-origin fetch; Chromium's page context
                # with correct referrer usually permits this same-domain call.
                suggestion = await page.evaluate(
                    """async (q) => {
                        const res = await fetch('/suggestion?alias=stripbooks&prefix=' + encodeURIComponent(q), {headers: {'accept': 'application/json'}});
                        if (!res.ok) return [];
                        const data = await res.json();
                        return (data && Array.isArray(data[1])) ? data[1].slice(0, 12) : [];
                    }""",
                    query,
                )
            except Exception:
                suggestion = []
            record = self.collector.evidence_store.create(
                session_id=session_id,
                marketplace=mkt.code,
                kind="autocomplete",
                url=scrub_url(page.url),
                title=f"autocomplete: {query}",
                data={"query": query, "suggestions": suggestion},
            )
            return ActionResult(
                action="autocomplete", ok=True, marketplace=mkt.code,
                url=scrub_url(page.url), evidence_ids=[record.id],
                summary=f"{len(suggestion)} suggestions for {query!r}",
            )
        finally:
            await self.browser.close_page(page)

    async def _do_open_product(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        asin = str(args.get("asin") or args.get("url") or "").strip()
        if not asin:
            raise ActionError("open_product requires 'asin' (or full url)")
        mkt = self._marketplace_for(args)
        if asin.startswith("http"):
            url_path = urlparse(asin).path + ("?" + urlparse(asin).query if urlparse(asin).query else "")
        else:
            url_path = f"/dp/{asin}"
        page = await self.browser.open_marketplace(mkt, url_path)
        try:
            await self._check_auth_wall(page)
            captured = await self.collector.capture_product_page(
                page, session_id=session_id, marketplace=mkt,
                screenshot=bool(args.get("screenshot", True)),
            )
            return ActionResult(
                action="open_product", ok=True, marketplace=mkt.code,
                url=scrub_url(page.url), evidence_ids=[captured.record.id],
                summary=f"product {captured.record.data.get('asin') or asin}: title={bool(captured.record.data.get('title'))}",
            )
        finally:
            await self.browser.close_page(page)

    async def _do_read_reviews(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        asin = str(args.get("asin") or "").strip()
        if not asin:
            raise ActionError("read_reviews requires 'asin'")
        mkt = self._marketplace_for(args)
        page = await self.browser.open_marketplace(
            mkt, f"/product-reviews/{asin}/ref=cm_cr_dp_d_show_all_btm?ie=UTF8&reviewerType=all_reviews"
        )
        try:
            await self._check_auth_wall(page)
            reviews = await _extract_reviews(page)
            record = self.collector.evidence_store.create(
                session_id=session_id,
                marketplace=mkt.code,
                kind="reviews",
                url=scrub_url(page.url),
                title=f"reviews: {asin}",
                data={"asin": asin, "reviews": reviews},
            )
            return ActionResult(
                action="read_reviews", ok=True, marketplace=mkt.code,
                url=scrub_url(page.url), evidence_ids=[record.id],
                summary=f"captured {len(reviews)} reviews for {asin}",
            )
        finally:
            await self.browser.close_page(page)

    async def _do_related_products(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        asin = str(args.get("asin") or "").strip()
        if not asin:
            raise ActionError("related_products requires 'asin'")
        mkt = self._marketplace_for(args)
        page = await self.browser.open_marketplace(mkt, f"/dp/{asin}")
        try:
            await self._check_auth_wall(page)
            items = await _extract_carousel(page)
            record = self.collector.evidence_store.create(
                session_id=session_id,
                marketplace=mkt.code,
                kind="related_products",
                url=scrub_url(page.url),
                title=f"related: {asin}",
                data={"asin": asin, "related": items},
            )
            return ActionResult(
                action="related_products", ok=True, marketplace=mkt.code,
                url=scrub_url(page.url), evidence_ids=[record.id],
                summary=f"{len(items)} related items for {asin}",
            )
        finally:
            await self.browser.close_page(page)

    async def _do_kdspy_data(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        """Read KDSpy panel data on the CURRENT Amazon page context.

        Requires the KDSpy extension loaded and rendered. Navigates to the
        target (default: search page for 'query') then reads the panel.
        """
        mkt = self._marketplace_for(args)
        query = str(args.get("query") or "").strip()
        path = _SEARCH_PATH.format(query=quote_plus(query)) if query else "/"
        page = await self.browser.open_marketplace(mkt, path)
        try:
            captured = await self.collector.capture_kdspy_panel(
                page, session_id=session_id, marketplace=mkt,
                screenshot=bool(args.get("screenshot", True)),
            )
            return ActionResult(
                action="kdspy_data", ok=True, marketplace=mkt.code,
                url=scrub_url(page.url), evidence_ids=[captured.record.id],
                summary="captured KDSpy panel data",
            )
        except EvidenceCaptureError as exc:
            return ActionResult(
                action="kdspy_data", ok=False, marketplace=mkt.code,
                url=scrub_url(page.url), evidence_ids=[],
                summary="KDSpy panel not available", error=str(exc),
            )
        finally:
            await self.browser.close_page(page)

    async def _do_capture_evidence(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        page = await self.browser.new_page()
        url = str(args.get("url") or "").strip()
        if url:
            await self.browser.navigate(page, url)
        captured = await self.collector.capture_page_state(
            page, session_id=session_id, note=str(args.get("note") or "")
        )
        await self.browser.close_page(page)
        return ActionResult(
            action="capture_evidence", ok=True, marketplace="",
            url=scrub_url(captured.record.url), evidence_ids=[captured.record.id],
            summary=f"captured page state: {captured.record.title[:80]}",
        )

    async def _do_record_observation(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        note = str(args.get("note") or args.get("text") or "").strip()
        if not note:
            raise ActionError("record_observation requires 'note'")
        self.events.append(
            session_id, level="info", actor="agent",
            action="agent_observation",
            detail={"note": note[:1000]},
        )
        return ActionResult(
            action="record_observation", ok=True, marketplace="", url="",
            evidence_ids=[], summary="recorded agent observation",
        )

    async def _do_wait(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        seconds = min(float(args.get("seconds", 2)), 10)
        import asyncio

        await asyncio.sleep(seconds)
        return ActionResult(
            action="wait", ok=True, marketplace="", url="",
            evidence_ids=[], summary=f"waited {seconds}s",
        )

    # ------------------------------------------------------------------ helpers
    def _marketplace_for(self, args: dict[str, Any]) -> Marketplace:
        ident = str(args.get("marketplace") or "").strip()
        if ident:
            return get_marketplace(ident)
        return self.next_marketplace()


# --------------------------------------------------------------------- DOM
async def _extract_reviews(page: Any) -> list[dict[str, Any]]:
    """Extract up to ~20 visible reviews (rating, title, date, text excerpt)."""
    reviews: list[dict[str, Any]] = []
    try:
        cards = await page.query_selector_all("div[data-hook='review']")
        for card in cards[:20]:
            try:
                rating = await _txt(card, "i[data-hook='review-star-rating'] span, i[data-hook='cmps-review-star-rating'] span")
                title = await _txt(card, "a[data-hook='review-title'] span:last-child, a[data-hook='review-title']")
                date = await _txt(card, "span[data-hook='review-date']")
                body = await _txt(card, "span[data-hook='review-body']")
                verified = await card.query_selector("span[data-hook='avp-badge']")
                reviews.append({
                    "rating": _rating_num(rating),
                    "title": (title or "").strip()[:200],
                    "date": (date or "").strip()[:60],
                    "text": (body or "").strip()[:1200],
                    "verified_purchase": verified is not None,
                })
            except Exception:
                continue
    except Exception:
        pass
    return reviews


async def _extract_carousel(page: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        cards = await page.query_selector_all("li[data-asin], div[data-asin]")
        seen: set[str] = set()
        for card in cards[:40]:
            try:
                asin = (await card.get_attribute("data-asin") or "").strip()
                if not asin or asin in seen:
                    continue
                seen.add(asin)
                title = await _txt(card, "img")
                alt_title = await _attr(card, "aria-label")
                items.append({"asin": asin, "title": (alt_title or title or "").strip()[:200]})
            except Exception:
                continue
    except Exception:
        pass
    return items


async def _txt(el: Any, selector: str) -> str:
    try:
        node = await el.query_selector(selector)
        if node is None:
            return ""
        return (await node.inner_text()) or ""
    except Exception:
        return ""


async def _attr(el: Any, name: str) -> str:
    try:
        return (await el.get_attribute(name)) or ""
    except Exception:
        return ""


def _rating_num(text: str) -> float | None:
    import re

    m = re.search(r"(\d(?:\.\d)?)\s*(?:out of|/)", text or "")
    return float(m.group(1)) if m else None
