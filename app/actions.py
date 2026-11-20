"""The research agent's browser-action executor.

The model REQUESTS actions and this module runs them against the real
persistent Camoufox browser through BrowserManager and EvidenceCollector.
There is no host allow-list and no per-page restriction: ``navigate`` goes
anywhere, ``evaluate`` runs real page JavaScript, and click/type/press/scroll/
hover/select/history/reload behave like a person at the keyboard. What stays
deterministic is the EVIDENCE: raw pages are never stored — only structured,
redacted records with stable ids.

The methodology phases (app.methodology) list the verbs each phase may use;
both the browser surface and the record-edit surface are present in every
phase, so a researcher is never told "you may look, but not there".

Auth safety: after any navigation the page is classified (sign-in wall,
CAPTCHA, OTP challenge) using AmazonAuthManager's DOM heuristics. Sign-in is
OPTIONAL — a login wall is skipped as a failed action and the model decides
what to do without that page (never a backend blocker: the durable auth
state can be wrong, per-domain). Only CAPTCHA/OTP, which need the owner's
hands, pause the session safely (`waiting_recovery` agent state,
`paused_auth` runner status) instead of corrupting research state.

Every executed action is journaled to the event log with redacted detail.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urlparse

from app import model_browser
from app.amazon_auth import ACCOUNT_AMAZON, AmazonAuthManager
from app.browser_manager import (
    BrowserManager,
    BrowserManagerError,
)
from app.events import EventLog
from app.evidence_capture import (
    EvidenceCaptureError,
    EvidenceCollector,
    book_scout_present,
)
from app.frontier import ALIAS, SUGGEST_JS, mid_for
from app.marketplace import (
    AMAZON_MARKETPLACES,
    Marketplace,
    get_marketplace,
    marketplace_from_url,
)
from app.product_rules import generic_completion, generic_seed_reason
from app.redact import redact, scrub_url
from app.search_totals import competition_text

# Search-path templates per marketplace for Amazon search URLs.
_SEARCH_PATH = "/s?k={query}"
# Amazon departments are SEPARATE catalogues: the Books store and the
# Kindle Store keep different category trees, so research can target either
# explicitly (kindle == "digital-text"). 'all' keeps the historical
# behaviour — no `i=` parameter at all.
_DEPARTMENT_PARAM = {
    "all": "",
    "books": "&i=stripbooks",
    "kindle": "&i=digital-text",
}


class ActionError(Exception):
    pass


# --------------------------------------------------------------------------- #
# External (non-Amazon) research actions
# --------------------------------------------------------------------------- #
# A deliberately tiny allow-list. External research is injected as evidence
# in the MIDDLE of the methodology (P4/P5/P6, plus a gated second wave in P1
# for autonomous runs), never as its own phase: the records land in the same
# evidence table with stable ids and flow through redact() -> claims ->
# filters -> gates untouched. Nothing outside this list is ever navigated to
# by an external action.
EXTERNAL_DOMAIN_ALLOWLIST: frozenset[str] = frozenset(
    {
        "www.reddit.com",
        "reddit.com",
        "old.reddit.com",
        "www.goodreads.com",
        "goodreads.com",
        "www.google.com",
        "google.com",
    }
)

_GOOGLE_SUGGEST_JS = """async (q) => {
  try {
    const res = await fetch('/complete/search?client=firefox&hl=en&q=' + encodeURIComponent(q),
                            {headers: {'accept': 'application/json'}});
    if (!res.ok) return [];
    const data = await res.json();
    return (Array.isArray(data) && Array.isArray(data[1])) ? data[1].slice(0, 10) : [];
  } catch (e) { return []; }
}"""

_REDDIT_RESULTS_JS = r"""() => {
  const out = [];
  const seen = new Set();
  const push = (title, extra) => {
    const t = (title || '').trim().replace(/\s+/g, ' ');
    if (!t || t.length < 12 || seen.has(t)) return;
    seen.add(t);
    out.push(Object.assign({title: t.slice(0, 220)}, extra || {}));
  };
  document.querySelectorAll('a[data-testid="post-title"]').forEach((a) => {
    const card = a.closest('a[href*="/comments/"]') || a.parentElement;
    let ups = '';
    if (card && card.parentElement) ups = (card.parentElement.textContent || '').match(/(\d[\d,.]*[kKmM]?)\s*upvote/)?.[1] || '';
    push(a.textContent, {upvotes: ups});
  });
  if (!out.length) {
    document.querySelectorAll('a.title, h3 a').forEach((a) => push(a.textContent, {}));
  }
  return out.slice(0, 20);
}"""

_GOODREADS_JS = r"""() => {
  const num = (s) => {
    const m = String(s || '').replace(/[^0-9]/g, '');
    return m ? Number(m) : null;
  };
  let resultCount = null;
  const bodyText = (document.body.innerText || '').slice(0, 20000);
  const m = bodyText.match(/of\s+([\d,]+)\s+results/i);
  if (m) resultCount = num(m[1]);
  const out = [];
  document.querySelectorAll('tr.bookish, tr[itemscope], table.tableList tr').forEach((tr) => {
    const a = tr.querySelector('a.bookTitle');
    if (!a) return;
    const author = tr.querySelector('a.authorName')?.textContent?.trim() || '';
    const meta = tr.querySelector('span.minirating')?.textContent?.trim() || '';
    const rm = meta.match(/([0-9.]+)\s+avg rating\s*—\s*([\d,]+)\s+ratings/i);
    out.push({
      title: (a.textContent || '').trim().slice(0, 200),
      author: author.slice(0, 120),
      avg_rating: rm ? Number(rm[1]) : null,
      ratings_count: rm ? num(rm[2]) : null,
    });
  });
  return {result_count: resultCount, results: out.slice(0, 10)};
}"""


def _allowed_external_host(url: str) -> bool:
    """Deny-by-default host check for external research navigation."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in EXTERNAL_DOMAIN_ALLOWLIST:
        return True
    return any(host.endswith("." + d) for d in EXTERNAL_DOMAIN_ALLOWLIST)


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
        # The page the model is driving ITSELF (navigate/click/type/press/
        # scroll). Kept across actions so a click lands on the page the model
        # just navigated, not on a fresh blank one.
        self._working: dict[str, Any] = {}

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
    async def _check_auth_wall(self, page: Any, session_id: str | None = None) -> None:
        """Never block on sign-in; pause only for CAPTCHA/OTP.

        Sign-in is OPTIONAL by design: the owner may not sign in at all, and
        the durable auth state can be wrong (Amazon sessions are per-domain,
        the store can read login_required while the page actually shows
        reviews). So a login wall is never a backend decision — the page is
        skipped as a failed action and the MODEL decides what to do without
        it (skip review mining, or keep going with the evidence that is
        visible). CAPTCHA/OTP are different: they block every page and need
        the owner's hands, so those still pause the session safely.
        """
        try:
            mkt = marketplace_from_url(page.url or "") or self.next_marketplace()
            check = await self.amazon_auth._classify_page(page, mkt)
        except Exception:
            return  # classification is best-effort; never break research on it
        if check.status not in ("login_required", "captcha_required", "otp_required"):
            return
        wall_code = str((check.detail or {}).get("marketplace") or "")
        # Persist the wall (status + marketplace) on the durable auth
        # store: the owner's Sign-in buttons read it to show which store
        # last needed them. Best-effort bookkeeping only — it must never
        # block the flow itself. (The durable signed-in marker inside
        # detail is preserved by the store itself.)
        try:
            self.amazon_auth.auth_store.set(
                ACCOUNT_AMAZON,
                self.amazon_auth.profile,
                check.status,
                {
                    "via": "research_wall",
                    "url": scrub_url(check.url),
                    "marketplace": wall_code,
                },
            )
        except Exception:
            pass
        mkt_obj = AMAZON_MARKETPLACES.get(wall_code)
        where = f" on {mkt_obj.domain}" if mkt_obj is not None else ""
        if check.status == "login_required":
            # Skip, never pause: sign-in is the owner's choice, not a
            # research blocker. The failed action is journaled so the model
            # sees WHY the page produced nothing and decides whether to
            # drop that source (e.g. review mining) or route around it.
            if session_id is not None:
                try:
                    self.events.append(
                        session_id,
                        level="warn",
                        actor="system",
                        action="sign_in_wall_skipped",
                        detail={
                            "marketplace": wall_code,
                            "url": scrub_url(check.url),
                        },
                    )
                except Exception:
                    pass
            raise ActionError(
                f"sign-in wall ({check.status}){where} — page skipped: sign-in is "
                "optional, so continue with the evidence that IS visible "
                "(you decide what, if anything, to skip)"
            )
        domain = mkt_obj.domain if mkt_obj is not None else "the marketplace"
        raise AuthPauseRequired(
            f"Amazon {check.status}{where} — open Settings → Amazon "
            f"authentication → Sign in on {domain}; research resumes "
            "automatically when the sign-in completes",
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
        # Optional department: 'all' (default) | 'books' | 'kindle'.
        department = str(args.get("department") or "all").strip().lower()
        if department not in _DEPARTMENT_PARAM:
            raise ActionError(
                f"unknown department {department!r} — use 'all', 'books' or 'kindle'"
            )
        mkt = self._marketplace_for(args)
        path = (
            _SEARCH_PATH.format(query=quote_plus(query))
            + _DEPARTMENT_PARAM[department]
        )
        page = await self.browser.open_marketplace(mkt, path)
        try:
            await self._check_auth_wall(page, session_id)
            captured = await self.collector.capture_search_results(
                page, session_id=session_id, marketplace=mkt, query=query,
                max_items=int(args.get("max_items", 20)),
                screenshot=bool(args.get("screenshot", True)),
            )
            data = captured[0].record.data if captured else {}
            evidence_ids = [c.record.id for c in captured]
            # Amazon's OWN printed total is the competition figure; the
            # number of rows this capture pulled is reported separately and
            # is never called "results" on its own.
            suffix = f" ({department} department)" if department != "all" else ""
            return ActionResult(
                action="search_marketplace", ok=True, marketplace=mkt.code,
                url=scrub_url(page.url), evidence_ids=evidence_ids,
                summary=(
                    f"searched {query!r} on {mkt.code}{suffix}: "
                    f"{competition_text(data)}"
                ),
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
            await self._check_auth_wall(page, session_id)
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
            await self._check_auth_wall(page, session_id)
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
            await self._check_auth_wall(page, session_id)
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
        """Amazon search suggestions for a TARGETED phrase.

        Autocomplete is only as specific as its seed: "grief" completes to
        "grief books", "grief journal", "grief gifts" — the whole store. So a
        generic seed is refused with the reason and an example of what to try
        instead.

        Every completion the targeted seed returns is handed over, unfiltered:
        which suggestions are worth pursuing is a research judgement and it
        belongs to the model, not to a word list. The store-root completions
        are MARKED (`store_root_suggestions`) so the model can weigh them, and
        nothing is thrown away.
        """
        query = " ".join(str(args.get("query") or "").split())
        if not query:
            raise ActionError("autocomplete requires 'query'")
        generic = generic_seed_reason(query)
        if generic:
            raise ActionError(
                f"autocomplete needs a targeted phrase, not a generic one — "
                f"{generic}. Seed with a reader and a situation, e.g. "
                "'sudden loss grief for widowers in year one' or "
                "'perimenopause sleep for shift nurses', then extend what "
                "Amazon completes. Generic seeds only return store roots."
            )
        mkt = self._marketplace_for(args)
        page = await self.browser.open_marketplace(mkt, "/")
        try:
            suggestion: list[str] = []
            try:
                # Same-domain completion call. The endpoint list (and the
                # marketplace id it needs) lives in app.frontier: Amazon
                # retired the legacy path, so hard-coding it here made this
                # action return zero suggestions on every run.
                suggestion = await page.evaluate(
                    SUGGEST_JS,
                    [ALIAS, mid_for(getattr(mkt, "domain", "")), query],
                )
            except Exception:
                suggestion = []
            suggestions = [
                str(t).strip() for t in (suggestion or []) if str(t).strip()
            ]
            roots = [t for t in suggestions if generic_completion(t)]
            record = self.collector.evidence_store.create(
                session_id=session_id,
                marketplace=mkt.code,
                kind="autocomplete",
                url=scrub_url(page.url),
                title=f"autocomplete: {query}",
                data={
                    "query": query,
                    "suggestions": suggestions,
                    "store_root_suggestions": roots,
                    "note": (
                        "all completions are here, exactly as Amazon returned "
                        "them; 'store_root_suggestions' are the ones that name "
                        "only a format, a broad market or a bare audience — "
                        "you decide whether any is worth pursuing"
                    ),
                },
            )
            summary = f"{len(suggestions)} suggestions for {query!r}"
            if roots:
                summary += f" ({len(roots)} of them are store roots)"
            return ActionResult(
                action="autocomplete", ok=True, marketplace=mkt.code,
                url=scrub_url(page.url), evidence_ids=[record.id],
                summary=summary,
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
            await self._check_auth_wall(page, session_id)
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
        """Read a book's reviews: the product page first, then the all-reviews
        pages, with a screenshot of the review section as proof.

        The product page is tried first because that is where Amazon renders
        the review block inline (and where reviews are readable without an
        account); the all-reviews pages are tried only when that page showed
        none, and they are best-effort — a sign-in wall there must not abort a
        capture that already succeeded.
        """
        asin = str(args.get("asin") or "").strip()
        if not asin:
            raise ActionError("read_reviews requires 'asin'")
        mkt = self._marketplace_for(args)
        max_reviews = int(args.get("max_reviews") or 40)

        page = await self.browser.open_marketplace(mkt, f"/dp/{asin}")
        try:
            await self._check_auth_wall(page, session_id)
            captured = await self.collector.capture_reviews(
                page, session_id=session_id, marketplace=mkt, asin=asin,
                max_reviews=max_reviews,
            )
            if not int((captured.record.data or {}).get("reviews_captured") or 0):
                # Fall through to the dedicated review pages.
                try:
                    page2 = await self.browser.open_marketplace(
                        mkt,
                        f"/product-reviews/{asin}/?ie=UTF8&reviewerType=all_reviews",
                    )
                except Exception as exc:
                    page2 = None
                    self.events.append(
                        session_id, level="warn", actor="agent",
                        action="reviews_page_failed",
                        detail={"asin": asin,
                                "error": f"{type(exc).__name__}: {exc}"[:300]},
                    )
                if page2 is not None:
                    try:
                        await self._check_auth_wall(page2, session_id)
                        captured = await self.collector.capture_reviews(
                            page2, session_id=session_id, marketplace=mkt,
                            asin=asin, max_reviews=max_reviews,
                        )
                        await self.browser.close_page(page)
                        page = page2
                    except AuthPauseRequired:
                        await self.browser.close_page(page2)
                        raise
                    except Exception as exc:
                        # Keep the product-page record: it says why reviews
                        # were unreadable, which is information for the report.
                        self.events.append(
                            session_id, level="warn", actor="agent",
                            action="reviews_page_failed",
                            detail={"asin": asin,
                                    "error": f"{type(exc).__name__}: {exc}"[:300]},
                        )
                        await self.browser.close_page(page2)
            data = captured.record.data or {}
            count = int(data.get("reviews_captured") or 0)
            bits = [f"captured {count} review(s) for {asin}" if count else f"no reviews captured for {asin}"]
            if isinstance(data.get("average_rating"), (int, float)):
                bits.append(f"average {data['average_rating']}")
            if data.get("ratings_total"):
                bits.append(f"{data['ratings_total']} ratings in total")
            if data.get("critical_reviews"):
                bits.append(f"{data['critical_reviews']} critical of the ones read")
            bits.append("screenshot saved" if captured.screenshot_artifact_id
                        else "no screenshot")
            if data.get("blocked_reason"):
                bits.append(str(data["blocked_reason"]))
            return ActionResult(
                action="read_reviews", ok=True, marketplace=mkt.code,
                url=scrub_url(page.url), evidence_ids=[captured.record.id],
                summary="; ".join(bits),
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
            await self._check_auth_wall(page, session_id)
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
        """Read KDSpy panel data on an Amazon page.

        Requires the KDSpy extension loaded and rendered. Opens the target —
        a search for 'query', a product by 'asin', or any 'url' — then reads
        whatever the extension rendered there. KDSpy's own injected controls
        are also clickable by the model like any other page element.
        """
        mkt = self._marketplace_for(args)
        query = str(args.get("query") or "").strip()
        asin = str(args.get("asin") or "").strip()
        url = str(args.get("url") or "").strip()
        if url.startswith("http"):
            page = await self.browser.new_page()
            page = await self.browser.navigate(page, url)
        else:
            if asin:
                path = f"/dp/{asin}"
            else:
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

    async def _do_book_scout_scan(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        """Read BSR for a whole search results page via Scrivy Book Scout.

        Amazon prints a rank only on a product page, so a search capture used
        to be rank-blind. The Book Scout add-on reads each result's page and
        renders the rank under the book; this action opens a search (or reads
        the page already open, when no query is given) and captures what the
        add-on rendered — every badge, the niche snapshot and its details
        table. Without the add-on the same ranks are read natively: each result's
        `/dp/<asin>` is fetched from the search page itself (same session, the
        add-on's own trick) and the printed rank parsed — so this action works
        with nothing installed. Only Amazon's own ranks are recorded on that
        path; the add-on's sales/revenue estimates are not.
        """
        mkt = self._marketplace_for(args)
        query = str(args.get("query") or "").strip()
        url = str(args.get("url") or "").strip()
        page = None
        opened = False
        if url.startswith("http"):
            page = await self.browser.new_page()
            page = await self.browser.navigate(page, url)
            opened = True
        elif query:
            # The Books catalogue is where the add-on's BSR badges belong; a
            # Kindle/all search mixes storefronts whose ranks are not the
            # print ranks the research is about.
            page = await self.browser.open_marketplace(
                mkt, _SEARCH_PATH.format(query=quote_plus(query)) + _DEPARTMENT_PARAM["books"]
            )
            opened = True
        else:
            page = await self._working_page(session_id)
        try:
            if opened:
                await self._check_auth_wall(page, session_id)
            # Add-on first (instant badges + its own sales/revenue estimates),
            # then the add-on-free path: fetch each result's product page in
            # this same browser session and read Amazon's printed rank. The
            # capability does not depend on the add-on being installed.
            if await book_scout_present(page):
                captured = await self.collector.capture_book_scout(
                    page,
                    session_id=session_id,
                    marketplace=mkt,
                    query=query,
                    wait_seconds=float(args.get("wait_seconds") or 45.0),
                    screenshot=bool(args.get("screenshot", True)),
                )
                counts = (captured.record.data or {}).get("counts") or {}
                niche = [
                    f"{s.get('label')} {s.get('value')}".strip()
                    for s in ((captured.record.data or {}).get("niche") or {}).get("stats", [])
                    if isinstance(s, dict)
                ]
                summary = (
                    f"Book Scout: {counts.get('with_bsr', 0)}/"
                    f"{counts.get('badges', 0)} results carry a BSR"
                    + (f" · {counts.get('unavailable', 0)} unavailable" if counts.get("unavailable") else "")
                    + (f" · niche: {', '.join(niche[:5])}" if niche else "")
                )
                return ActionResult(
                    action="book_scout_scan", ok=True, marketplace=mkt.code,
                    url=scrub_url(page.url), evidence_ids=[captured.record.id],
                    summary=summary,
                )
            captured = await self.collector.capture_bsr_scan(
                page,
                session_id=session_id,
                marketplace=mkt,
                max_items=int(args.get("max_items") or 16),
                screenshot=bool(args.get("screenshot", True)),
            )
            result = captured.record.data or {}
            best = min(
                (r for r in result.get("rows") or [] if r.get("rank")),
                key=lambda r: r.get("rank") or 0,
                default=None,
            )
            summary = (
                f"BSR scan (add-on not loaded — read each product page): "
                f"{result.get('with_bsr', 0)}/{result.get('scanned', 0)} results carry a BSR"
                + (f" · best #{best['rank']:,}: {best.get('category')}" if best else "")
            )
            return ActionResult(
                action="book_scout_scan", ok=True, marketplace=mkt.code,
                url=scrub_url(page.url), evidence_ids=[captured.record.id],
                summary=summary,
            )
        except EvidenceCaptureError as exc:
            return ActionResult(
                action="book_scout_scan", ok=False, marketplace=mkt.code,
                url=scrub_url(page.url), evidence_ids=[],
                summary="Scrivy Book Scout data not available", error=str(exc),
            )
        finally:
            if opened:
                await self.browser.close_page(page)

    # ------------------------------------------------------- external sources
    async def _open_external(self, url: str) -> Any:
        """Open an allow-listed external page. Deny-by-default on host."""
        if not _allowed_external_host(url):
            raise ActionError(f"external host not allow-listed: {url}")
        page = await self.browser.new_page()
        try:
            page = await self.browser.navigate(page, url)
        except Exception:
            try:
                await self.browser.close_page(page)
            except Exception:
                pass
            raise
        return page

    async def _external_extract(
        self, session_id: str, *, action: str, url: str, js: str,
        query: str, source: str, marketplace: str = "",
    ) -> ActionResult:
        """Shared plumbing: navigate → extract → redact → evidence → close."""
        page = await self._open_external(url)
        try:
            try:
                data = await page.evaluate(js)
            except Exception as exc:
                return ActionResult(
                    action=action, ok=False, marketplace=marketplace,
                    url=scrub_url(url), evidence_ids=[],
                    summary=f"{source} extraction failed", error=str(exc)[:200],
                )
            if not isinstance(data, (dict, list)):
                data = [] if action != "goodreads_check" else {"result_count": None, "results": []}
            payload = {
                "source": source,
                "query": query,
                "marketplace": marketplace,
                "external": True,
            }
            if isinstance(data, dict):
                payload.update(data)
            else:
                payload["suggestions" if action == "google_suggest" else "posts"] = data
            empty = (
                not payload.get("suggestions")
                and not payload.get("posts")
                and not payload.get("results")
                and payload.get("result_count") in (None, 0)
            )
            evidence_id = ""
            if not empty:
                rec = self.collector.evidence_store.create(
                    session_id=session_id,
                    marketplace=marketplace,
                    kind="external_demand",
                    url=scrub_url(url),
                    title=f"{source}: {query}"[:300],
                    data=redact(payload),
                )
                evidence_id = rec.id
            return ActionResult(
                action=action, ok=not empty, marketplace=marketplace,
                url=scrub_url(url), evidence_ids=[evidence_id] if evidence_id else [],
                summary=(
                    f"{source}: captured for {query!r}"
                    if not empty else
                    f"{source}: nothing extracted for {query!r} — treat this source "
                    "as unavailable here and decide what to do without it"
                ),
                error=None if not empty else "empty_extraction",
            )
        finally:
            try:
                await self.browser.close_page(page)
            except Exception:
                pass

    async def _do_google_suggest(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        """Un-Amazon-anchored demand phrasing (Google's suggestion stream)."""
        query = str(args.get("query") or "").strip()
        if not query:
            raise ActionError("google_suggest requires 'query'")
        return await self._google_suggest(session_id, query)

    async def _google_suggest(self, session_id: str, query: str) -> ActionResult:
        page = await self._open_external("https://www.google.com/")
        try:
            try:
                out = await page.evaluate(_GOOGLE_SUGGEST_JS, query)
            except Exception as exc:
                return ActionResult(
                    action="google_suggest", ok=False, marketplace="", url="https://www.google.com/",
                    evidence_ids=[], summary="google_suggest extraction failed", error=str(exc)[:200],
                )
            suggestions = [str(s).strip() for s in (out or []) if str(s).strip()] if isinstance(out, list) else []
            payload = {
                "source": "google_suggest", "query": query, "external": True,
                "suggestions": suggestions[:10],
            }
            evidence_ids: list[str] = []
            if suggestions:
                rec = self.collector.evidence_store.create(
                    session_id=session_id, marketplace="", kind="external_demand",
                    url="https://www.google.com/", title=f"google_suggest: {query}"[:300],
                    data=redact(payload),
                )
                evidence_ids = [rec.id]
            return ActionResult(
                action="google_suggest", ok=bool(suggestions), marketplace="",
                url="https://www.google.com/", evidence_ids=evidence_ids,
                summary=(
                    f"{len(suggestions)} Google suggestion(s) for {query!r}"
                    if suggestions else
                    f"no Google suggestions for {query!r} — treat this source as "
                    "unavailable here and decide what to do without it"
                ),
                error=None if suggestions else "empty_extraction",
            )
        finally:
            try:
                await self.browser.close_page(page)
            except Exception:
                pass

    async def _do_forum_search(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        """Community complaint / unmet-need language (Reddit search)."""
        from urllib.parse import quote_plus

        query = str(args.get("query") or "").strip()
        if not query:
            raise ActionError("forum_search requires 'query'")
        urls = [
            f"https://www.reddit.com/search/?q={quote_plus(query)}&type=posts",
            f"https://old.reddit.com/search?q={quote_plus(query)}",
        ]
        last: ActionResult | None = None
        for url in urls:
            last = await self._external_extract(
                session_id, action="forum_search", url=url, js=_REDDIT_RESULTS_JS,
                query=query, source="reddit",
            )
            if last.ok:
                return last
        return last or ActionResult(
            action="forum_search", ok=False, marketplace="", url="",
            evidence_ids=[], summary="forum search unavailable", error="no attempt",
        )

    async def _do_goodreads_check(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        """Independent demand evidence: how many titles exist and how they rate."""
        from urllib.parse import quote_plus

        query = str(args.get("query") or "").strip()
        if not query:
            raise ActionError("goodreads_check requires 'query'")
        return await self._external_extract(
            session_id, action="goodreads_check",
            url=f"https://www.goodreads.com/search?q={quote_plus(query)}",
            js=_GOODREADS_JS, query=query, source="goodreads",
        )

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

    # ---------------------------------------------- the model's own browser
    # The model drives a real Playwright page itself: navigate, click, type,
    # press, scroll. That is how it dismisses Amazon's shipping-country notice
    # when it sees one (``click`` with target "Dismiss") instead of relying on
    # a special-purpose verb — and how it can do anything else a page needs.
    # The page SURVIVES between actions, so a click lands on the page the model
    # just navigated. Pacing is human (app.model_browser).
    async def _working_page(self, session_id: str) -> Any:
        page = self._working.get(session_id)
        if page is not None:
            try:
                if not page.is_closed():
                    return page
            except Exception:
                pass
            self._working.pop(session_id, None)
        page = await self.browser.new_page()
        self._working[session_id] = page
        return page

    async def _after_model_action(
        self, session_id: str, page: Any, summary: str, action: str
    ) -> ActionResult:
        """Refresh the screenshot the model will see, and report the state.

        ``browser.screenshot`` dismisses any refusal overlay BEFORE it captures,
        so the image attached to the next model call can never show the notice
        covering the result total. The model does not need to be told to look
        for it first — but if it does see one, ``click`` on "Dismiss" works.
        """
        try:
            self.collector.last_screenshot = await self.browser.screenshot(page)
        except Exception:
            pass  # a missing screenshot never invalidates the action itself
        mkt = marketplace_from_url(str(getattr(page, "url", "") or ""))
        return ActionResult(
            action=action, ok=True,
            marketplace=mkt.code if mkt else "",
            url=scrub_url(str(getattr(page, "url", "") or "")),
            evidence_ids=[], summary=summary,
        )

    async def _model_verb(
        self, session_id: str, action: str, op: Any
    ) -> ActionResult:
        """Run one Playwright verb the model requested, then refresh its view."""
        page = await self._working_page(session_id)
        try:
            summary = await op(page)
        except model_browser.BrowserActionError as exc:
            raise ActionError(str(exc)) from exc
        return await self._after_model_action(session_id, page, summary, action)

    async def _do_navigate(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        """Go anywhere. No host allow-list: the model decides where evidence is."""
        url = str(args.get("url") or "").strip()
        return await self._model_verb(
            session_id, "navigate", lambda page: model_browser.navigate(page, url)
        )

    async def _do_click(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        target = str(
            args.get("target") or args.get("text") or args.get("selector") or ""
        )
        return await self._model_verb(
            session_id, "click", lambda page: model_browser.click(page, target)
        )

    async def _do_type(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        target = str(
            args.get("target") or args.get("selector") or args.get("field") or ""
        )
        text = str(args.get("text") or args.get("value") or "")
        return await self._model_verb(
            session_id, "type",
            lambda page: model_browser.type_text(page, target, text),
        )

    async def _do_press(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        key = str(args.get("key") or "")
        return await self._model_verb(
            session_id, "press", lambda page: model_browser.press(page, key)
        )

    async def _do_scroll(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        direction = str(args.get("direction") or "down")
        try:
            amount = int(args.get("amount") or 1)
        except (TypeError, ValueError):
            amount = 1
        return await self._model_verb(
            session_id, "scroll",
            lambda page: model_browser.scroll(page, direction, amount),
        )

    async def _do_hover(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        target = str(args.get("target") or args.get("selector") or "")
        return await self._model_verb(
            session_id, "hover", lambda page: model_browser.hover(page, target)
        )

    async def _do_select_option(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        target = str(args.get("target") or args.get("selector") or "")
        value = str(args.get("value") or args.get("label") or "")
        return await self._model_verb(
            session_id, "select_option",
            lambda page: model_browser.select_option(page, target, value),
        )

    async def _do_evaluate(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        """Run arbitrary JavaScript in the page and return its value.

        The honest escape hatch: reading a printed count, expanding a section,
        dismissing a custom overlay — anything the verb list does not spell out
        is still one console line away, exactly as it would be for the owner.
        """
        js = str(args.get("js") or args.get("script") or args.get("expression") or "")
        extra = args.get("args") if isinstance(args.get("args"), list) else []
        page = await self._working_page(session_id)
        try:
            value = await model_browser.evaluate(page, js, *extra)
        except model_browser.BrowserActionError as exc:
            raise ActionError(str(exc)) from exc
        try:
            rendered = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            rendered = str(value)
        return await self._after_model_action(
            session_id, page, f"JS returned: {rendered[:1200]}", "evaluate"
        )

    async def _do_wait_for(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        target = str(args.get("target") or args.get("selector") or args.get("text") or "")
        seconds = args.get("seconds")
        return await self._model_verb(
            session_id, "wait_for",
            lambda page: model_browser.wait_for(
                page, target, float(seconds) if seconds is not None else None
            ),
        )

    async def _do_go_back(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        return await self._model_verb(
            session_id, "go_back", lambda page: model_browser.go_back(page)
        )

    async def _do_go_forward(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        return await self._model_verb(
            session_id, "go_forward", lambda page: model_browser.go_forward(page)
        )

    async def _do_reload(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        return await self._model_verb(
            session_id, "reload", lambda page: model_browser.reload(page)
        )

    async def _do_screenshot(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        """Refresh the visual ground truth attached to the next model call."""
        page = await self._working_page(session_id)
        return await self._after_model_action(
            session_id, page,
            f"refreshed the screenshot of {scrub_url(str(getattr(page, 'url', '') or ''))}",
            "screenshot",
        )

    async def _do_browser(self, session_id: str, args: dict[str, Any]) -> ActionResult:
        """One generic escape hatch for the same verbs, by name.

        ``{"action": "browser", "args": {"op": "scroll", "direction": "down"}}``
        keeps working even for a verb added to this executor later, and lets a
        model that thinks in Playwright terms name the operation directly.
        """
        op = str(
            args.get("op") or args.get("operation") or args.get("verb") or ""
        ).strip().lower()
        if not op or op == "browser":
            raise ActionError(
                "browser requires 'op' — one of: navigate, click, type, press, "
                "scroll, hover, select_option, evaluate, wait_for, go_back, "
                "go_forward, reload, screenshot"
            )
        handler = getattr(self, f"_do_{op}", None)
        if handler is None:
            raise ActionError(f"unknown browser op {op!r}")
        return await handler(
            session_id, {k: v for k, v in args.items() if k not in ("op", "operation", "verb")}
        )

    # ------------------------------------------------------------------ helpers
    def _marketplace_for(self, args: dict[str, Any]) -> Marketplace:
        ident = str(args.get("marketplace") or "").strip()
        if ident:
            return get_marketplace(ident)
        return self.next_marketplace()


# --------------------------------------------------------------------- DOM
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
