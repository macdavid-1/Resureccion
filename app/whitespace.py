"""The whitespace score — demand strength relative to competition strength.

Everything the score needs already exists in captured evidence: search
result rows, top-10 review counts, review presence, BSR observations,
autocomplete depth and census medians. This module combines them
DETERMINISTICALLY into one 0..1 "headroom" number per candidate:

    whitespace = demand / (demand + competition)

Reading it: near 1.0 means real observed demand meeting almost no incumbent
resistance; near 0.0 means a crowd is already fighting over it.

THE RULE THAT KEEPS THE SYSTEM INTACT (enforced by construction — this
module has no authority over any verdict):
- it only ORDERS and ALLOCATES — candidates are sorted by headroom for
  budget/priority and reports are ranked by it;
- it NEVER overrides a filter, a quality gate, a KDP risk screen or the
  verification engine. A high score cannot rescue a rejected candidate and a
  zero score cannot save a verified one.

Unmeasured candidates score `None` (not 0.0): absence of evidence is not
evidence of absence, and must never masquerade as a measured zero.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

# Component weights (renormalised over the components actually present).
_DEMAND_WEIGHTS = {
    "buyer_activity": 0.45,     # share of observed competitors with reviews
    "autocomplete_depth": 0.25, # how deep the demand language runs
    "bsr_strength": 0.30,       # best observed best-seller rank
}
_COMPETITION_WEIGHTS = {
    "review_barrier": 0.55,     # median reviews of the top of the list
    "deep_moat_share": 0.30,    # share of incumbents with 500+ reviews
    "crowding": 0.15,           # how many distinct incumbents were observed
}

_DEEP_MOAT = 500
_MAX_CROWD = 20
_BSR_FLOOR_LOG = 5.7  # log10(500k): a book this deep is not selling


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _log_norm(value: float, ceiling: float) -> float:
    """0..1 by log scale: review counts and BSR span orders of magnitude."""
    if value is None or value <= 0:
        return 0.0
    return _clamp(math.log10(1 + float(value)) / math.log10(1 + float(ceiling)))


@dataclass
class WhitespaceScore:
    score: float | None = None
    demand: float | None = None
    competition: float | None = None
    components: dict[str, float] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)

    @property
    def measured(self) -> bool:
        return self.score is not None

    @property
    def headroom(self) -> float:
        return float(self.score or 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4) if self.score is not None else None,
            "measured": self.measured,
            "demand": round(self.demand, 4) if self.demand is not None else None,
            "competition": round(self.competition, 4) if self.competition is not None else None,
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "evidence_ids": list(self.evidence_ids[:12]),
        }

    def note(self) -> str:
        if not self.measured:
            return "headroom unmeasured (no comparable evidence captured yet)"
        return f"headroom {self.score:.2f} (demand {self.demand:.2f} ÷ competition {self.competition:.2f})"


# --------------------------------------------------------------------------- #
# Component extraction (pure functions over evidence dicts)
# --------------------------------------------------------------------------- #
def _observed_competitors(evidence: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in evidence:
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        if e.get("kind") == "search_results":
            for r in data.get("results") or []:
                if isinstance(r, dict) and (r.get("asin") or r.get("review_count") is not None):
                    out.append(r)
        elif e.get("kind") == "product_page":
            if data.get("asin") or data.get("review_count") is not None:
                out.append(data)
    return out


def _review_counts(competitors: Iterable[dict[str, Any]]) -> list[int]:
    return [
        int(c["review_count"])
        for c in competitors
        if isinstance(c.get("review_count"), (int, float)) and int(c["review_count"]) >= 0
    ]


def _median(values: list[int]) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    mid = len(vals) // 2
    if len(vals) % 2:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2.0


def _autocomplete_depth(evidence: Iterable[dict[str, Any]], head_terms: list[str]) -> float:
    """Distinct demand-language completions that mention the niche's terms."""
    terms: set[str] = set()
    for e in evidence:
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        kind = e.get("kind")
        if kind in ("autocomplete", "autocomplete_frontier"):
            for s in data.get("suggestions") or []:
                terms.add(str(s).lower())
        elif kind == "demand_language":
            for t in data.get("terms") or []:
                if isinstance(t, dict):
                    terms.add(str(t.get("term") or "").lower())
                else:
                    terms.add(str(t).lower())
    if not terms:
        return 0.0
    if head_terms:
        relevant = [
            t for t in terms
            if sum(term in t for term in head_terms) >= min(2, len(head_terms))
        ] or list(terms)
    else:
        relevant = list(terms)
    return _log_norm(len(set(relevant)), 40)


def _best_bsr(competitors: Iterable[dict[str, Any]]) -> float | None:
    ranks = [
        int(c["best_seller_rank"])
        for c in competitors
        if isinstance(c.get("best_seller_rank"), (int, float))
        and int(c["best_seller_rank"]) > 0
    ]
    if not ranks:
        return None
    return _clamp(1.0 - math.log10(min(ranks)) / _BSR_FLOOR_LOG)


def _weighted(parts: dict[str, float], weights: dict[str, float]) -> float | None:
    """Weighted mean over the components that are actually present."""
    total = 0.0
    weight_sum = 0.0
    for name, weight in weights.items():
        if name not in parts:
            continue
        total += parts[name] * weight
        weight_sum += weight
    if weight_sum <= 0:
        return None
    return total / weight_sum


def _head_terms(niche: str) -> list[str]:
    import re

    return [w for w in re.findall(r"[a-z0-9]+", (niche or "").lower()) if len(w) > 3][:4]


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def compute_whitespace(
    candidate: Any,
    evidence_records: Iterable[dict[str, Any]],
) -> WhitespaceScore:
    """Deterministic headroom score for one candidate. Never raises."""
    evidence = [e for e in evidence_records if isinstance(e, dict)]
    niche = str(getattr(candidate, "niche", "") or "")
    head = _head_terms(niche)

    competitors = _observed_competitors(evidence)
    counts = _review_counts(competitors)
    ev_ids = [str(e.get("id")) for e in evidence if e.get("id")]

    demand_parts: dict[str, float] = {}
    comp_parts: dict[str, float] = {}

    if competitors:
        with_reviews = [c for c in counts if c > 0]
        demand_parts["buyer_activity"] = len(with_reviews) / max(1, len(competitors))
        if len(competitors) >= 3:
            comp_parts["crowding"] = _clamp(len(set(
                str(c.get("asin") or c.get("title") or i)
                for i, c in enumerate(competitors)
            )) / _MAX_CROWD)

    barrier = _median(counts)
    if barrier is not None:
        comp_parts["review_barrier"] = _log_norm(barrier, 1000)
    if counts:
        comp_parts["deep_moat_share"] = sum(1 for c in counts if c >= _DEEP_MOAT) / len(counts)

    depth = _autocomplete_depth(evidence, head)
    if depth > 0:
        demand_parts["autocomplete_depth"] = depth

    bsr = _best_bsr(competitors)
    if bsr is not None:
        demand_parts["bsr_strength"] = bsr

    demand = _weighted(demand_parts, _DEMAND_WEIGHTS)
    competition = _weighted(comp_parts, _COMPETITION_WEIGHTS)
    if demand is None and competition is None:
        return WhitespaceScore(evidence_ids=ev_ids[:12])

    d = demand if demand is not None else 0.0
    c = competition if competition is not None else 0.0
    score = d / (d + c) if (d + c) > 0 else 0.5
    return WhitespaceScore(
        score=round(_clamp(score), 4),
        demand=round(d, 4),
        competition=round(c, 4),
        components={**demand_parts, **comp_parts},
        evidence_ids=ev_ids[:12],
    )


def ranking(scores: dict[str, WhitespaceScore | None]) -> list[tuple[str, float]]:
    """Candidate id -> headroom, measured first, unmeasured last.

    Pure ordering: ties break deterministically by id so prompts and reports
    are stable between renders.
    """
    items = [
        (cid, float(s.score) if (s is not None and s.score is not None) else -1.0)
        for cid, s in scores.items()
    ]
    items.sort(key=lambda kv: (-kv[1], kv[0]))
    return items


def priority_lines(
    candidates: Any,
    scores: dict[str, WhitespaceScore | None],
    *,
    limit: int = 8,
) -> list[str]:
    """Human-readable allocation order shown to the model and in the report."""
    by_id = {c.id: c for c in candidates}
    lines: list[str] = []
    for cid, value in ranking(scores)[:limit]:
        cand = by_id.get(cid)
        if cand is None:
            continue
        label = "unmeasured" if value < 0 else f"{value:.2f}"
        lines.append(f"{label} — {cand.niche[:90]} [{cand.status}]")
    return lines
