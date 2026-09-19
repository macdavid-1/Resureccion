"""Deterministic candidate filters.

Named, explicit filters applied to every candidate before it may enter
verification. Each filter has an identifier, a deterministic predicate over
measurable session data, and a human-readable reason. A candidate that fails
any filter is rejected WITH its reason recorded as a durable integrity
assessment — rejection is never silent and never vibes-based.

Filters (per the owner's specification):
    insufficient_evidence        too few evidence pieces touch the candidate
    duplicate_opportunity        near-duplicate of an existing candidate
    overly_broad_market          niche fails specificity requirements
    market_too_narrow            niche too thin to be commercially meaningful
    weak_demand                  demand evidence is absent or contradicted
    excessive_competition        saturation signals dominate the niche
    misleading_anomaly           demand rests on a single outlier
    insufficient_competitors     competitor sample below threshold
    insufficient_consumer_evidence  review/consumer evidence below threshold
    stale_evidence               evidence predates the staleness window
    contradictory_evidence       claims conflict without resolution
    unverifiable_claims          too many claims lack any evidence ids
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.claims import CLAIM_UNSUPPORTED, ClaimStore, EQ_INFERENCE
from app.integrity import KIND_FILTER, IntegrityAssessmentStore
from app.thresholds import EvidenceThresholds

# Filters that reject outright vs. downgrade to "needs more research".
HARD_FILTERS = (
    "duplicate_opportunity",
    "overly_broad_market",
    "weak_demand",
    "excessive_competition",
    "misleading_anomaly",
    "contradictory_evidence",
    "unverifiable_claims",
)
SOFT_FILTERS = (
    "insufficient_evidence",
    "market_too_narrow",
    "insufficient_competitors",
    "insufficient_consumer_evidence",
    "stale_evidence",
)

FILTER_NAMES = HARD_FILTERS + SOFT_FILTERS

# Words that signal a niche is still a broad market, not a niche.
_BROAD_MARKERS = {
    "productivity", "self-help", "self help", "health", "fitness", "diet",
    "cooking", "parenting", "finance", "money", "business", "marketing",
    "journal", "notebook", "planner", "coloring book", "activity book",
    "childrens books", "kids books", "romance", "thriller", "fantasy",
    "science fiction", "sci-fi", "horror", "history", "biography",
    "meditation", "mindfulness", "hobbies", "crafts", "travel", "spirituality",
}

# Word-count below which a niche string is almost certainly too broad to
# name WHO has WHAT problem in WHICH context.
_MIN_NICHE_WORDS = 4


@dataclass
class FilterResult:
    name: str
    passed: bool
    outcome: str          # "pass" | "reject" | "downgrade"
    reason: str
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "outcome": self.outcome,
            "reason": self.reason,
            "metrics": self.metrics,
        }


@dataclass
class FilterReport:
    candidate_id: str
    niche: str
    verdict: str           # "accept" | "reject" | "downgrade"
    failures: list[FilterResult]
    passes: list[FilterResult]

    @property
    def reasons(self) -> list[str]:
        return [f.reason for f in self.failures]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "niche": self.niche,
            "verdict": self.verdict,
            "failures": [f.to_dict() for f in self.failures],
            "passes": [f.to_dict() for f in self.passes],
        }


class CandidateFilterEngine:
    """Applies the named deterministic filters to one candidate."""

    def __init__(
        self,
        thresholds: EvidenceThresholds,
        assessments: IntegrityAssessmentStore,
    ) -> None:
        self.t = thresholds
        self.assessments = assessments

    # ------------------------------------------------------------------ run
    def evaluate(
        self,
        session_id: str,
        candidate: Any,
        *,
        evidence_records: list[dict[str, Any]],
        claims: list[Any],
        existing_niches: list[str],
        observation_count: int = 0,
    ) -> FilterReport:
        """Run all filters. `evidence_records` are the session's browser
        evidence dicts relevant to this candidate (filtered by the caller);
        `claims` are the candidate's Claim objects."""
        results: list[FilterResult] = []

        results.append(self._filter_insufficient_evidence(evidence_records))
        results.append(self._filter_duplicate(candidate, existing_niches))
        results.append(self._filter_overly_broad(candidate))
        results.append(self._filter_too_narrow(candidate, evidence_records))
        results.append(self._filter_weak_demand(claims, evidence_records))
        results.append(self._filter_excessive_competition(evidence_records, claims))
        results.append(self._filter_misleading_anomaly(evidence_records, claims))
        results.append(self._filter_insufficient_competitors(evidence_records, claims))
        results.append(self._filter_insufficient_consumer_evidence(evidence_records))
        results.append(self._filter_stale(evidence_records))
        results.append(self._filter_contradictory(claims))
        results.append(self._filter_unverifiable(claims))

        failures = [r for r in results if not r.passed]
        hard_failures = [r for r in failures if r.name in HARD_FILTERS]
        soft_failures = [r for r in failures if r.name in SOFT_FILTERS]
        if hard_failures:
            verdict = "reject"
        elif soft_failures:
            verdict = "downgrade"   # needs more research, not death
        else:
            verdict = "accept"

        report = FilterReport(
            candidate_id=str(getattr(candidate, "id", "")),
            niche=str(getattr(candidate, "niche", "")),
            verdict=verdict,
            failures=sorted(failures, key=lambda r: FILTER_NAMES.index(r.name)),
            passes=[r for r in results if r.passed],
        )

        # Persist each failure as a durable assessment (and a summary).
        for r in failures:
            self.assessments.record(
                session_id,
                kind=KIND_FILTER,
                subject_type="candidate",
                subject_id=report.candidate_id,
                stage=r.name,
                outcome="fail" if r.name in HARD_FILTERS else "warn",
                reason=r.reason,
                metrics=r.metrics,
            )
        if not failures:
            self.assessments.record(
                session_id,
                kind=KIND_FILTER,
                subject_type="candidate",
                subject_id=report.candidate_id,
                stage="all_filters",
                outcome="pass",
                reason="candidate passed all deterministic filters",
                metrics={"filters_run": len(results)},
            )
        return report

    # -------------------------------------------------------------- filters
    def _filter_insufficient_evidence(self, evidence_records: list[dict[str, Any]]) -> FilterResult:
        n = len(evidence_records)
        floor = max(2, self.t.min_independent_sources)
        ok = n >= floor
        return FilterResult(
            name="insufficient_evidence",
            passed=ok,
            outcome="pass" if ok else "downgrade",
            reason=(
                f"candidate has {n} relevant evidence records; "
                f"needs >= {floor} before verification"
            ),
            metrics={"evidence_count": n, "required": floor},
        )

    def _filter_duplicate(self, candidate: Any, existing_niches: list[str]) -> FilterResult:
        niche = _norm_niche(str(getattr(candidate, "niche", "")))
        dup = None
        for other in existing_niches:
            o = _norm_niche(other)
            if not o or o == niche:
                continue
            if _similarity(niche, o) >= 0.7:
                dup = other
                break
        ok = dup is None
        return FilterResult(
            name="duplicate_opportunity",
            passed=ok,
            outcome="pass" if ok else "reject",
            reason=(
                "no near-duplicate candidate exists" if ok
                else f"near-duplicate of existing candidate niche {dup!r} (similarity >= 0.7)"
            ),
            metrics={"niche": niche, "duplicate_of": dup or ""},
        )

    def _filter_overly_broad(self, candidate: Any) -> FilterResult:
        niche = str(getattr(candidate, "niche", ""))
        words = len(niche.split())
        lowered = niche.lower().strip()
        # A niche made only of broad markers (plus filler) is a broad market.
        content_words = [
            w for w in re.findall(r"[a-z0-9']+", lowered)
            if w not in ("for", "with", "and", "the", "a", "an", "of", "in", "to", "book", "books")
        ]
        broad_hits = [m for m in _BROAD_MARKERS if m in lowered]
        # Broad ONLY if every content word is a broad marker or the niche is
        # too short to carry specificity.
        is_broad = (
            words < _MIN_NICHE_WORDS
            or (broad_hits and len(content_words) <= len(broad_hits) + 1)
        )
        ok = not is_broad
        return FilterResult(
            name="overly_broad_market",
            passed=ok,
            outcome="pass" if ok else "reject",
            reason=(
                f"niche {niche!r} names a specific audience+problem+context"
                if ok else
                f"niche {niche!r} is a broad market (word count {words} < {_MIN_NICHE_WORDS}, "
                f"broad markers: {broad_hits or 'n/a'}); it does not name WHO has WHAT problem in WHICH context"
            ),
            metrics={
                "niche": niche,
                "word_count": words,
                "min_words": _MIN_NICHE_WORDS,
                "broad_markers": broad_hits,
            },
        )

    def _filter_too_narrow(self, candidate: Any, evidence_records: list[dict[str, Any]]) -> FilterResult:
        """A niche is TOO narrow when there is essentially no market around it:
        almost no distinct competitors and no demand signals at all."""
        competitor_asins = _distinct_asins(evidence_records)
        demand_signals = _demand_signal_count(evidence_records)
        # Too narrow only when BOTH are effectively absent. A niche with a
        # handful of sellers is specific, not dead — commercial viability is
        # judged by the demand/competition gates, not here.
        ok = not (len(competitor_asins) == 0 and demand_signals == 0)
        return FilterResult(
            name="market_too_narrow",
            passed=ok,
            outcome="pass" if ok else "downgrade",
            reason=(
                "niche shows adjacent market activity"
                if ok else
                "no competitor products and no demand signals observed at all; "
                "the niche may be too narrow to be commercially meaningful — needs discovery before verification"
            ),
            metrics={
                "distinct_competitor_asins": len(competitor_asins),
                "demand_signals": demand_signals,
            },
        )

    def _filter_weak_demand(self, claims: list[Any], evidence_records: list[dict[str, Any]]) -> FilterResult:
        demand_claims = [c for c in claims if getattr(c, "kind", "") == "demand"]
        supported = [
            c for c in demand_claims
            if getattr(c, "evidence_ids", None) and getattr(c, "status", "") != CLAIM_UNSUPPORTED
        ]
        demand_signals = _demand_signal_count(evidence_records)
        # Hard filters judge the QUALITY of evidence that exists. A candidate
        # with no demand data at all is the soft insufficient_evidence
        # filter's job (downgrade), not a conviction on an empty record.
        has_any_data = bool(evidence_records) or bool(demand_claims)
        ok = (
            not has_any_data
            or bool(supported)
            or demand_signals >= self.t.min_independent_sources
        )
        return FilterResult(
            name="weak_demand",
            passed=ok,
            outcome="pass" if ok else "reject",
            reason=(
                f"demand supported by {len(supported)} evidenced claim(s) "
                f"and {demand_signals} demand signal(s)"
                if ok and (supported or demand_signals) else
                "no demand evidence gathered yet; insufficient_evidence governs"
                if ok else
                "no evidenced demand claims and no observable demand signals "
                "(rankings, review velocity, competing breadth) — demand is asserted, not observed"
            ),
            metrics={
                "demand_claims": len(demand_claims),
                "supported_demand_claims": len(supported),
                "demand_signals": demand_signals,
                "required_signals": self.t.min_independent_sources,
            },
        )

    def _filter_excessive_competition(self, evidence_records: list[dict[str, Any]], claims: list[Any]) -> FilterResult:
        competitor_asins = _distinct_asins(evidence_records)
        # Saturation is judged from the competitive-map claims + observation:
        # many incumbents with deep review moats and no differentiation talk.
        saturation_claims = [
            c for c in claims
            if getattr(c, "kind", "") == "competition"
            and "saturat" in str(getattr(c, "statement", "")).lower()
            and getattr(c, "evidence_ids", None)
        ]
        deep_moats = _deep_review_moat_count(evidence_records)
        # Excessive = large incumbent set AND most show deep review moats AND
        # a corroborated saturation claim exists.
        ok = not (
            len(competitor_asins) >= self.t.min_competitor_sample * 2
            and deep_moats > len(competitor_asins) / 2
            and saturation_claims
        )
        return FilterResult(
            name="excessive_competition",
            passed=ok,
            outcome="pass" if ok else "reject",
            reason=(
                "competition level within acceptable bounds"
                if ok else
                f"{len(competitor_asins)} incumbents observed with {deep_moats} showing deep review "
                f"moats (>50%) and a corroborated saturation claim exists — saturation dominates the niche"
            ),
            metrics={
                "distinct_competitor_asins": len(competitor_asins),
                "deep_review_moats": deep_moats,
                "saturation_claims": len(saturation_claims),
                "threshold_multiplier": 2,
            },
        )

    def _filter_misleading_anomaly(self, evidence_records: list[dict[str, Any]], claims: list[Any]) -> FilterResult:
        """Demand that rests on ONE product/one observation is an anomaly."""
        demand_sources = _demand_source_urls(evidence_records)
        demand_claims = [c for c in claims if getattr(c, "kind", "") == "demand"]
        single_source_demand = (
            len(demand_sources) == 1
            and len(demand_claims) >= 1
        )
        ok = not single_source_demand
        return FilterResult(
            name="misleading_anomaly",
            passed=ok,
            outcome="pass" if ok else "reject",
            reason=(
                "demand evidence spans multiple sources"
                if ok else
                "all demand evidence traces to a single URL — this may be a one-book anomaly, "
                "a promoted item, or a transient; corroboration required"
            ),
            metrics={
                "demand_source_urls": len(demand_sources),
                "demand_claims": len(demand_claims),
            },
        )

    def _filter_insufficient_competitors(self, evidence_records: list[dict[str, Any]], claims: list[Any]) -> FilterResult:
        competitors = _competitor_records(evidence_records)
        distinct_asins = _distinct_asins(evidence_records)
        ok = len(distinct_asins) >= self.t.min_competitor_sample or len(competitors) == 0
        # A candidate with NO competitor records at all hasn't been researched
        # yet (that's insufficient_evidence's job); this filter only fires when
        # SOME competition work happened but the sample is thin.
        return FilterResult(
            name="insufficient_competitors",
            passed=ok,
            outcome="pass" if ok else "downgrade",
            reason=(
                f"competitor sample adequate ({len(distinct_asins)} distinct ASINs)"
                if ok else
                f"only {len(distinct_asins)} distinct competitor ASINs observed; need >= "
                f"{self.t.min_competitor_sample} to judge the landscape (judging from 1-2 books is how false positives are born)"
            ),
            metrics={
                "distinct_competitor_asins": len(distinct_asins),
                "required": self.t.min_competitor_sample,
            },
        )

    def _filter_insufficient_consumer_evidence(self, evidence_records: list[dict[str, Any]]) -> FilterResult:
        review_records = [e for e in evidence_records if e.get("kind") == "reviews"]
        review_count = sum(
            len((e.get("data") or {}).get("reviews") or []) for e in review_records
        )
        distinct_products = len({(e.get("data") or {}).get("asin") or e.get("url") for e in review_records})
        # Only fires when the session HAS consumer evidence work; the
        # insufficient_evidence filter handles "nothing gathered at all".
        has_any_consumer_work = bool(review_records)
        ok = (
            not has_any_consumer_work
            or (
                review_count >= self.t.min_review_sample
                and distinct_products >= self.t.min_review_products
            )
        )
        return FilterResult(
            name="insufficient_consumer_evidence",
            passed=ok,
            outcome="pass" if ok else "downgrade",
            reason=(
                "consumer evidence adequate or not yet attempted"
                if ok else
                f"{review_count} reviews across {distinct_products} products; need >= "
                f"{self.t.min_review_sample} reviews across >= {self.t.min_review_products} distinct products "
                "before complaint patterns count as market-wide"
            ),
            metrics={
                "reviews": review_count,
                "distinct_products": distinct_products,
                "required_reviews": self.t.min_review_sample,
                "required_products": self.t.min_review_products,
            },
        )

    def _filter_stale(self, evidence_records: list[dict[str, Any]]) -> FilterResult:
        cutoff = datetime.now(timezone.utc) - timedelta(days=_STALENESS_DAYS)
        stale = 0
        total = 0
        for e in evidence_records:
            captured = str(e.get("captured_at") or "")
            if not captured:
                continue
            total += 1
            dt = _parse_iso(captured)
            if dt is not None and dt < cutoff:
                stale += 1
        ok = total == 0 or stale / total < 0.34  # >1/3 stale is unreliable
        return FilterResult(
            name="stale_evidence",
            passed=ok,
            outcome="pass" if ok else "downgrade",
            reason=(
                "evidence is current"
                if ok else
                f"{stale}/{total} evidence records are older than {_STALENESS_DAYS} days — "
                "rankings/prices drift; refresh before verification"
            ),
            metrics={"stale": stale, "total": total, "staleness_days": _STALENESS_DAYS},
        )

    def _filter_contradictory(self, claims: list[Any]) -> FilterResult:
        """Two same-kind claims with opposite polarity and no resolution."""
        contradictions = 0
        by_kind: dict[str, list[Any]] = {}
        for c in claims:
            by_kind.setdefault(str(getattr(c, "kind", "")), []).append(c)
        for kind, group in by_kind.items():
            if kind == "other" or len(group) < 2:
                continue
            pos = [c for c in group if _polarity(str(getattr(c, "statement", ""))) > 0]
            neg = [c for c in group if _polarity(str(getattr(c, "statement", ""))) < 0]
            if pos and neg:
                # Contradiction unless one side is explicitly marked resolved.
                resolved = any(
                    "resolved" in str(getattr(c, "meta", {})).lower() for c in group
                )
                if not resolved:
                    contradictions += 1
        ok = contradictions == 0
        return FilterResult(
            name="contradictory_evidence",
            passed=ok,
            outcome="pass" if ok else "reject",
            reason=(
                "no unresolved contradictions between claims"
                if ok else
                f"{contradictions} unresolved same-kind contradiction(s) — investigate or qualify before verification"
            ),
            metrics={"contradictions": contradictions},
        )

    def _filter_unverifiable(self, claims: list[Any]) -> FilterResult:
        important = [c for c in claims if getattr(c, "kind", "") in _IMPORTANT_KINDS]
        if not important:
            # No important claims yet — nothing to call unverifiable; the
            # supported-ratio gate handles "no claims at all".
            return FilterResult(
                name="unverifiable_claims",
                passed=True,
                outcome="pass",
                reason="no important claims registered yet",
                metrics={"important_claims": 0},
            )
        unsupported = [
            c for c in important
            if not getattr(c, "evidence_ids", None)
            or getattr(c, "status", "") == CLAIM_UNSUPPORTED
        ]
        ratio = len(unsupported) / len(important)
        ok = ratio <= 0.5
        return FilterResult(
            name="unverifiable_claims",
            passed=ok,
            outcome="pass" if ok else "reject",
            reason=(
                f"{len(important) - len(unsupported)}/{len(important)} important claims are evidence-backed"
                if ok else
                f"{len(unsupported)}/{len(important)} important claims have NO evidence ids — "
                "more than half the candidate's substance is unverifiable model assertion"
            ),
            metrics={
                "important_claims": len(important),
                "unsupported_claims": len(unsupported),
                "unsupported_ratio": round(ratio, 3),
            },
        )


# ------------------------------------------------------------------ helpers
_IMPORTANT_KINDS = ("demand", "competition", "consumer_need", "market_gap")

# Evidence captured within this window counts as current (rankings, prices,
# and review counts drift; anything older is treated as stale).
_STALENESS_DAYS = 7


def _norm_niche(niche: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", niche.lower()).split()


def _similarity(a_words: list[str], b_words: list[str]) -> float:
    """Word-overlap Jaccard similarity between two niches."""
    a, b = set(a_words), set(b_words)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _distinct_asins(evidence_records: list[dict[str, Any]]) -> set[str]:
    asins: set[str] = set()
    for e in evidence_records:
        data = e.get("data") or {}
        for item in data.get("results") or []:
            asin = str(item.get("asin") or "").strip()
            if asin:
                asins.add(asin)
        for rel in data.get("related") or []:
            asin = str(rel.get("asin") or "").strip()
            if asin:
                asins.add(asin)
        asin = str(data.get("asin") or "").strip()
        if asin:
            asins.add(asin)
    return asins


def _competitor_records(evidence_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Records that carry competitor product data (search results, product
    pages, related products)."""
    return [
        e for e in evidence_records
        if e.get("kind") in ("search_results", "product_page", "related_products")
    ]


def _demand_signal_count(evidence_records: list[dict[str, Any]]) -> int:
    """Count demand-bearing observations: review counts, rankings, prices on
    live product/search captures, KDSpy demand panels."""
    signals = 0
    for e in evidence_records:
        data = e.get("data") or {}
        kind = e.get("kind", "")
        if kind == "kdspy_panel":
            signals += 1
        elif kind == "search_results":
            results = data.get("results") or []
            if any((r.get("review_count") or 0) > 0 for r in results):
                signals += 1
        elif kind == "product_page":
            if (data.get("review_count") or 0) > 0 or (data.get("best_seller_rank") or 0) > 0:
                signals += 1
        elif kind == "reviews":
            if data.get("reviews"):
                signals += 1
    return signals


def _demand_source_urls(evidence_records: list[dict[str, Any]]) -> set[str]:
    urls: set[str] = set()
    for e in evidence_records:
        if _demand_signal_count([e]) > 0:
            urls.add(str(e.get("url") or ""))
    return urls


def _deep_review_moat_count(evidence_records: list[dict[str, Any]]) -> int:
    """Distinct competitors with review counts suggesting entrenched
    incumbents (> 500 reviews is a serious moat for most KDP niches)."""
    moats: set[str] = set()
    for e in evidence_records:
        data = e.get("data") or {}
        asin = str(data.get("asin") or "")
        if (data.get("review_count") or 0) > 500 and asin:
            moats.add(asin)
        for item in data.get("results") or []:
            asin2 = str(item.get("asin") or "")
            if (item.get("review_count") or 0) > 500 and asin2:
                moats.add(asin2)
    return len(moats)


def _polarity(statement: str) -> int:
    """+1 if the statement asserts presence/strong demand, -1 if absence/weak."""
    s = statement.lower()
    neg_markers = ("no demand", "weak demand", "declining", "not selling", "does not sell",
                   "isn't selling", "little demand", "absent", "no evidence of demand")
    pos_markers = ("strong demand", "sustained demand", "sells well", "consistent demand",
                   "growing demand", "high demand", "present demand")
    if any(m in s for m in neg_markers):
        return -1
    if any(m in s for m in pos_markers):
        return 1
    return 0


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
