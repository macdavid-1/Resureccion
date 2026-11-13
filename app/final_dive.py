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
    _extract_review_snippets,
    _extract_search_items,
)
from app.frontier import (
    ALIAS,
    SUGGEST_JS,
    mid_for,
    phrase_score,
    screen_phrase,
    seeds_from_text,
    topical_tokens,
)
from app.marketplace import Marketplace
from app.redact import scrub_url

# Amazon departments: Books and the Kindle Store are different catalogues.
DEPARTMENT_PARAM = {"books": "stripbooks", "kindle": "digital-text"}

# Breadcrumb levels that are store roots, not choosable KDP categories.
CATEGORY_ROOTS = frozenset({
    "books", "kindle store", "kindle ebooks", "kindle books", "kindle",
    "audible audiobooks", "audible", "amazon", "everything else",
})

# Competition buckets for a measured keyword (Amazon's total result count).
LOW_COMPETITION_MAX = 5_000
MEDIUM_COMPETITION_MAX = 25_000
# Above this a phrase is REJECTED rather than merely ranked last: a head term
# with tens of thousands of results (let alone millions) is not a niche keyword
# a new book can rank for, and presenting it as "researched" is a lie.
DEFAULT_MAX_COMPETITION = MEDIUM_COMPETITION_MAX

# Real customer reviews read per opened competitor product page (the page is
# already open for its category breadcrumbs, so this costs no navigation).
REVIEWS_PER_PRODUCT = 4
MAX_REVIEW_SNIPPETS = 24

# Letters appended to the most productive seed to surface long-tail buyer
# phrases ("space facts book for k…") — bounded, deterministic order.
_LETTERS = "abcdefghklnoprstw"

# Same-origin suggestion call inside the open marketplace page (Firefox
# blocks cross-origin fetch; the endpoint list lives in app.frontier, because
# Amazon RETIRED the legacy /suggestion path — pointing the dive at it mined
# zero phrases on every run, which is why the keyword section was always empty).
_SUGGEST_JS = SUGGEST_JS


def competition_label(total: int | None) -> str:
    """Plain-language competition bucket for a measured result total."""
    if total is None:
        return "not measured"
    if total <= LOW_COMPETITION_MAX:
        return "low"
    if total <= MEDIUM_COMPETITION_MAX:
        return "medium"
    return "high"


def competition_rank(total: int | None) -> int:
    """Sort key: lowest competition first, unmeasured last.

    The owner asks for keywords that can actually rank, so competition leads
    the ordering and buyer demand breaks ties — a phrase nobody searches is
    winnable but worthless.
    """
    label = competition_label(total)
    return {"low": 0, "medium": 1, "high": 2}.get(label, 3)


def order_for_marketplace(
    marketplaces: list[Marketplace], code: str
) -> list[Marketplace]:
    """Put the candidate's OWN marketplace first, keeping the rest in order.

    A candidate discovered on Amazon UK must be measured on Amazon UK: the
    keyword totals, review barriers and category trees are marketplace
    specific, and the report tells the owner which marketplace the numbers
    came from.
    """
    wanted = [c.strip() for c in str(code or "").split(",") if c.strip()]
    own = [m for m in (marketplaces or []) if m.code in wanted]
    rest = [m for m in (marketplaces or []) if m.code not in wanted]
    return own + rest


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
    # Real customer reviews read off the competitor product pages opened for
    # the Books category map — the raw material for "beat the incumbents".
    reviews: list[dict[str, Any]] = field(default_factory=list)
    review_evidence_id: str = ""
    review_products_read: int = 0
    evidence_ids: list[str] = field(default_factory=list)
    phrases_mined: int = 0
    # Phrases built from the niche because the completion stream gave nothing.
    phrases_derived: int = 0
    keywords_measured: int = 0
    # Phrases that were screened out before measuring (`stage: "screened"`) or
    # measured and then refused (`stage: "measured"`), each with its reason and
    # its real result total when one was read. This is the audit trail that
    # shows the dive FILTERED instead of pouring every suggestion in.
    rejected: list[dict[str, Any]] = field(default_factory=list)
    # Screened phrases with no search yet (budget ran out).
    unmeasured: list[dict[str, Any]] = field(default_factory=list)
    competition_ceiling: int = 0
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
            "reviews": self.reviews,
            "review_evidence_id": self.review_evidence_id,
            "review_products_read": self.review_products_read,
            "evidence_ids": self.evidence_ids,
            "phrases_mined": self.phrases_mined,
            "phrases_derived": self.phrases_derived,
            "keywords_measured": self.keywords_measured,
            "keywords_rejected": len(self.rejected),
            "rejected": self.rejected[:40],
            "unmeasured": self.unmeasured[:20],
            "competition_ceiling": self.competition_ceiling,
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
        max_competition: int = DEFAULT_MAX_COMPETITION,
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
        # 0 disables the ceiling (every measured phrase is then eligible).
        self.max_competition = max(0, int(max_competition))
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
        res.competition_ceiling = self.max_competition
        attempted: set[str] = set()
        try:
            phrases = await self._mine_phrases(
                session_id, mkt, res, deadline
            )
            if not phrases:
                # Amazon's completion stream can be unavailable (retired
                # endpoint, bot wall, a marketplace with no suggestion data).
                # Falling through with an empty list is what left the report's
                # keyword section and its accounting permanently empty: the
                # niche's OWN phrases are measurable too, so they are searched
                # for real and labelled honestly.
                phrases = self._phrases_from_niche(res.niche)
                res.phrases_derived = len(phrases)
            if not (self._expired(deadline) or self.should_stop()):
                await self._measure_keywords(
                    session_id, mkt, res, phrases, deadline, attempted=attempted
                )
            # ---- deepening pass: mine FROM the phrases that measured well --
            # The first sweep is anchored on the candidate's own niche words;
            # buyers complete those into phrasings the niche label never
            # contains. One bounded second sweep, seeded by the accepted
            # keywords, is what makes the keyword set deep instead of shallow.
            if (
                len(res.keywords) < self.max_keywords
                and not (self._expired(deadline) or self.should_stop())
            ):
                seeds = [
                    str(k.get("keyword") or "")
                    for k in res.keywords[:4]
                    if k.get("measured") and str(k.get("keyword") or "")
                ]
                if seeds:
                    deeper = await self._mine_phrases(
                        session_id, mkt, res, deadline, seeds=seeds
                    )
                    deeper = [
                        d for d in deeper
                        if d["keyword"].lower() not in attempted
                    ]
                    if deeper:
                        await self._measure_keywords(
                            session_id, mkt, res, deeper, deadline,
                            attempted=attempted,
                        )
            if not (self._expired(deadline) or self.should_stop()):
                await self._map_categories(session_id, mkt, res, deadline)
            self._finalize_keywords(res)
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
        *,
        seeds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Mine buyer phrases from Amazon's suggestion box (one page).

        `seeds` lets the deepening pass re-mine from the phrases that already
        measured well, instead of asking the same niche prefixes again.
        """
        from_niche = seeds is None
        if from_niche:
            seeds = seeds_from_text(res.niche, limit=4)
            if not seeds:
                seeds = [w for w in res.niche.split() if len(w) > 3][:3]
        else:
            seeds = [s for s in (_clean_phrase(x).lower() for x in seeds or []) if s][:4]
        if not seeds:
            return []
        freq: dict[str, int] = {}
        page = None
        try:
            page = await self.browser.open_marketplace(mkt, "/")
            res.pages_opened += 1

            # The first sweep leaves a slice of the query budget for the
            # deepening sweep seeded by the accepted keywords: spending every
            # query on the alphabet leaves the second (deeper) pass with
            # nothing, which is how a dive ends up shallow.
            limit = self.max_queries
            if from_niche:
                limit = max(4, int(self.max_queries * 0.7))

            async def ask(prefix: str) -> None:
                nonlocal page
                if (
                    res.queries_run >= limit
                    or self._expired(deadline)
                    or self.should_stop()
                ):
                    return
                prefix = _clean_phrase(prefix).lower()
                if not prefix:
                    return
                res.queries_run += 1
                try:
                    out = await page.evaluate(
                        _SUGGEST_JS,
                        [ALIAS, mid_for(getattr(mkt, "domain", "")), prefix],
                    )
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
            if from_niche:
                await ask(res.niche)
            for seed in seeds:
                if res.queries_run >= limit:
                    break
                await ask(seed)
            productive = sorted(
                seeds, key=lambda s: -sum(
                    1 for k in freq if k.startswith(s.lower() + " ")
                )
            )
            for seed in productive[:2]:
                for letter in _LETTERS:
                    if res.queries_run >= limit:
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

        # Screen BEFORE any search is spent: roots ("journal for women"), noise
        # and phrases that have drifted away from this candidate are counted
        # and dropped, never researched and never presented as keywords.
        topics = topical_tokens(res.niche)
        for seed in seeds:
            topics |= topical_tokens(seed)
        shortlist: list[dict[str, Any]] = []
        for key, count in freq.items():
            phrase = _clean_phrase(key)
            score = phrase_score(phrase, frequency=count)
            words = len(phrase.split())
            if score <= 0 or words < 2 or len(phrase) < 6 or len(phrase) > 70:
                if phrase:
                    self._reject(res, phrase, "too_broad" if words < 2 else "noise")
                continue
            ok, reason = screen_phrase(phrase, topics)
            if not ok:
                self._reject(res, phrase, reason)
                continue
            shortlist.append({
                "keyword": phrase,
                "demand_score": score,
                "suggestion_hits": count,
                "source": "autocomplete",
            })
        # Buyer demand first (how often shoppers complete it), then alphabet
        # for determinism. Measurement trims to max_keywords below.
        shortlist.sort(key=lambda d: (-d["demand_score"], d["keyword"]))
        res.phrases_mined += len(shortlist)
        return shortlist

    @staticmethod
    def _reject(res: DiveResult, phrase: str, reason: str, **extra: Any) -> None:
        """Journal one dropped phrase (bounded; the report shows the reasons)."""
        if len(res.rejected) >= 120:
            return
        res.rejected.append({
            "keyword": phrase[:120],
            "reason": reason,
            **extra,
        })

    @staticmethod
    def _phrases_from_niche(niche: str) -> list[dict[str, Any]]:
        """Deterministic phrases built from the niche itself.

        Used when the suggestion box yields nothing. Each phrase is still
        MEASURED with a real Books-department search — a measured result total
        and review barrier is useful even when Amazon would not complete the
        query — and is marked ``source: "niche"`` so the report never implies
        buyers typed it.
        """
        text = _clean_phrase(niche).lower()
        if not text:
            return []
        words = text.split()
        candidates: list[str] = [text]
        head = text.split(" for ")[0].strip()
        if head and head != text:
            candidates.append(head)
            audience = text.split(" for ", 1)[1].strip()
            if audience:
                candidates.append(f"{head} for {audience.split()[0]}")
        for n in (2, 3):
            trimmed = " ".join(words[:n])
            if trimmed and trimmed not in candidates:
                candidates.append(trimmed)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        topics = topical_tokens(text)
        for phrase in candidates:
            phrase = _clean_phrase(phrase)
            key = phrase.lower()
            if not phrase or key in seen or len(phrase) < 6 or len(phrase.split()) < 2:
                continue
            ok, _reason = screen_phrase(phrase, topics)
            if not ok:
                # Derived phrases are held to the same bar as mined ones: a
                # dangling "... for" is not a keyword, it is a cut-off query.
                continue
            seen.add(key)
            out.append({
                "keyword": phrase,
                "demand_score": phrase_score(phrase, frequency=1),
                "suggestion_hits": 0,
                "source": "niche",
            })
        return out

    # ---------------------------------------------------------- measurement
    def _competition_reason(self, total: Any) -> str:
        """Why a measured phrase is refused, or '' when it is eligible."""
        if (
            self.max_competition > 0
            and isinstance(total, int)
            and total > self.max_competition
        ):
            return "too_much_competition"
        return ""

    async def _measure_keywords(
        self,
        session_id: str,
        mkt: Marketplace,
        res: DiveResult,
        phrases: list[dict[str, Any]],
        deadline: float,
        *,
        attempted: set[str] | None = None,
    ) -> None:
        """Verify phrases ONE BY ONE and keep only the ones worth a book.

        Each screened phrase gets a real Books-department search; the measured
        total is the accept/reject decision (above the competition ceiling the
        phrase is refused with its number attached, not silently ranked last).
        The loop keeps walking the ranked pool until `max_keywords` phrases are
        ACCEPTED or the pool/budget runs out — so the report gets a verified
        set instead of the first N suggestions that happened to come back.
        """
        if not phrases or self.max_keywords == 0:
            return
        attempted = attempted if attempted is not None else set()
        pool = phrases[: self.max_keywords * 3]  # oversample; rejects trim
        unmeasured: list[dict[str, Any]] = []
        dept = DEPARTMENT_PARAM["books"]
        page = None
        try:
            for i, entry in enumerate(pool):
                if len(res.keywords) >= self.max_keywords:
                    break
                kw = str(entry.get("keyword") or "")
                if not kw or kw.lower() in attempted:
                    continue
                if self._expired(deadline) or self.should_stop():
                    unmeasured.extend(
                        {"keyword": p["keyword"], "measured": False,
                         "demand_score": p["demand_score"],
                         "suggestion_hits": p["suggestion_hits"],
                         "source": p.get("source") or "autocomplete"}
                        for p in pool[i:]
                        if p["keyword"].lower() not in attempted
                    )
                    break
                attempted.add(kw.lower())
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
                row: dict[str, Any] = {
                    "keyword": kw,
                    "measured": True,
                    "source": entry.get("source") or "autocomplete",
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
                }
                refused = self._competition_reason(total)
                if refused:
                    self._reject(
                        res, kw, refused, stage="measured",
                        total_results=total,
                        competition=row["competition"],
                        marketplace=mkt.code,
                    )
                    continue
                res.keywords.append(row)
        finally:
            if page is not None:
                try:
                    await self.browser.close_page(page)
                except Exception:
                    pass

        res.unmeasured.extend(unmeasured)
        res.keywords_measured = len(res.keywords)
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
                    "competition_ceiling": self.max_competition,
                    "rejected": res.rejected[:40],
                    "unmeasured": res.unmeasured[:20],
                    "ranked_by": (
                        "screened for specificity, then verified one by one "
                        "with a real search; competition must stay under "
                        f"{self.max_competition} results; lightest first"
                    ),
                    "keywords": res.keywords,
                },
            )
            res.evidence_ids.append(str(rec.id))
        except Exception:
            pass  # measurement already happened; persistence is additive

    def _finalize_keywords(self, res: DiveResult) -> None:
        """Order the accepted set and attach the honest unmeasured tail."""
        res.keywords.sort(key=lambda d: (
            competition_rank(
                d.get("total_results") if isinstance(d.get("total_results"), int) else None
            ),
            -int(d.get("demand_score") or 0),
            d.get("total_results") if isinstance(d.get("total_results"), int) else 10**12,
            d["keyword"].lower(),
        ))
        res.unmeasured.sort(
            key=lambda d: (-int(d.get("demand_score") or 0), d["keyword"].lower())
        )
        room = max(0, self.max_keywords - len(res.keywords))
        if room and res.unmeasured:
            res.keywords.extend(
                {"keyword": d["keyword"], "measured": False,
                 "demand_score": d.get("demand_score", 0),
                 "suggestion_hits": d.get("suggestion_hits", 0),
                 "source": d.get("source") or "autocomplete"}
                for d in res.unmeasured[:room]
            )
        res.keywords_measured = sum(1 for k in res.keywords if k.get("measured"))

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
        # Reviews are read from the SAME Books product pages the category map
        # already opens, so the instruction "beat these books, from reviews"
        # rests on live text without costing extra navigations.
        review_state: dict[str, Any] = {"snippets": [], "products": 0}
        for store in ("books", "kindle"):
            if self._expired(deadline) or self.should_stop():
                break
            if self.max_categories == 0:
                break
            rows, urls = await self._categories_for_store(
                mkt, res, store, deadline,
                review_state=review_state if store == "books" else None,
            )
            if store == "books":
                books = rows
            else:
                kindle = rows
            sources.extend(urls)
        res.books_categories = books
        res.kindle_categories = kindle
        res.reviews = review_state["snippets"]
        res.review_products_read = int(review_state["products"])
        if self._expired(deadline):
            res.stopped_reason = "budget_reached"
        if res.reviews:
            try:
                rec = self.evidence_store.create(
                    session_id=session_id,
                    marketplace=mkt.code,
                    kind="review_research",
                    url=scrub_url(f"{mkt.base_url}/s?k={quote_plus(res.niche)}&i={DEPARTMENT_PARAM['books']}"),
                    title=f"competitor reviews read for {res.niche[:120]}",
                    data={
                        "niche": res.niche,
                        "products_read": res.review_products_read,
                        "reviews": res.reviews,
                        "ranked_by": (
                            "reviews of the top Books-store competitors that "
                            "rank for this niche, read live"
                        ),
                        "note": (
                            "Verbatim customer review text captured from the "
                            "pages the category map opened; complaints and "
                            "praises here are quoted, not inferred"
                        ),
                    },
                )
                res.review_evidence_id = str(rec.id)
                res.evidence_ids.append(str(rec.id))
            except Exception:
                pass  # reviews are already in hand; persistence is additive
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
        review_state: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Up to `max_categories` real categories for one Amazon store."""
        dept = DEPARTMENT_PARAM[store]
        path = f"/s?k={quote_plus(res.niche)}&i={dept}"
        page: Any = None
        rows: list[dict[str, Any]] = []
        urls: list[str] = []
        seen: set[str] = set()

        def add(breadcrumb: list[str], asin: str, url: str, fallback: str) -> None:
            """Record the product's real category path — leaf AND its ancestors.

            A KDP book is filed under a leaf deeper in the tree, but the
            ancestors are real, choosable categories too. Recording only the
            leaf could never fill `max_categories`: one product yields one
            leaf, so a 10-category target stalled at `products_per_store`
            rows and the report's accounting said "3 of 10 ... the rest were
            not read" on every run.
            """
            crumbs = [str(c).strip() for c in (breadcrumb or []) if str(c).strip()]
            levels: list[str] = list(reversed(crumbs))
            if not levels:
                only = str(fallback or "").strip()
                levels = [only] if only else []
            for depth, name in enumerate(levels):
                if not name or len(rows) >= self.max_categories:
                    return
                key = " ".join(name.lower().split())
                if key in seen or key in CATEGORY_ROOTS:
                    continue
                seen.add(key)
                rows.append({
                    "store": store,
                    "category": name[:120],
                    "breadcrumb": " > ".join(crumbs[:8]) or name[:120],
                    "depth": depth,
                    "asin": asin,
                    "source_url": url,
                })

        try:
            page = await self.browser.open_marketplace(mkt, path)
            res.pages_opened += 1
            items = await _extract_search_items(page, 20)
            asins = [str(i.get("asin") or "") for i in items if i.get("asin")]
            urls.append(scrub_url(getattr(page, "url", "") or ""))
            # Products are how categories are found, so the walk is bounded by
            # the CATEGORY target as well: `products_per_store` alone could
            # never reach `max_categories` (and duplicates reduce it further).
            product_cap = max(self.products_per_store, self.max_categories)
            for asin in asins[:product_cap]:
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
                if review_state is not None:
                    review_state["products"] = int(review_state.get("products", 0)) + 1
                    for snip in await _extract_review_snippets(
                        page, REVIEWS_PER_PRODUCT
                    ):
                        if len(review_state["snippets"]) >= MAX_REVIEW_SNIPPETS:
                            break
                        snip = {**snip, "asin": snip.get("asin") or prod.get("asin") or asin}
                        review_state["snippets"].append(snip)
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
