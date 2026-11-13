"""The autocomplete frontier — a deterministic demand-language miner.

Amazon's search box completes with what buyers ACTUALLY type. A long-tail
phrase that completes readily and returns a thin result set is hidden demand
sitting in plain sight. This module mines it as backend code, not as a model
decision:

    seed ──► "seed"            (what the box completes the seed itself to)
          ──► "seed a" … "seed z"   (the next-word frontier)
          ──► deepen the most promising completions (recursive, budgeted)

Design properties (why it is shaped this way):

- **No page load per query.** One page per marketplace, then many same-origin
  `fetch('/suggestion?alias=…')` calls inside that page — gentle on a 2-core
  box and on the owner's egress path.
- **Deterministic and budgeted.** Query count, recursion depth and a wall
  clock budget are fixed by config; the routine stops on budget, deadline or
  error and reports why. It never blocks research: any failure degrades to
  "fewer terms", never to a crash.
- **Ordinary evidence.** Every batch lands in the `browser_evidence` table
  with a stable id, through `redact()`, so the terms flow through the normal
  evidence → claims → filters → gates pipeline with no new plumbing.
- **No login required.** Suggestions are a public endpoint; sign-in state is
  irrelevant here.
"""
from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.marketplace import Marketplace
from app.redact import redact, scrub_url

# Amazon's books alias (the KDP-relevant completion stream).
ALIAS = "stripbooks"

# Marketplace ids the completion API needs. Amazon RETIRED the legacy
# `/suggestion` path — it now answers 404 on amazon.com while the storefront's
# own search box calls `/api/2017/suggestions` — so the legacy path is only a
# fallback for a marketplace that still serves it. Without this, autocomplete
# (discovery AND the finalists' backend keywords) silently mined NOTHING.
MARKETPLACE_IDS: dict[str, str] = {
    "amazon.com": "ATVPDKIKX0DER",
    "amazon.co.uk": "A1F83G8C2ARO7P",
    "amazon.de": "A1PA6795UKMFR9",
    "amazon.fr": "A13V1IB3VIYZZH",
    "amazon.it": "APJ6JRA9NG5V4",
    "amazon.es": "A1RKKUPIHCS9HS",
    "amazon.ca": "A2EUQ1WTGCTBG2",
    "amazon.com.au": "A39IBJ37TRP1C6",
    "amazon.co.jp": "A1VC38T7YXB528",
    "amazon.in": "A21TJRUUN4KGV",
    "amazon.com.mx": "A1AM78C64UM0Y8",
    "amazon.com.br": "A2Q3Y263D00KWC",
    "amazon.nl": "A1805IZSGTT6HS",
    "amazon.se": "A2NODRKZP88ZB9",
    "amazon.pl": "A1C3SOZRARQ6R3",
    "amazon.com.tr": "A33AVAJ2PDY3EV",
    "amazon.ae": "A2VIGQ35RCS4UG",
    "amazon.sa": "A17E79C6D8DWNP",
    "amazon.sg": "A19VAU5U5O7RUS",
    "amazon.eg": "ARBP9OOSHTCHU",
    "amazon.com.be": "AMEN7PMS3EDWL",
}
DEFAULT_MARKETPLACE_ID = "ATVPDKIKX0DER"


def mid_for(domain: str) -> str:
    """Marketplace id for a domain (US when the marketplace is unknown)."""
    d = str(domain or "").strip().lower().lstrip(".")
    if d in MARKETPLACE_IDS:
        return MARKETPLACE_IDS[d]
    for known, mid in MARKETPLACE_IDS.items():
        if d.endswith("." + known):
            return mid
    return DEFAULT_MARKETPLACE_ID


# Same-origin completion call, executed inside the already-open marketplace
# page (Firefox blocks cross-origin fetch; this is same-domain). The modern
# endpoint is tried FIRST and the retired one second, so a marketplace that
# still serves `/suggestion` keeps working. Both response shapes are read
# (`["", [..]]` legacy, `{"suggestions": [{"value": ..}]}` current).
SUGGEST_JS = """async ([alias, mid, prefix]) => {
  const paths = [];
  if (mid) {
    paths.push('/api/2017/suggestions?limit=11&prefix=' + encodeURIComponent(prefix)
      + '&suggestion-type=WIDGET&alias=' + encodeURIComponent(alias)
      + '&mid=' + encodeURIComponent(mid)
      + '&lop=en_US&site-variant=desktop&client=amazon-search-ui&fb=1&plain-mid=1');
  }
  paths.push('/suggestion?alias=' + encodeURIComponent(alias)
    + '&prefix=' + encodeURIComponent(prefix)
    + (mid ? '&mid=' + encodeURIComponent(mid) : ''));
  for (const path of paths) {
    try {
      const res = await fetch(path, {headers: {'accept': 'application/json'}});
      if (!res.ok) continue;
      const data = await res.json();
      if (Array.isArray(data) && Array.isArray(data[1])) {
        return data[1].filter((t) => typeof t === 'string').slice(0, 11);
      }
      if (data && Array.isArray(data.suggestions)) {
        return data.suggestions
          .map((s) => (s && typeof s === 'object' ? s.value : s))
          .filter((t) => typeof t === 'string' && t.trim())
          .slice(0, 11);
      }
    } catch (e) { /* try the next endpoint */ }
  }
  return [];
}"""

# Backwards-compatible alias for callers that only need the script.
_SUGGEST_JS = SUGGEST_JS

# Fallback seeds for a run with NO topic (mode 'auto'): book-format roots.
# They are deliberately generic — the point is to harvest the long tail that
# hangs off format words, not to assert a market.
FALLBACK_SEEDS: tuple[str, ...] = (
    "journal", "workbook", "log", "planner", "guide", "handbook",
    "checklist", "worksheet", "tracker", "worksheet for",
)

_STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "how", "i", "in", "is", "it", "its", "of", "on", "or", "our", "that", "the", "their", "to", "was", "we", "what", "when", "which", "who", "will", "with", "you", "your", "about", "into", "over", "under", "best", "new", "free", "book", "books", "kindle", "amazon"]
)

# Phrases that are noise rather than demand language.
_NOISE = (
    "amazon.com", "amazon ", " books by ", "paperback", "hardcover",
    "kindle edition", " AUD", " mp3 ",
)

# Words that name the FORMAT, not the subject. A phrase built only from these
# ("journal for women", "self help book", "kids activity book") is a category
# root, not hidden demand: it competes with the whole store and describes no
# reader. Suggestions are stripped of them before anything is researched, so
# the model starts from specific demand language instead of a pile of roots.
GENERIC_TOKENS = frozenset(
    ["book", "books", "ebook", "ebooks", "workbook", "workbooks", "journal", "journals", "notebook", "notebooks", "planner", "planners", "log", "logs", "diary", "diaries", "guide", "guides", "handbook", "handbooks", "manual", "manuals", "checklist", "checklists", "tracker", "trackers", "worksheet", "worksheets", "coloring", "colouring", "activity", "activities", "gift", "gifts", "idea", "ideas", "best", "top", "new", "free", "amazon", "kindle", "paperback", "hardcover", "audiobook", "audible", "pdf", "epub", "volume", "vol", "edition", "series", "set", "bundle", "box", "kit", "pack", "printable", "downloads", "download", "ebook-only", "self", "help", "how", "to"]
)

# Whole-phrase filler: audience/format with no subject of its own.
TRAILING_FILLER = frozenset(
    ["for", "with", "and", "the", "a", "an", "of", "to", "in", "on", "or", "by", "from", "at", "your", "my", "our", "is", "are"]
)

GENERIC_FILLER: tuple[str, ...] = (
    "for women", "for men", "for kids", "for adults", "for beginners",
    "for teens", "for children", "for seniors", "for girls", "for boys",
    "for dummies", "in 30 days", "step by step", "large print", "all ages",
)


def topical_tokens(text: str, *, min_len: int = 3) -> set[str]:
    """The SUBJECT words of a phrase — format/audience filler removed."""
    out: set[str] = set()
    for raw in str(text or "").lower().replace("-", " ").replace("/", " ").split():
        w = "".join(ch for ch in raw if ch.isalnum())
        if len(w) < min_len or w in _STOPWORDS or w in GENERIC_TOKENS:
            continue
        out.add(w)
    return out


def screen_phrase(phrase: str, topic_tokens: set[str]) -> tuple[bool, str]:
    """Deterministic accept/reject for one demand phrase, with the reason.

    The suggestion stream happily returns roots ("book for women") and drift
    ("therapy worksheets for ptsd" for a grief niche). Researching those wastes
    searches and, worse, presents unwinnable generic terms as validated. This
    is the same screen the finalist dive applies before it measures a phrase,
    so discovery and the report agree on what a usable keyword is.
    """
    import re

    t = " ".join(str(phrase or "").lower().split())
    if not t:
        return False, "empty"
    if any(n.lower() in t for n in _NOISE):
        return False, "noise"
    if re.search(r"https?://|\bwww\.|\d{4,}", t):
        return False, "noise"
    words = t.split()
    if len(words) < 2:
        return False, "too_broad"
    if len(words) > 12 or len(t) > 70:
        return False, "too_abstract"
    # A truncated prefix ("grief journals for") is not demand language; it is
    # what an unfinished query looks like. Only FUNCTION words count here: a
    # phrase may legitimately end in a format word ("grief journal workbook").
    if words[-1] in TRAILING_FILLER:
        return False, "incomplete"
    topical = topical_tokens(t)
    if not topical:
        return False, "too_generic"
    filler = any(f in t for f in GENERIC_FILLER)
    if filler and len(topical) < 2:
        return False, "too_generic"
    if topic_tokens and not (topical & topic_tokens):
        return False, "off_niche"
    return True, ""


def filter_demand_terms(
    terms: Iterable[str],
    seeds: Iterable[str],
    *,
    limit: int = 60,
) -> tuple[list[str], dict[str, int]]:
    """Keep the demand phrases worth researching; count why the rest went.

    Returns (kept, rejected-by-reason). The kept list is what the model sees
    as STARTING POINTS — never as its research boundary.
    """
    topics = set()
    for seed in seeds:
        topics |= topical_tokens(seed)
    kept: list[str] = []
    seen: set[str] = set()
    rejected: dict[str, int] = {}
    for term in terms:
        key = " ".join(str(term or "").lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        ok, reason = screen_phrase(key, topics)
        if not ok:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        kept.append(key)
        if len(kept) >= limit:
            break
    return kept, rejected


def seeds_from_text(text: str, *, limit: int = 6) -> list[str]:
    """Deterministic seed extraction from the owner's brief/objective."""
    words: list[str] = []
    seen: set[str] = set()
    for raw in str(text or "").lower().replace("?", " ").replace(",", " ").split():
        w = "".join(ch for ch in raw if ch.isalnum() or ch in ("-", "'"))
        if len(w) < 4 or w in _STOPWORDS or w in seen:
            continue
        seen.add(w)
        words.append(w)
        if len(words) >= limit:
            break
    return words


def phrase_score(term: str, *, frequency: int = 1) -> int:
    """Deterministic 'how promising is this demand phrase' ranking.

    Longer, repeated completions are the strongest signal: a phrase that
    completes out of MANY different prefixes is language buyers really type.
    """
    t = " ".join(str(term).lower().split())
    if not t or any(n.lower() in t for n in _NOISE):
        return -1
    words = len(t.split())
    if words < 2:
        return 0
    score = min(words, 6) * 10 + min(frequency, 20) * 4
    if words >= 3:
        score += 15
    if len(t) > 60:
        score -= 20
    return score


@dataclass
class FrontierResult:
    queries_run: int = 0
    terms: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    per_marketplace: dict[str, int] = field(default_factory=dict)
    # Why suggestions were dropped before the model ever saw them.
    rejected: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    stopped_reason: str = "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "queries_run": self.queries_run,
            "term_count": len(self.terms),
            "terms": self.terms[:80],
            "rejected": dict(self.rejected),
            "evidence_ids": self.evidence_ids,
            "per_marketplace": dict(self.per_marketplace),
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "stopped_reason": self.stopped_reason,
        }


class AutocompleteFrontier:
    """Mines Amazon's suggestion endpoint for long-tail demand language."""

    def __init__(
        self,
        browser: Any,
        evidence_store: Any,
        events: Any = None,
        *,
        max_queries: int = 160,
        max_depth: int = 2,
        time_budget_seconds: float = 420.0,
        max_marketplaces: int = 2,
        should_stop: Any = None,
    ) -> None:
        self.browser = browser
        self.evidence_store = evidence_store
        self.events = events
        self.max_queries = max(0, int(max_queries))
        self.max_depth = max(1, int(max_depth))
        self.time_budget = max(1.0, float(time_budget_seconds))
        self.max_marketplaces = max(1, int(max_marketplaces))
        # Cooperative stop: the runner passes owner-pause/cancel checks here
        # so the sweep yields within one query instead of running to budget.
        self.should_stop = should_stop or (lambda: False)

    # ------------------------------------------------------------------ run
    async def run(
        self,
        *,
        session_id: str,
        marketplaces: list[Marketplace],
        seeds: list[str],
    ) -> FrontierResult:
        """Deterministic breadth-then-depth sweep. Never raises."""
        result = FrontierResult()
        start = time.monotonic()
        deadline = start + self.time_budget
        mkts = list(marketplaces or [])[: self.max_marketplaces] or []
        if not mkts or self.max_queries == 0 or not seeds:
            result.stopped_reason = "no_budget_or_seeds"
            result.elapsed_seconds = time.monotonic() - start
            return result

        freq: dict[str, int] = {}
        prefix_counts: dict[str, int] = {}
        batches: dict[tuple[str, str], list[str]] = {}

        # Open ONE page per marketplace for the whole sweep.
        pages: dict[str, Any] = {}
        page_urls: dict[str, str] = {}
        mkt_by_code = {m.code: m for m in mkts}
        for mkt in mkts:
            if result.queries_run >= self.max_queries:
                break
            try:
                page = await self.browser.open_marketplace(mkt, "/")
                pages[mkt.code] = page
                page_urls[mkt.code] = scrub_url(getattr(page, "url", "") or "")
            except Exception:
                continue  # a marketplace that will not open is skipped

        async def query(mkt_code: str, prefix: str) -> list[str]:
            page = pages.get(mkt_code)
            if page is None:
                return []
            if (
                result.queries_run >= self.max_queries
                or time.monotonic() > deadline
            ):
                return []
            result.queries_run += 1
            result.per_marketplace[mkt_code] = (
                result.per_marketplace.get(mkt_code, 0) + 1
            )
            try:
                mkt = mkt_by_code.get(mkt_code)
                out = await page.evaluate(
                    SUGGEST_JS,
                    [ALIAS, mid_for(getattr(mkt, "domain", "")), prefix],
                )
            except Exception:
                return []  # one failed call never aborts the sweep
            if not isinstance(out, list):
                return []
            terms = [str(t).strip() for t in out[:10] if str(t).strip()]
            batches.setdefault((mkt_code, prefix), []).extend(terms)
            for t in terms:
                key = " ".join(t.lower().split())
                freq[key] = freq.get(key, 0) + 1
            prefix_counts[prefix] = prefix_counts.get(prefix, 0) + len(terms)
            return terms

        def exhausted() -> bool:
            return (
                result.queries_run >= self.max_queries
                or time.monotonic() > deadline
                or self.should_stop()
            )

        # ---- round 1: the seeds themselves -------------------------------
        for seed in seeds:
            if exhausted():
                break
            for mkt in mkts:
                if exhausted():
                    break
                await query(mkt.code, seed)

        # ---- round 2: the letter frontier. Only seeds that actually
        #      completed something get the (expensive) a-z sweep, and the
        #      sweep round-robins letter -> seed -> marketplace so every
        #      selected seed covers the same alphabet instead of one seed
        #      eating the whole budget. Letters are ordered a..z after
        #      ranking, so the sweep is deterministic.
        productive = sorted(
            seeds,
            key=lambda s: (-prefix_counts.get(s, 0), s),
        )[:4]
        letters = "abcdefghijklmnopqrstuvwxyz"
        frontier_terms: list[str] = []
        for letter in letters:
            if exhausted():
                break
            for seed in productive:
                if exhausted():
                    break
                for mkt in mkts:
                    if exhausted():
                        break
                    await query(mkt.code, f"{seed} {letter}")

        # ---- round 3: deepen promising completions (recursive, budgeted) --
        deep_candidates = self._rank_terms(freq)[: max(0, self.max_queries // 3)]
        frontier_terms.extend(deep_candidates)
        for depth in range(2, self.max_depth + 1):
            if exhausted() or not deep_candidates:
                break
            next_round: list[str] = []
            for term in deep_candidates:
                if exhausted():
                    break
                for mkt in mkts:
                    if exhausted():
                        break
                    await query(mkt.code, term)
            # The terms this round completed INTO are the deeper frontier.
            for t, _f in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0])):
                if len(t.split()) >= depth + 1 and t not in deep_candidates:
                    next_round.append(t)
                if len(next_round) >= 40:
                    break
            frontier_terms.extend(next_round)
            deep_candidates = next_round

        # ---- persist as ordinary evidence --------------------------------
        for (mkt_code, prefix), terms in list(batches.items()):
            if not terms:
                continue
            rec = self._record(
                session_id=session_id,
                marketplace=mkt_code,
                kind="autocomplete_frontier",
                title=f"frontier: {prefix}",
                url=page_urls.get(mkt_code, ""),
                data={
                    "prefix": prefix,
                    "suggestions": list(dict.fromkeys(terms))[:10],
                    "marketplace": mkt_code,
                },
            )
            if rec:
                result.evidence_ids.append(rec)

        # Screen BEFORE the model sees them: roots ("book for women") and
        # drift ("therapy worksheets for ptsd" off a grief seed) are counted as
        # filtered, not handed over as research targets.
        ranked = self._rank_terms(freq)
        kept, rejected = filter_demand_terms(ranked, seeds, limit=80)
        result.terms = kept
        result.rejected = rejected
        if kept:
            rec = self._record(
                session_id=session_id,
                marketplace=mkts[0].code if mkts else "",
                kind="demand_language",
                title="frontier: demand language",
                url=page_urls.get(mkts[0].code, "") if mkts else "",
                data={
                    "terms": [
                        {"term": t, "completions_seen": freq.get(t, 1)}
                        for t in kept
                    ],
                    "seeds": list(seeds),
                    "filtered_out": rejected,
                    "queries_run": result.queries_run,
                },
            )
            if rec:
                result.evidence_ids.append(rec)

        if result.queries_run >= self.max_queries:
            result.stopped_reason = "query_budget"
        elif time.monotonic() > deadline:
            result.stopped_reason = "time_budget"

        for page in pages.values():
            try:
                await self.browser.close_page(page)
            except Exception:
                pass
        result.elapsed_seconds = time.monotonic() - start
        if self.events is not None:
            try:
                self.events.append(
                    session_id,
                    level="info",
                    actor="system",
                    action="autocomplete_frontier_completed",
                    detail=redact(result.to_dict()),
                )
            except Exception:
                pass
        return result

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _rank_terms(freq: dict[str, int]) -> list[str]:
        scored = [
            (phrase_score(t, frequency=f), t)
            for t, f in freq.items()
        ]
        scored = [(s, t) for s, t in scored if s >= 20]
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [t for _s, t in scored]

    def _record(
        self,
        *,
        session_id: str,
        marketplace: str,
        kind: str,
        title: str,
        url: str,
        data: dict[str, Any],
    ) -> str | None:
        try:
            rec = self.evidence_store.create(
                session_id=session_id,
                marketplace=marketplace,
                kind=kind,
                url=url,
                title=title[:300],
                data=redact(data),
            )
            return rec.id
        except Exception:
            return None  # memory pressure must never kill the sweep


def seeds_for_session(
    prompt: str, objective: str, *, census_categories: Iterable[str] = ()
) -> list[str]:
    """Deterministic seed set: the owner's words first; the measured census
    second (an autonomous run has no words of its own); format roots last."""
    seeds = seeds_from_text(f"{prompt} {objective}")
    if seeds:
        return seeds
    for cat in census_categories:
        for w in seeds_from_text(str(cat), limit=2):
            if w not in seeds:
                seeds.append(w)
        if len(seeds) >= 4:
            break
    if not seeds:
        seeds = list(FALLBACK_SEEDS[:6])
    return seeds[:6]
