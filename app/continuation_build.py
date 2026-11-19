"""Read a finished (or paused) run out of the durable stores into a brief.

`app.continuation.py` owns the SHAPE and the rendering; this module owns the
READING. Every field comes from a stored row — nothing here summarizes with a
model, and nothing invents a figure. Sections that a session never produced
stay empty, and the renderer simply omits them.

Nothing is paged, capped or truncated here: a follow-up run inherits the whole
parent record however long that run was. The only bound on a stored value is
the one the writing store itself applies.
"""
from __future__ import annotations

import json
from typing import Any

from app.continuation import ContinuationBrief

# Candidate statuses that mean "this was researched and killed".
_REJECTED = {"rejected"}

# A hard ceiling only for the SQL LIMIT of a list query: "every row", not "the
# first page". (SQLite needs a number; this is not a context budget.)
_EVERY = 10_000_000


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


def _report_record(reports: Any, session_id: str) -> tuple[str, str, str]:
    """(title, full markdown, flat summary) of the newest report.

    The REPORT ITSELF is what a continuation must carry: the previous run's
    conclusions, in its own words, with its own numbers. It is handed over
    verbatim — condensing it was what made the follow-up feel like a new
    project with a brief instead of the same researcher continuing.
    """
    try:
        rep = reports.latest(session_id)
    except Exception:
        return "", "", ""
    if rep is None:
        return "", "", ""
    # The body lives on disk (`body_path`); the row itself carries no markdown.
    # Reading the attribute off the row silently yielded "" — so the previous
    # report never actually reached a follow-up run.
    try:
        body = str(reports.read_markdown(rep) or "")
    except Exception:
        body = ""
    flat = " ".join(body.split())
    return str(getattr(rep, "title", "") or ""), body, flat


def _thought_rows(thoughts: Any, session_id: str) -> list[dict[str, Any]]:
    """Every reasoning row the run produced, oldest first, uncondensed.

    The model's own thinking is the richest context a continuation can get:
    its reasoning about what it already ruled out, what it wanted to check
    next, and why a number looked wrong. Excerpts lost exactly that.
    """
    out: list[dict[str, Any]] = []
    try:
        if hasattr(thoughts, "every"):
            rows: Any = thoughts.every(session_id)
        else:  # duck-typed test double without the uncapped reader
            rows = []
            for row in thoughts.recent(session_id, limit=200):
                detail = thoughts.get(int(row["id"]))
                if detail is not None:
                    rows.append(detail)
        for t in rows:
            text = str(getattr(t, "detail", "") or "").strip()
            if not text:
                continue
            out.append({
                "label": str(getattr(t, "label", "") or "thinking"),
                "created_at": str(getattr(t, "created_at", "") or ""),
                "excerpt": text,
            })
    except Exception:
        return out
    return out


def _actions(trace: Any, session_id: str) -> list[dict[str, Any]]:
    """Every trace entry (the run's own account of what it did)."""
    try:
        rows = trace.every(session_id) if hasattr(trace, "every") else trace.tail(
            session_id, limit=400
        )
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


def _competitors(evidence_rows: list[Any]) -> list[dict[str, Any]]:
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
    return out


def _consumer_lines(evidence_rows: list[Any]) -> list[str]:
    """The review material the run captured, verbatim and whole.

    Both kinds are read: `review_research` is what the collector writes now,
    and `reviews` is what older runs stored. Review text is not truncated — a
    later run must read the complaint in full, not its first line.
    """
    lines: list[str] = []
    for e in evidence_rows:
        if getattr(e, "kind", "") not in ("review_research", "reviews"):
            continue
        data = getattr(e, "data", None)
        if not isinstance(data, dict):
            continue
        for r in data.get("reviews") or []:
            if isinstance(r, dict):
                text = str(r.get("text") or r.get("body") or r.get("review") or "").strip()
                if text:
                    lines.append(text)
            elif isinstance(r, str):
                lines.append(r)
    return lines


def _limitations(verifications: Any, session_id: str, cands: list[Any]) -> list[str]:
    out: list[str] = []
    for c in cands:
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
    return out


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
    owner_messages: Any = None,
    memory: Any = None,
    adjacent_rows: list[dict[str, Any]] | None = None,
) -> ContinuationBrief:
    """Assemble the parent run's record. Read-only; never raises for a gap."""
    session = sessions.require(parent_session_id)
    cands: list[Any] = []
    try:
        cands = candidates.list(parent_session_id, limit=_EVERY)
    except Exception:
        cands = []
    opps: list[Any] = []
    try:
        opps = opportunities.list(parent_session_id, limit=_EVERY)
    except Exception:
        opps = []
    ev_rows: list[Any] = []
    if browser_evidence is not None:
        try:
            ev_rows = browser_evidence.list(parent_session_id, limit=_EVERY)
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

    if owner_messages is not None:
        try:
            journal = owner_messages.journal(parent_session_id, limit=_EVERY)
        except Exception:
            journal = []
        brief.owner_messages = [
            {"created_at": getattr(m, "created_at", ""), "text": getattr(m, "text", "")}
            for m in journal
            if str(getattr(m, "text", "") or "").strip()
        ]

    if reports is not None:
        (
            brief.report_title,
            brief.report_markdown,
            brief.report_summary,
        ) = _report_record(reports, parent_session_id)

    # Opportunities carry the verification verdict — the report's portfolio.
    for o in opps:
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
            why = "; ".join(str(r) for r in reasons) if isinstance(reasons, list) else str(reasons)
            brief.rejected_candidates.append({"niche": row["niche"], "why": why or row["rationale"]})
        else:
            brief.candidates.append(row)

    brief.keywords, brief.categories = _keywords_and_categories(ev_rows)
    brief.competitors = _competitors(ev_rows)
    brief.consumer = _consumer_lines(ev_rows)
    brief.limitations = _limitations(verifications, parent_session_id, cands)

    if notes is not None:
        try:
            rows = notes.list(parent_session_id, limit=_EVERY)
        except Exception:
            rows = []
        for n in rows:
            text = str(getattr(n, "text", "") or "").strip()
            if text:
                brief.notes.append({"phase": getattr(n, "phase", ""), "text": text})

    if memory is not None:
        try:
            entries = memory.list(parent_session_id)
        except Exception:
            entries = []
        brief.memory = [
            {
                "kind": getattr(m, "kind", "") or "memory",
                "phase": getattr(m, "phase", ""),
                "created_at": getattr(m, "created_at", ""),
                "text": str(getattr(m, "text", "") or ""),
            }
            for m in entries
            if str(getattr(m, "text", "") or "").strip()
        ]

    if thoughts is not None:
        brief.thoughts = _thought_rows(thoughts, parent_session_id)
    if trace is not None:
        brief.actions = _actions(trace, parent_session_id)

    # Open threads: the adjacency ledger rows ARE the unfunded discoveries.
    for r in (adjacent_rows or []):
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


def seed_candidate_ledger(
    parent_session_id: str,
    session_id: str,
    *,
    candidates: Any,
) -> list[dict[str, Any]]:
    """Give the follow-up run the previous run's live candidate ledger.

    The record tells the model what the previous run concluded; this makes the
    child's own machinery agree with it. Without rows here, every downstream
    layer (headroom ordering, adjacency, the CANDIDATES digest, synthesis
    linking) sees an empty investigation and the run really does start from
    nothing — which is exactly how a follow-up felt like a new research.

    Rows are seeded as they were FOUND, never as VERIFIED: a verdict belongs to
    the evidence that produced it, and this run has not reproduced that
    evidence yet. The previous status/score ride along in ``continued_from`` so
    nothing is lost and nothing is falsely claimed.
    """
    out: list[dict[str, Any]] = []
    try:
        rows = candidates.list(parent_session_id, limit=_EVERY)
    except Exception:
        return out
    for c in rows:
        if str(getattr(c, "status", "")) == "rejected":
            # Rejections stay rejected; the record carries them. Copying them
            # as rows would count dead ends as this run's unresolved work.
            continue
        niche = str(getattr(c, "niche", "") or "").strip()
        if not niche:
            continue
        parent_meta = getattr(c, "meta", None) or {}
        meta: dict[str, Any] = {
            "continued_from": {
                "session_id": parent_session_id,
                "candidate_id": getattr(c, "id", ""),
                "status": str(getattr(c, "status", "") or ""),
                "score": float(getattr(c, "score", 0.0) or 0.0),
            }
        }
        for key in ("parent_market", "chain"):
            if parent_meta.get(key):
                meta[key] = parent_meta[key]
        try:
            fresh = candidates.create(
                session_id=session_id,
                niche=niche,
                marketplace=str(getattr(c, "marketplace", "") or ""),
                rationale=str(getattr(c, "rationale", "") or ""),
                score=float(getattr(c, "score", 0.0) or 0.0),
                meta=meta,
            )
        except Exception:
            continue
        out.append({
            "candidate_id": fresh.id,
            "niche": niche,
            "previous_status": meta["continued_from"]["status"],
        })
    return out


def seed_memory(
    parent_session_id: str,
    session_id: str,
    *,
    memory: Any,
) -> list[dict[str, Any]]:
    """Copy the previous run's long-running memory into the follow-up run.

    Memory is the one thing a continuation must not re-derive: the parent's
    entries become the child's own first entries, verbatim and in order, with
    their origin recorded. The child then keeps appending to the same journal,
    so an investigation's memory grows across runs instead of being replaced
    by a brief about them.
    """
    out: list[dict[str, Any]] = []
    if memory is None:
        return out
    try:
        entries = memory.list(parent_session_id)
    except Exception:
        return out
    for m in entries:
        text = str(getattr(m, "text", "") or "")
        if not text.strip():
            continue
        try:
            fresh = memory.append(
                session_id,
                text=text,
                phase=str(getattr(m, "phase", "") or ""),
                kind=str(getattr(m, "kind", "") or "memory"),
                origin=parent_session_id,
            )
        except Exception:
            continue
        out.append({"id": fresh.id, "kind": fresh.kind, "chars": len(fresh.text)})
    return out


def brief_digest(brief: ContinuationBrief) -> str:
    """One-line summary for the trace/event log (never the full brief)."""
    return (
        f"{brief.parent_name or brief.parent_session_id}: "
        f"{len(brief.opportunities)} opportunit(ies), "
        f"{len(brief.candidates)} kept / {len(brief.rejected_candidates)} rejected "
        f"candidate(s), {len(brief.keywords)} measured keyword(s), "
        f"{len(brief.actions)} traced action(s), {len(brief.thoughts)} thought(s), "
        f"{len(brief.memory)} memory entry(ies)"
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
            return [r for r in model["adjacent_ledger"] if isinstance(r, dict)]
    return []


def prior_run_records(
    session_id: str,
    *,
    sessions: Any,
    candidates: Any,
    opportunities: Any,
    verifications: Any,
    notes: Any,
    browser_evidence: Any,
    reports: Any,
    trace: Any = None,
    thoughts: Any = None,
    owner_messages: Any = None,
    memory: Any = None,
) -> list[dict[str, Any]]:
    """The FULL record of every earlier run in this session's chain.

    A follow-up's report — and a chat about a report — must read like one
    analyst who has followed the market from the first discovery: not a
    summary of the previous report, but the earlier runs themselves, oldest
    first, bounded only by the number of runs in the chain (cycles are
    guarded). Shared by the report writer and the report chat.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = {session_id}
    try:
        current = sessions.require(session_id).parent_session_id
    except Exception:
        return out
    while current and current not in seen:
        seen.add(current)
        try:
            brief = build_continuation_brief(
                current,
                sessions=sessions,
                candidates=candidates,
                opportunities=opportunities,
                verifications=verifications,
                notes=notes,
                browser_evidence=browser_evidence,
                reports=reports,
                trace=trace,
                thoughts=thoughts,
                owner_messages=owner_messages,
                memory=memory,
                adjacent_rows=load_adjacent_rows(reports, current),
            )
        except Exception:
            break
        out.append({
            "session_id": current,
            "name": brief.parent_name,
            "status": brief.parent_status,
            "record": brief.record_text(),
            "report_title": brief.report_title,
        })
        try:
            current = sessions.require(current).parent_session_id
        except Exception:
            break
    out.reverse()  # oldest first: the chain as it actually happened
    return out


def _safe_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        out = json.loads(raw or "{}")
    except Exception:
        return {}
    return out if isinstance(out, dict) else {}
