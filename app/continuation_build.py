"""Read a finished (or paused) run out of the durable stores into a brief.

`app/continuation.py` owns the SHAPE and the rendering; this module owns the
READING. Every field comes from a stored row — nothing here summarizes with a
model, and nothing invents a figure. Sections that a session never produced
stay empty, and the renderer simply omits them.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.continuation import (
    OPPORTUNITY_LIMIT,
    THOUGHT_LIMIT,
    ContinuationBrief,
)

# Candidate statuses that mean "this was researched and killed".
_REJECTED = {"rejected"}


def _opp_verdict(verifications: Any, session_id: str, candidate_id: str) -> str:
    try:
        vers = verifications.for_candidate(session_id, candidate_id)
    except Exception:
        return ""
    for v in vers:
        verdict = str(getattr(v, "verdict", "") or "")
        if verdict:
            return verdict
    return ""


def _report_summary(reports: Any, session_id: str) -> tuple[str, str]:
    """(title, readable summary) of the newest report, or ('', '')."""
    try:
        rep = reports.latest(session_id)
    except Exception:
        return "", ""
    if rep is None:
        return "", ""
    body = str(getattr(rep, "body_markdown", "") or "")
    text = re.sub(r"^#{1,6}\s*", "", body, flags=re.MULTILINE)
    text = re.sub(r"[*_`>#|]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return str(getattr(rep, "title", "") or ""), text


def _thought_excerpts(thoughts: Any, session_id: str) -> list[dict[str, Any]]:
    """The last few reasoning rows, as short excerpts (never the raw dump)."""
    out: list[dict[str, Any]] = []
    try:
        rows = thoughts.recent(session_id, limit=THOUGHT_LIMIT * 3)
    except Exception:
        return out
    for row in rows:
        try:
            detail = thoughts.get(int(row["id"]))
        except Exception:
            continue
        text = " ".join(str(getattr(detail, "detail", "") or "").split())
        if not text:
            continue
        out.append({
            "label": str(row.get("label") or "thinking"),
            "excerpt": text[:700],
        })
    return out[-THOUGHT_LIMIT:]


def _actions(trace: Any, session_id: str) -> list[dict[str, Any]]:
    """Latest trace entries (the run's own account of what it did)."""
    try:
        rows = trace.tail(session_id, limit=60)
    except Exception:
        return []
    return [
        {"kind": str(getattr(r, "kind", "") or ""), "text": str(getattr(r, "text", "") or "")}
        for r in rows
        if str(getattr(r, "text", "") or "").strip()
    ]


def _keywords_and_categories(evidence_rows: list[Any]) -> tuple[list, list]:
    keywords: list[dict[str, Any]] = []
    categories: list[dict[str, Any]] = []
    for e in evidence_rows:
        kind = getattr(e, "kind", "")
        data = getattr(e, "data", None)
        if not isinstance(data, dict):
            continue
        if kind == "keyword_research":
            for k in data.get("keywords") or []:
                if isinstance(k, dict) and k.get("measured"):
                    keywords.append({
                        "keyword": k.get("keyword"),
                        "total_results": k.get("total_results"),
                        "competition": k.get("competition"),
                        "top10_review_median": k.get("top10_review_median"),
                        "marketplace": k.get("marketplace") or getattr(e, "marketplace", ""),
                    })
        elif kind == "category_research":
            for store, rows in (("books", data.get("books")), ("kindle", data.get("kindle_store"))):
                for r in rows or []:
                    if isinstance(r, dict) and r.get("category"):
                        categories.append({
                            "store": store,
                            "category": r.get("category"),
                            "breadcrumb": r.get("breadcrumb"),
                        })
    return keywords, categories


def _competitors(evidence_rows: list[Any], limit: int = 8) -> list[dict[str, Any]]:
    """Observed competitor products — the run's own browser evidence."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in evidence_rows:
        if getattr(e, "kind", "") not in ("product_page", "search_results", "competitors"):
            continue
        data = getattr(e, "data", None)
        if not isinstance(data, dict):
            continue
        rows = data.get("items") if isinstance(data.get("items"), list) else []
        if isinstance(data.get("asin"), str):
            rows = [data]
        for r in rows:
            if not isinstance(r, dict):
                continue
            key = str(r.get("asin") or r.get("title") or "")[:120]
            if not key or key in seen:
                continue
            seen.add(key)
            title = str(r.get("title") or "").strip()
            if not title:
                continue
            note = []
            if r.get("review_count") is not None:
                note.append(f"{r['review_count']} reviews")
            if r.get("rating") is not None:
                note.append(f"rated {r['rating']}")
            out.append({"title": title[:160], "asin": r.get("asin"), "note": ", ".join(note)})
            if len(out) >= limit:
                return out
    return out


def _consumer_lines(evidence_rows: list[Any]) -> list[str]:
    """Verbatim-ish review material the run captured (nothing inferred)."""
    lines: list[str] = []
    for e in evidence_rows:
        if getattr(e, "kind", "") != "review_research":
            continue
        data = getattr(e, "data", None)
        if not isinstance(data, dict):
            continue
        for r in data.get("reviews") or []:
            if isinstance(r, dict):
                text = str(r.get("text") or r.get("review") or "").strip()
                if text:
                    lines.append(text[:240])
            elif isinstance(r, str):
                lines.append(r[:240])
    return lines[:8]


def _limitations(evidence_rows: list[Any], verifications: Any, session_id: str, cands: list[Any]) -> list[str]:
    out: list[str] = []
    for c in cands[:40]:
        try:
            vers = verifications.for_candidate(session_id, c.id)
        except Exception:
            continue
        for v in vers:
            checks = getattr(v, "checks", None) or {}
            if isinstance(checks, dict):
                for item in checks.get("limitations") or []:
                    text = str(item).strip()
                    if text and text not in out:
                        out.append(text)
    return out[:8]


def build_continuation_brief(
    parent_session_id: str,
    *,
    sessions: Any,
    candidates: Any,
    opportunities: Any,
    verifications: Any,
    notes: Any,
    browser_evidence: Any = None,
    reports: Any = None,
    trace: Any = None,
    thoughts: Any = None,
    adjacent_rows: list[dict[str, Any]] | None = None,
) -> ContinuationBrief:
    """Assemble the parent run's record. Read-only; never raises for a gap."""
    session = sessions.require(parent_session_id)
    cands: list[Any] = []
    try:
        cands = candidates.list(parent_session_id, limit=400)
    except Exception:
        cands = []
    opps: list[Any] = []
    try:
        opps = opportunities.list(parent_session_id, limit=60)
    except Exception:
        opps = []
    ev_rows: list[Any] = []
    if browser_evidence is not None:
        try:
            ev_rows = browser_evidence.list(parent_session_id, limit=800)
        except Exception:
            ev_rows = []

    brief = ContinuationBrief(
        parent_session_id=parent_session_id,
        parent_name=session.name,
        parent_status=session.status,
        parent_mode=session.mode,
        parent_objective=session.objective,
        parent_prompt=session.prompt,
        marketplaces=list(session.marketplaces or []),
        elapsed_minutes=round(float(session.elapsed_seconds or 0) / 60),
    )

    try:
        brief.counts = {k: int(v) for k, v in (sessions.counts(parent_session_id) or {}).items()}
    except Exception:
        brief.counts = {}

    if reports is not None:
        brief.report_title, brief.report_summary = _report_summary(reports, parent_session_id)

    # Opportunities carry the verification verdict — the report's portfolio.
    for o in opps[:OPPORTUNITY_LIMIT * 2]:
        brief.opportunities.append({
            "title": getattr(o, "title", ""),
            "niche": getattr(o, "niche", ""),
            "angle": getattr(o, "angle", ""),
            "confidence": round(float(getattr(o, "confidence", 0.0) or 0.0), 2),
            "verdict": _opp_verdict(verifications, parent_session_id, getattr(o, "candidate_id", "")),
        })

    for c in cands:
        status = str(getattr(c, "status", "") or "")
        row = {
            "niche": getattr(c, "niche", ""),
            "status": status,
            "marketplace": getattr(c, "marketplace", ""),
            "rationale": getattr(c, "rationale", ""),
        }
        if status in _REJECTED:
            meta = getattr(c, "meta", None) or {}
            reasons = meta.get("rejection_reasons") or meta.get("reasons") or []
            why = "; ".join(str(r) for r in reasons[:3]) if isinstance(reasons, list) else str(reasons)
            brief.rejected_candidates.append({"niche": row["niche"], "why": why or row["rationale"]})
        else:
            brief.candidates.append(row)

    brief.keywords, brief.categories = _keywords_and_categories(ev_rows)
    brief.competitors = _competitors(ev_rows)
    brief.consumer = _consumer_lines(ev_rows)
    brief.limitations = _limitations(ev_rows, verifications, parent_session_id, cands)

    if notes is not None:
        try:
            rows = notes.list(parent_session_id, limit=300)
        except Exception:
            rows = []
        for n in rows[-40:]:
            text = str(getattr(n, "text", "") or "").strip()
            if text:
                brief.notes.append({"phase": getattr(n, "phase", ""), "text": text})

    if thoughts is not None:
        brief.thoughts = _thought_excerpts(thoughts, parent_session_id)
    if trace is not None:
        brief.actions = _actions(trace, parent_session_id)

    # Open threads: the adjacency ledger rows ARE the unfunded discoveries.
    for r in (adjacent_rows or [])[:16]:
        label = str(r.get("niche") or r.get("title") or "").strip()
        if not label:
            continue
        why = str(r.get("why") or r.get("reason") or r.get("basis") or "").strip()
        brief.open_threads.append(f"{label}" + (f" — {why[:160]}" if why else ""))
    if not brief.open_threads and brief.rejected_candidates:
        brief.open_threads.append(
            "No explicit adjacency ledger was recorded; the rejected candidates "
            "above are the nearest documented dead ends."
        )
    return brief


def brief_digest(brief: ContinuationBrief) -> str:
    """One-line summary for the trace/event log (never the full brief)."""
    return (
        f"{brief.parent_name or brief.parent_session_id}: "
        f"{len(brief.opportunities)} opportunit(ies), "
        f"{len(brief.candidates)} kept / {len(brief.rejected_candidates)} rejected "
        f"candidate(s), {len(brief.keywords)} measured keyword(s), "
        f"{len(brief.actions)} traced action(s), {len(brief.thoughts)} thought(s)"
    )


def load_adjacent_rows(reports: Any, session_id: str) -> list[dict[str, Any]]:
    """The persisted adjacent ledger of the newest report that has one."""
    if reports is None:
        return []
    try:
        candidates = reports.list(session_id)
    except Exception:
        return []
    for rep in candidates:
        try:
            model = reports.read_model_json(rep)
        except Exception:
            continue
        if isinstance(model, dict) and model.get("adjacent_ledger"):
            return [r for r in model["adjacent_ledger"] if isinstance(r, dict)][:60]
    return []


def _safe_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        out = json.loads(raw or "{}")
    except Exception:
        return {}
    return out if isinstance(out, dict) else {}
