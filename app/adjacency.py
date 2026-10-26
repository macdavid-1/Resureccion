"""Adjacency expansion — walk outward from every winner.

A strong niche used to be the end of discovery: the run researched it and
stopped looking OUTWARD. Adjacency expansion is discovery re-entry triggered
by SUCCESS instead of total failure — from each strong candidate the system
walks the surrounding demand language (its autocomplete completions, the
frontier terms that share its head words, its census/category siblings),
measures the most promising neighbours with real searches, and promotes the
high-headroom ones to ordinary candidates mid-run.

Why this shape (and why it breaks nothing):
- it is a STEP INSIDE the existing phases, not a new phase — P1→P9 ordering,
  the exit gates and the report contract are untouched;
- promoted candidates are normal `candidates` rows: they flow through the
  same evidence → claims → filters → verification → gates pipeline as any
  other candidate, with their seed evidence ids attached from the start;
- promotion requires a MEASURED whitespace score at or above the threshold —
  a neighbour is never promoted on vibes, only on captured numbers;
- everything it derives is deterministic text processing over evidence the
  session already holds. No model, no new data source, no browser novelty.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from app.whitespace import WhitespaceScore, _head_terms, _log_norm

NEIGHBOR_SOURCES = ("autocomplete", "frontier", "category")


@dataclass
class Neighbor:
    term: str
    parent_candidate_id: str
    parent_niche: str
    source: str
    marketplace: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    pre_score: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "parent": self.parent_niche,
            "parent_candidate_id": self.parent_candidate_id,
            "source": self.source,
            "marketplace": self.marketplace,
            "evidence_ids": self.evidence_ids[:6],
            "pre_score": round(self.pre_score, 4),
        }


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _shares_head(term: str, head: list[str], *, minimum: int = 1) -> bool:
    if not head:
        return False
    t = _norm(term)
    return sum(h in t for h in head) >= min(minimum, len(head))


def _is_noise_phrase(term: str) -> bool:
    t = _norm(term)
    if len(t) < 8 or len(t) > 70:
        return True
    words = t.split()
    if len(words) < 2:
        return True
    bad = ("amazon.com", "paperback", "hardcover", "kindle", "books by", "$")
    return any(b in t for b in bad)


def _autocomplete_phrases(evidence: Iterable[dict[str, Any]], head: list[str]) -> list[tuple[str, str, str, list[str]]]:
    """(term, marketplace, source, evidence_ids) from demand-language rows."""
    out: list[tuple[str, str, str, list[str]]] = []
    for e in evidence:
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        kind = str(e.get("kind") or "")
        prefix = str(data.get("prefix") or data.get("query") or "")
        suggestions: list[str] = []
        if kind in ("autocomplete", "autocomplete_frontier"):
            suggestions = [str(s) for s in data.get("suggestions") or []]
        elif kind == "demand_language":
            for t in data.get("terms") or []:
                if isinstance(t, dict):
                    suggestions.append(str(t.get("term") or ""))
                else:
                    suggestions.append(str(t))
        if not suggestions:
            continue
        relevant = _shares_head(prefix, head) or _shares_head(
            " ".join(prefix.split()[:2]), head
        )
        if not relevant:
            # Terms themselves may still match even when the prefix did not.
            pass
        eid = str(e.get("id") or "")
        for s in suggestions:
            if _is_noise_phrase(s):
                continue
            if relevant or _shares_head(s, head):
                out.append((_norm(s), str(e.get("marketplace") or ""), "autocomplete", [eid] if eid else []))
    return out


def _category_phrases(evidence: Iterable[dict[str, Any]], head: list[str]) -> list[tuple[str, str, str, list[str]]]:
    out: list[tuple[str, str, str, list[str]]] = []
    for e in evidence:
        if str(e.get("kind") or "") != "census":
            continue
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        category = str(data.get("category") or "")
        if not category or _is_noise_phrase(category) or category in ("bestsellers", "new_releases"):
            continue
        if not _shares_head(category, head, minimum=1):
            continue
        eid = str(e.get("id") or "")
        out.append((_norm(category), str(e.get("marketplace") or ""), "category", [eid] if eid else []))
    return out


def _frequency(evidence: Iterable[dict[str, Any]], term: str) -> int:
    """How often this phrase appears across the session's demand language."""
    t = _norm(term)
    hits = 0
    for e in evidence:
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        kind = str(e.get("kind") or "")
        pool: list[str] = []
        if kind in ("autocomplete", "autocomplete_frontier"):
            pool = [str(s) for s in data.get("suggestions") or []]
        elif kind == "demand_language":
            for x in data.get("terms") or []:
                pool.append(str(x.get("term") if isinstance(x, dict) else x))
        hits += sum(1 for p in pool if _norm(p) == t)
    return hits


def pre_score(term: str, evidence: Iterable[dict[str, Any]]) -> float:
    """Cheap ordering signal: how much measured demand language exists for
    this neighbour. Used ONLY to decide which neighbours get measured next —
    never to promote (promotion needs a real whitespace score)."""
    rows = list(evidence)
    freq = _frequency(rows, term)
    depth = _log_norm(freq, 12)
    # Does any captured search already cover it?
    head = _head_terms(term)
    covered = any(
        str(e.get("kind")) == "search_results"
        and _shares_head(str((e.get("data") or {}).get("query") or ""), head, minimum=2)
        for e in rows
    )
    score = depth * 0.7 + (0.3 if covered else 0.0)
    return round(min(1.0, score + (0.15 if len(term.split()) >= 3 else 0.0)), 4)


def gather_neighbors(
    *,
    candidates: list[Any],
    evidence_records: Iterable[dict[str, Any]],
    frontier_terms: Iterable[str] = (),
    exclude_niches: Iterable[str] = (),
    limit: int = 40,
) -> list[Neighbor]:
    """Deterministic outward walk from every strong candidate.

    Neighbours are derived from evidence the session already holds and are
    ordered by `pre_score` (measured demand language first), ties broken
    deterministically so runs stay reproducible.
    """
    rows = [e for e in evidence_records if isinstance(e, dict)]
    excluded = {_norm(x) for x in exclude_niches if _norm(x)}
    strong = [
        c for c in candidates
        if str(getattr(c, "status", "")) != "rejected"
    ]
    found: dict[tuple[str, str], Neighbor] = {}

    for cand in strong:
        head = _head_terms(str(getattr(cand, "niche", "")))
        if not head:
            continue
        derived = _autocomplete_phrases(rows, head) + _category_phrases(rows, head)
        for term, mkt, source, eids in derived:
            if not term or term in excluded or _norm(getattr(cand, "niche", "")) == term:
                continue
            key = (term, mkt)
            if key in found:
                continue
            found[key] = Neighbor(
                term=term,
                parent_candidate_id=str(getattr(cand, "id", "")),
                parent_niche=str(getattr(cand, "niche", "")),
                source=source,
                marketplace=mkt,
                evidence_ids=eids,
                pre_score=pre_score(term, rows),
            )

    # Frontier terms only attach where they genuinely extend a winner's
    # language — otherwise they belong to discovery, not adjacency.
    for term in frontier_terms:
        t = _norm(term)
        if not t or t in excluded or _is_noise_phrase(t):
            continue
        for cand in strong:
            head = _head_terms(str(getattr(cand, "niche", "")))
            if not _shares_head(t, head):
                continue
            key = (t, str(getattr(cand, "marketplace", "")) or "")
            if key in found:
                break
            found[key] = Neighbor(
                term=t,
                parent_candidate_id=str(getattr(cand, "id", "")),
                parent_niche=str(getattr(cand, "niche", "")),
                source="frontier",
                marketplace=str(getattr(cand, "marketplace", "")) or "",
                evidence_ids=[],
                pre_score=pre_score(t, rows),
            )
            break

    neighbors = sorted(found.values(), key=lambda n: (-n.pre_score, n.term))
    return neighbors[: max(0, limit)]


def measured_score(
    neighbor: Neighbor,
    candidate: Any,
    evidence_records: Iterable[dict[str, Any]],
) -> WhitespaceScore:
    """Whitespace for a neighbour, using the evidence its measurement
    produced (the caller passes the candidate-scoped records)."""
    from app.whitespace import compute_whitespace

    return compute_whitespace(candidate, evidence_records)
