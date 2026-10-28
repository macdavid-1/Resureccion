"""Final-candidate deep dive — measured backend keywords and store categories.

Runs AFTER the methodology has chosen the candidates that will reach the
report and BEFORE the report is written. For every surviving niche it does
two concrete, deterministic jobs with the real browser:

1. **Backend keywords (up to `max_keywords`, default 20).**
   Buyer phrases are mined from Amazon's own suggestion box (what shoppers
   actually type), then each shortlisted phrase is MEASURED with a real
   search in the Books department: Amazon's printed total-result line is the
   competition figure, and the median review count of the top 10 is the
   entry barrier. Nothing here is guessed — a phrase the run had no time to
   measure is recorded as `measured: False`, never as a number.

2. **Categories (up to `max_categories` per store, default 10, for BOTH).**
   The Kindle Store and the Books store are separate catalogues with
   separate category trees, so they are mapped separately: one search per
   department, then the top competitor product pages in that department are
   opened and their real breadcrumbs read. Books pages yield the Books tree,
   Kindle pages yield the Kindle tree.

Everything lands in `browser_evidence` with a stable id and flows through
the normal evidence -> claims -> filters -> gates pipeline, so the report and
the analyst model can only INTERPRET what was measured. The numbers are
computed here, deterministically; the model never calculates them.

Budgeted and pause-aware: a wall-clock budget, a query/page cap and a
`should_stop` callback (owner pause / cancel) bound the walk, and any failure
degrades to "fewer measured rows" — this must never fail a session.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus

from app.evidence_capture import (
    _extract_product,
    _extract_results_summary,
    _extract_search_items,
)
from app.frontier import ALIAS, phrase_score, seeds_from_text
from app.marketplace import Marketplace
from app.redact import scrub_url

# Amazon departments: Books and the Kindle Store are different catalogues.
DEPARTMENT_PARAM = {"books": "stripbooks", "kindle": "digital-text"}

# Competition buckets for a measured keyword (Amazon's total result count).
LOW_COMPETITION_MAX = 5_000
MEDIUM_COMPETITION_MAX = 25_000

# Letters appended to the most productive seed to surface long-tail buyer
# phrases ("space facts book for k…") — bounded, deterministic order.
_LETTERS = "abcdefghklnoprstw"

# Same-origin suggestion call inside the open marketplace page (Firefox
# blocks cross-origin fetch; this mirrors app.frontier).
_SUGGEST_JS = """async (q) => {
  try {
    const res = await fetch('/suggestion?alias=%s&prefix=' + encodeURIComponent(q),
                            {headers: {'accept': 'application/json'}});
    if (!res.ok) return [];
    const data = await res.json();
    return (Array.isArray(data) && Array.isArray(data[1])) ? data[1].slice(0, 10) : [];
  } catch (e) { return []; }
}""" % ALIAS


def competition_label(total: int | None) -> str:
    """Plain-language competition bucket for a measured result total."""
    if total is None:
        return "not measured"
    if total <= LOW_COMPETITION_MAX:
        return "low"
    if total <= MEDIUM_COMPETITION_MAX:
        return "medium"
    return "high"


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return int(s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2)


def _clean_phrase(raw: str) -> str:
    t = " ".join(str(raw or "").replace("\u00a0", " ").split())
    return t.strip(" ,.-–—:;|")


@dataclass
class DiveResult:
    """What the dive measured for ONE final candidate."""

    candidate_id: str = ""
    niche: str = ""
    marketplace: str = ""
    keywords: list[dict[str, Any]] = field(default_factory=list)
    books_categories: list[dict[str, Any]] = field(default_factory=list)
    kindle_categories: list[dict[str, Any]] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    phrases_mined: int = 0
    keywords_measured: int = 0
    queries_run: int = 0
    pages_opened: int = 0
    stopped_reason: str = "completed"
    elapsed_seconds: float = 0.0
    error: str | None = None

    @property
    def done(self) -> bool:
        return self.error is None and self.stopped_reason in (
            "completed", "budget_reached", "no_more_seeds",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "niche": self.niche,
            "marketplace": self.marketplace,
            "keywords": self.keywords,
            "books_categories": self.books_categories,
            "kindle_categories": self.kindle_categories,
            "evidence_ids": self.evidence_ids,
            "phrases_mined": self.phrases_mined,
            "keywords_measured": self.keywords_measured,
            "queries_run": self.queries_run,
            "pages_opened": self.pages_opened,
            "stopped_reason": self.stopped_reason,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "error": self.error,
        }


class FinalCandidateDive:
    """Deterministic per-candidate keyword + category research."""

    def __init__(
        self,
        browser: Any,
        evidence_store: Any,
        events: Any = None,
        *,
        max_keywords: int = 20,
        max_categories: int = 10,
        autocomplete_queries: int = 24,
        products_per_store: int = 6,
        time_budget_seconds: float = 300.0,
        should_stop: Any = None,
    ) -> None:
        self.browser = browser
        self.evidence_store = evidence_store
        self.events = events
        self.max_keywords = max(0, int(max_keywords))
        self.max_categories = max(0, int(max_categories))
        self.max_queries = max(0, int(autocomplete_queries))
        self.products_per_store = max(1, int(products_per_store))
        self.time_budget = max(10.0, float(time_budget_seconds))
        # Cooperative stop: owner pause / cancel yields within one step.
        self.should_stop = should_stop or (lambda: False)

    # ------------------------------------------------------------------ run
    async def run(
        self,
        *,
        session_id: str,
        marketplaces: list[Marketplace],
        niche: str,
        candidate_id: str = "",
    ) -> DiveResult:
        """Measure one candidate. Never raises."""
        res = DiveResult(candidate_id=candidate_id, niche=str(niche or "").strip())
        start = time.monotonic()
        deadline = start + self.time_budget
        mkt = next((m for m in (marketplaces or []) if m is not None), None)
        if mkt is None or not res.niche or self.max_keywords + self.max_categories == 0:
            res.stopped_reason = "no_budget_or_scope"
            res.elapsed_seconds = time.monotonic() - start
            return res
        res.marketplace = getattr(mkt, "code", "")
        try:
            phrases = await self._mine_phrases(
                session_id, mkt, res, deadline
            )
            if not (self._expired(deadline) or self.should_stop()):
                await self._measure_keywords(
                    session_id, mkt, res, phrases, deadline
                )
            if not (self._expired(deadline) or self.should_stop()):
                await self._map_categories(session_id, mkt, res, deadline)
        except Exception as exc:  # a dive failure is a finding, never a crash
            res.error = f"{type(exc).__name__}: {exc}"[:200]
            res.stopped_reason = "error"
            # The caller journals the failure (events + error state) — this
            # module only ever degrades, it never raises.
        res.elapsed_seconds = time.monotonic() - start
        return res

    def _expired(self, deadline: float) -> bool:
        return time.monotonic() > deadline

    # ------------------------------------------------------------- phrases
    async def _mine_phrases(
        self,
        session_id: str,
        mkt: Marketplace,
        res: DiveResult,
        deadline: float,
    ) -> list[dict[str, Any]]:
        """Mine buyer phrases from Amazon's suggestion box (one page)."""
        seeds = seeds_from_text(res.niche, limit=4)
        if not seeds:
            seeds = [w for w in res.niche.split() if len(w) > 3][:3]
        if not seeds:
            return []
        freq: dict[str, int] = {}
        page = None
        try:
            page = await self.browser.open_marketplace(mkt, "/")
            res.pages_opened += 1

            async def ask(prefix: str) -> None:
                nonlocal page
                if (
                    res.queries_run >= self.max_queries
                    or self._expired(deadline)
                    or self.should_stop()
                ):
                    return
                prefix = _clean_phrase(prefix).lower()
                if not prefix:
                    return
                res.queries_run += 1
                try:
                    out = await page.evaluate(_SUGGEST_JS, prefix)
                except Exception:
                    return  # one failed call never aborts the dive
                if not isinstance(out, list):
                    return
                for term in out:
                    t = _clean_phrase(term)
                    if not t:
                        continue
                    key = t.lower()
                    freq[key] = freq.get(key, 0) + 1

            # The niche itself, then each seed, then one letter frontier on
            # the most productive seed — bounded by max_queries.
            await ask(res.niche)
            for seed in seeds:
                if res.queries_run >= self.max_queries:
                    break
                await ask(seed)
            productive = sorted(
                seeds, key=lambda s: -sum(
                    1 for k in freq if k.startswith(s.lower() + " ")
                )
            )
            for seed in productive[:2]:
                for letter in _LETTERS:
                    if res.queries_run >= self.max_queries:
                        break
                    if self._expired(deadline) or self.should_stop():
                        break
                    await ask(f"{seed} {letter}")
        finally:
            if page is not None:
                try:
                    await self.browser.close_page(page)
                except Exception:
                    pass

        shortlist: list[dict[str, Any]] = []
        for key, count in freq.items():
            phrase = _clean_phrase(key)
            score = phrase_score(phrase, frequency=count)
            words = len(phrase.split())
            if score <= 0 or words < 2 or len(phrase) < 6 or len(phrase) > 70:
                continue
            shortlist.append({
                "keyword": phrase,
                "demand_score": score,
                "suggestion_hits": count,
            })
        # Buyer demand first (how often shoppers complete it), then alphabet
        # for determinism. Measurement trims to max_keywords below.
        shortlist.sort(key=lambda d: (-d["demand_score"], d["keyword"]))
        res.phrases_mined = len(shortlist)
        return shortlist

    # ---------------------------------------------------------- measurement
    async def _measure_keywords(
        self,
        session_id: str,
        mkt: Marketplace,
        res: DiveResult,
        phrases: list[dict[str, Any]],
        deadline: float,
    ) -> None:
        """Run a real Books-department search per shortlisted phrase."""
        if not phrases or self.max_keywords == 0:
            return
        target = phrases[: self.max_keywords * 2]  # oversample; budget trims
        measured: list[dict[str, Any]] = []
        unmeasured: list[dict[str, Any]] = []
        dept = DEPARTMENT_PARAM["books"]
        page = None
        try:
            for i, entry in enumerate(target):
                if len(measured) >= self.max_keywords:
                    break
                if self._expired(deadline) or self.should_stop():
                    unmeasured.extend(
                        {"keyword": p["keyword"], "measured": False,
                         "demand_score": p["demand_score"],
                         "suggestion_hits": p["suggestion_hits"]}
                        for p in target[i:]
                    )
                    break
                kw = entry["keyword"]
                path = f"/s?k={quote_plus(kw)}&i={dept}"
                try:
                    if page is None:
                        page = await self.browser.open_marketplace(mkt, path)
                        res.pages_opened += 1
                    else:
                        page = await self.browser.navigate(
                            page, f"{mkt.base_url}{path}"
                        )
                except Exception:
                    # A page that will not open is skipped, honestly marked.
                    unmeasured.append({**entry, "measured": False})
                    continue
                res.queries_run += 1
                summary = await _extract_results_summary(page)
                items = await _extract_search_items(page, 10)
                total = summary.get("total_results")
                reviews = [
                    int(r["review_count"]) for r in items
                    if r.get("review_count") is not None
                ]
                measured.append({
                    "keyword": kw,
                    "measured": True,
                    "department": "books",
                    "total_results": total,
                    "total_results_are_approximate": summary.get(
                        "total_results_are_approximate"
                    ),
                    "results_text": summary.get("results_text", ""),
                    "competition": competition_label(
                        total if isinstance(total, int) else None
                    ),
                    "top10_review_median": _median(reviews),
                    "captured_results": len(items),
                    "top_asins": [
                        str(r.get("asin") or "") for r in items[:3] if r.get("asin")
                    ],
                    "demand_score": entry["demand_score"],
                    "suggestion_hits": entry["suggestion_hits"],
                    "marketplace": mkt.code,
                })
        finally:
            if page is not None:
                try:
                    await self.browser.close_page(page)
                except Exception:
                    pass

        # Measured first: buyer demand desc, then lightest competition, then
        # the honest unmeasured tail (never a fabricated number).
        measured.sort(key=lambda d: (
            -int(d.get("demand_score") or 0),
            d.get("total_results") if isinstance(d.get("total_results"), int) else 10**12,
            d["keyword"].lower(),
        ))
        unmeasured.sort(key=lambda d: (-int(d.get("demand_score") or 0), d["keyword"].lower()))
        res.keywords = measured[: self.max_keywords] + unmeasured[
            : max(0, self.max_keywords - len(measured))
        ]
        res.keywords_measured = min(len(measured), self.max_keywords)
        if self._expired(deadline):
            res.stopped_reason = "budget_reached"

        try:
            rec = self.evidence_store.create(
                session_id=session_id,
                marketplace=mkt.code,
                kind="keyword_research",
                url=scrub_url(f"{mkt.base_url}/s?k={quote_plus(res.niche)}&i={dept}"),
                title=f"backend keywords measured for {res.niche[:120]}",
                data={
                    "niche": res.niche,
                    "department": "books",
                    "requested": self.max_keywords,
                    "measured": res.keywords_measured,
                    "phrases_mined": res.phrases_mined,
                    "ranked_by": (
                        "buyer demand first, then lightest competition "
                        "(Amazon's own total-result count)"
                    ),
                    "keywords": res.keywords,
                },
            )
            res.evidence_ids.append(str(rec.id))
        except Exception:
            pass  # measurement already happened; persistence is additive

    # ------------------------------------------------------------ categories
    async def _map_categories(
        self,
        session_id: str,
        mkt: Marketplace,
        res: DiveResult,
        deadline: float,
    ) -> None:
        """Map Books and Kindle Store categories separately (they differ)."""
        books: list[dict[str, Any]] = []
        kindle: list[dict[str, Any]] = []
        sources: list[str] = []
        for store in ("books", "kindle"):
            if self._expired(deadline) or self.should_stop():
                break
            if self.max_categories == 0:
                break
            rows, urls = await self._categories_for_store(
                mkt, res, store, deadline
            )
            if store == "books":
                books = rows
            else:
                kindle = rows
            sources.extend(urls)
        res.books_categories = books
        res.kindle_categories = kindle
        if self._expired(deadline):
            res.stopped_reason = "budget_reached"
        if not books and not kindle:
            return
        try:
            rec = self.evidence_store.create(
                session_id=session_id,
                marketplace=mkt.code,
                kind="category_research",
                url=scrub_url(f"{mkt.base_url}/s?k={quote_plus(res.niche)}"),
                title=f"store categories mapped for {res.niche[:120]}",
                data={
                    "niche": res.niche,
                    "books": books,
                    "kindle_store": kindle,
                    "note": (
                        "Kindle Store and Books keep separate category trees; "
                        "both were mapped from real product breadcrumbs in "
                        "their own department"
                    ),
                    "source_urls": sources[:12],
                },
            )
            res.evidence_ids.append(str(rec.id))
        except Exception:
            pass

    async def _categories_for_store(
        self,
        mkt: Marketplace,
        res: DiveResult,
        store: str,
        deadline: float,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Up to `max_categories` real categories for one Amazon store."""
        dept = DEPARTMENT_PARAM[store]
        path = f"/s?k={quote_plus(res.niche)}&i={dept}"
        page: Any = None
        rows: list[dict[str, Any]] = []
        urls: list[str] = []
        seen: set[str] = set()

        def add(breadcrumb: list[str], asin: str, url: str, fallback: str) -> None:
            leaf = ""
            if breadcrumb:
                leaf = str(breadcrumb[-1]).strip()
            leaf = leaf or str(fallback or "").strip()
            if not leaf:
                return
            key = " ".join(leaf.lower().split())
            if key in seen or len(rows) >= self.max_categories:
                return
            seen.add(key)
            rows.append({
                "store": store,
                "category": leaf[:120],
                "breadcrumb": " > ".join(str(c) for c in breadcrumb[:8]),
                "asin": asin,
                "source_url": url,
            })

        try:
            page = await self.browser.open_marketplace(mkt, path)
            res.pages_opened += 1
            items = await _extract_search_items(page, 20)
            asins = [str(i.get("asin") or "") for i in items if i.get("asin")]
            urls.append(scrub_url(getattr(page, "url", "") or ""))
            for asin in asins[: self.products_per_store]:
                if self._expired(deadline) or self.should_stop():
                    break
                if len(rows) >= self.max_categories:
                    break
                try:
                    page = await self.browser.navigate(
                        page, f"{mkt.base_url}/dp/{asin}"
                    )
                except Exception:
                    continue
                res.pages_opened += 1
                prod = await _extract_product(page)
                crumbs = [str(c).strip() for c in (prod.get("category_path") or [])]
                add(
                    crumbs,
                    str(prod.get("asin") or asin),
                    scrub_url(getattr(page, "url", "") or ""),
                    str(prod.get("best_seller_category") or ""),
                )
        except Exception:
            pass
        finally:
            if page is not None:
                try:
                    await self.browser.close_page(page)
                except Exception:
                    pass
        return rows, urls
