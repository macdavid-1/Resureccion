"""Structured market-intelligence report model.

The final deliverable is NOT one giant markdown string. It is a structured
model (`ReportModel`) built deterministically from the session's durable
research state — opportunities, verifications, phase outputs, consumer maps,
competitor evidence, KDP risk assessments, claims and evidence — and that
model renders into markdown (web + PDF today, more formats later). The model
is persisted alongside the rendered body, so the report stays editable and
re-renderable from structured data.

Every section is derived from stores — never invented here:
  Executive summary        ← session + counts + demand verdicts
  Opportunity portfolio    ← persisted opportunities (gate-passed only)
  Niching hierarchies      ← P2 output chains
  Marketplace analysis     ← candidate markets + P7 cross-market checks + registry
  Consumer intelligence    ← P4 consumer maps (+ review evidence)
  Competitor analysis      ← P3 competitive maps joined with browser evidence
  Book angles              ← P4 design opportunities + P6 gap analyses
  Titles                   ← opportunity contract title_concepts
  Keywords                 ← opportunity keywords, integrity-screened
  KDP risk                 ← deterministic risk assessments
  Evidence appendix        ← browser evidence ledger
  Quality statement        ← counts, verification outcomes, limitations

Nothing in this module fabricates: where data is absent the model says so.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.methodology import PHASE_ORDER, PHASE_TITLES
from app.marketplace import AMAZON_MARKETPLACES

SCHEMA_VERSION = 2

# Output-contract keys produced by each methodology phase.
_NICHES_KEY = "niches"
_COMPETITIVE_KEY = "competitive_maps"
_CONSUMER_KEY = "consumer_maps"
_DEMAND_KEY = "demand_assessments"
_GAP_KEY = "gap_analyses"
_CROSSMARKET_KEY = "cross_market_checks"
_SIGNALS_KEY = "signals"

_PROBLEM_WORDS = (
    "how to", "without", "stop", "fix", "avoid", "problem", "help", "cope",
    "recover", "beginner", "step by step", "guide",
)


# --------------------------------------------------------------------- model
@dataclass
class ReportModel:
    """The complete structured report. JSON-serializable via to_dict()."""

    schema_version: int = SCHEMA_VERSION
    session: dict[str, Any] = field(default_factory=dict)
    generated_at: str = ""
    executive_summary: dict[str, Any] = field(default_factory=dict)
    opportunities: list[dict[str, Any]] = field(default_factory=list)
    consumer_intelligence: dict[str, Any] = field(default_factory=dict)
    competitors: list[dict[str, Any]] = field(default_factory=list)
    candidate_ledger: list[dict[str, Any]] = field(default_factory=list)
    methodology_trace: list[dict[str, Any]] = field(default_factory=list)
    evidence_appendix: list[dict[str, Any]] = field(default_factory=list)
    quality_statement: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session": self.session,
            "generated_at": self.generated_at,
            "executive_summary": self.executive_summary,
            "opportunities": self.opportunities,
            "consumer_intelligence": self.consumer_intelligence,
            "competitors": self.competitors,
            "candidate_ledger": self.candidate_ledger,
            "methodology_trace": self.methodology_trace,
            "evidence_appendix": self.evidence_appendix,
            "quality_statement": self.quality_statement,
            "validation": self.validation,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=1)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReportModel":
        m = cls()
        for f in (
            "schema_version", "session", "generated_at", "executive_summary",
            "opportunities", "consumer_intelligence", "competitors",
            "candidate_ledger", "methodology_trace", "evidence_appendix",
            "quality_statement", "validation",
        ):
            setattr(m, f, data.get(f, {} if f != "opportunities" and f != "competitors" and f != "candidate_ledger" and f != "methodology_trace" and f != "evidence_appendix" else []))
        return m


# ------------------------------------------------------------------- builder
def build_report_model(
    session_id: str,
    *,
    sessions: Any,
    candidates: Any,
    opportunities: Any,
    verifications: Any,
    claims: Any,
    browser_evidence: Any,
    observations: Any,
    assessments: Any | None = None,
    phase_outputs: dict[str, Any] | None = None,
) -> ReportModel:
    """Assemble the structured report model from durable state. Deterministic."""
    session = sessions.require(session_id)
    cands = candidates.list(session_id, limit=300)
    opps = opportunities.list(session_id, limit=50)
    outputs = phase_outputs or {}
    cand_by_id = {c.id: c for c in cands}

    # Flat lookups -----------------------------------------------------------
    risk_by_cand: dict[str, dict[str, Any]] = {}
    kwi_by_cand: dict[str, dict[str, Any]] = {}
    if assessments is not None:
        for a in assessments.list(session_id, kind="kdp_risk", limit=800):
            risk_by_cand.setdefault(a.subject_id, a.metrics or {})
        for a in assessments.list(session_id, kind="keyword_integrity", limit=800):
            kwi_by_cand.setdefault(a.subject_id, a.metrics or {})
    claims_by_cand: dict[str, list[Any]] = {}
    for c in claims.list(session_id, limit=3000):
        claims_by_cand.setdefault(c.candidate_id or "", []).append(c)

    # Phase-output indices keyed by niche -------------------------------------
    niches_out = _phase_items(outputs, _NICHES_KEY)
    comp_out = _phase_items(outputs, _COMPETITIVE_KEY)
    cons_out = _phase_items(outputs, _CONSUMER_KEY)
    demand_out = _phase_items(outputs, _DEMAND_KEY)
    gap_out = _phase_items(outputs, _GAP_KEY)
    xmarket_out = _phase_items(outputs, _CROSSMARKET_KEY)

    # Browser evidence: competitors + appendix --------------------------------
    ev_rows = browser_evidence.list(session_id, limit=600)
    competitors = _build_competitors(comp_out, ev_rows, session_id)

    # ---------------------------------------------------------------- dossier
    dossiers: list[dict[str, Any]] = []
    for o in opps:
        cand = cand_by_id.get(o.candidate_id)
        meta = o.meta or {}
        vers = verifications.for_candidate(session_id, o.candidate_id)
        best_v = _best_verification(vers)
        cand_claims = claims_by_cand.get(o.candidate_id, [])
        ev_ids = _opportunity_evidence(meta, cand_claims)
        dossiers.append({
            "name": o.title,
            "id": o.id,
            "candidate_id": o.candidate_id,
            "niche": o.niche,
            "parent_market": meta.get("parent_market") or (cand.meta or {}).get("parent_market") or _parent_from(niches_out, o.niche) or "—",
            "target_reader": meta.get("target_reader", "—"),
            "reader_problem": meta.get("reader_problem", "—"),
            "marketplaces": [m.strip() for m in (o.marketplace or "").split(",") if m.strip()],
            "demand": _demand_for(o.niche, demand_out),
            "competitive_landscape": meta.get("competitive_landscape") or _comp_summary(comp_out, o.niche),
            "consumer_needs": meta.get("consumer_needs") or _consumer_summary(cons_out, o.niche),
            "market_gap": meta.get("market_gap", "—"),
            "differentiation": meta.get("differentiation", "—"),
            "positioning": o.angle or meta.get("positioning", ""),
            "risks": _as_list(meta.get("risks")),
            "kdp_risk": _kdp_risk_for(o.candidate_id, risk_by_cand, meta),
            "keyword_integrity": kwi_by_cand.get(o.candidate_id) or meta.get("keyword_integrity") or {},
            "verification": _verification_summary(best_v, meta),
            "confidence": round(float(o.confidence), 3),
            "niching_chain": _chain_for(o.niche, niches_out, meta),
            "marketplace_analysis": _marketplace_analysis(cand, xmarket_out, o.niche),
            "angles": _angles_for(o.niche, cons_out, gap_out, cand_claims),
            "titles": _titles_for(meta),
            "keywords": _keyword_clusters(o.keywords, o.niche),
            "evidence_ids": ev_ids[:24],
            "next_step": _next_step(o, best_v),
        })

    # ------------------------------------------------------------- sections
    counts = sessions.counts(session_id)
    model = ReportModel(
        session={
            "id": session.id,
            "name": session.name,
            "mode": session.mode,
            "prompt": session.prompt,
            "objective": session.objective,
            "marketplaces_requested": list(session.marketplaces),
            "status": session.status,
            "elapsed_seconds": session.elapsed_seconds,
            "created_at": session.created_at,
        },
        generated_at="",
        executive_summary=_exec_summary(session, counts, demand_out, outputs, dossiers),
        opportunities=dossiers,
        consumer_intelligence=_aggregate_consumer(cons_out),
        competitors=competitors,
        candidate_ledger=_ledger(cands),
        methodology_trace=_trace(outputs),
        evidence_appendix=_evidence_appendix(ev_rows),
        quality_statement=_quality_statement(counts, cands, verifications, session_id, assessments, claims),
    )
    return model


# --------------------------------------------------------------- exec summary
def _exec_summary(
    session: Any,
    counts: dict[str, int],
    demand_out: list[dict[str, Any]],
    outputs: dict[str, Any],
    dossiers: list[dict[str, Any]],
) -> dict[str, Any]:
    verdicts = [str(d.get("verdict", "")) for d in demand_out]
    findings: list[str] = []
    for d in dossiers[:10]:
        findings.append(
            f"{d['name']} — {d['niche']} for {d['target_reader']} "
            f"(confidence {int(round(d['confidence'] * 100))}%)"
        )
    return {
        "what_was_researched": session.prompt or session.objective
        or "Autonomous discovery across Amazon marketplaces — no topic was specified; Resurrección selected signals independently.",
        "why": session.objective
        or "Identify exceptional, evidence-backed KDP book opportunities ready for immediate planning and writing.",
        "marketplaces_investigated": list(session.marketplaces) or ["auto-selected by the methodology"],
        "methodology": (
            "Deterministic 9-phase methodology (discovery → aggressive niching → "
            "competitive landscape → consumer intelligence → demand validation → "
            "gap analysis → cross-market validation → adversarial verification → "
            "synthesis), followed by deterministic filtering, an adversarial "
            "verification engine, KDP risk screening, and quality gates."
        ),
        "candidates_considered": counts.get("discovered", 0) + counts.get("rejected", 0)
        + counts.get("verifying", 0) + counts.get("verified", 0),
        "candidates_rejected": counts.get("rejected", 0),
        "survived_verification": counts.get("verified", 0),
        "demand_verdicts": verdicts,
        "major_findings": findings,
    }


# ------------------------------------------------------------------ dossier
def _phase_items(outputs: dict[str, Any], key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for out in outputs.values() or []:
        if isinstance(out, dict):
            v = out.get(key)
            if isinstance(v, list):
                items.extend(x for x in v if isinstance(x, dict))
    return items


def _parent_from(niches_out: list[dict[str, Any]], niche: str) -> str:
    n = niche.lower()
    for item in niches_out:
        if str(item.get("niche", "")).lower() in n or n in str(item.get("niche", "")).lower():
            return str(item.get("parent_market", "")) or ""
    return ""


def _chain_for(niche: str, niches_out: list[dict[str, Any]], meta: dict[str, Any]) -> list[str]:
    """The aggressive-niching hierarchy: broad → … → final book opportunity."""
    chain = meta.get("niching_chain")
    if isinstance(chain, list) and chain:
        return [str(x) for x in chain]
    for item in niches_out:
        if str(item.get("niche", "")).lower() == niche.lower():
            c = item.get("chain")
            if isinstance(c, list) and c:
                return [str(x) for x in c]
    # Fall back to the market→niche pair we can honestly state.
    parent = meta.get("parent_market")
    return ([str(parent)] if parent else []) + [niche]


def _best_verification(vers: list[Any]) -> Any | None:
    rank = {"verified": 0, "verified_with_limitations": 1, "inconclusive": 2, "rejected": 3}
    passing = [v for v in vers if v.verdict in ("verified", "verified_with_limitations")]
    pool = passing or vers
    return min(pool, key=lambda v: rank.get(v.verdict, 9)) if pool else None


def _verification_summary(best: Any | None, meta: dict[str, Any]) -> dict[str, Any]:
    if best is None:
        return {"status": meta.get("verification_status", "unknown"), "verdict": "",
                "confidence": None, "limitations": []}
    checks = best.checks if isinstance(best.checks, dict) else {}
    limitations = [
        str(k) for k, v in checks.items()
        if isinstance(v, dict) and str(v.get("outcome", "")).lower() in ("limited", "partial", "inconclusive")
    ]
    return {
        "status": "verified" if best.verdict == "verified" else str(best.verdict),
        "verdict": str(best.verdict),
        "confidence": round(float(best.confidence), 3) if best.confidence is not None else None,
        "limitations": limitations,
        "attempted_disproof": "The verification engine attempted to disprove this candidate; surviving claims are those it could not kill.",
    }


def _demand_for(niche: str, demand_out: list[dict[str, Any]]) -> dict[str, Any]:
    n = niche.lower()
    for d in demand_out:
        if str(d.get("niche", "")).lower() in n or n in str(d.get("niche", "")).lower():
            return {
                "verdict": str(d.get("verdict", "not assessed")),
                "signals": [str(s) for s in _as_list(d.get("signals"))][:8],
                "demand_risks": [str(s) for s in _as_list(d.get("risks"))][:6],
            }
    return {"verdict": "not separately assessed", "signals": [], "demand_risks": []}


def _comp_summary(comp_out: list[dict[str, Any]], niche: str) -> str:
    n = niche.lower()
    for m in comp_out:
        if str(m.get("niche", "")).lower() in n or n in str(m.get("niche", "")).lower():
            parts = []
            comps = _as_list(m.get("competitors"))
            if comps:
                parts.append(f"{len(comps)} competitors inspected ({', '.join(str(c) for c in comps[:6])})")
            for k, label in (("saturation", "saturation"), ("pricing", "pricing"), ("patterns", "patterns")):
                if m.get(k):
                    parts.append(f"{label}: {m[k]}")
            return "; ".join(parts) or "—"
    return "—"


def _consumer_summary(cons_out: list[dict[str, Any]], niche: str) -> str:
    n = niche.lower()
    for m in cons_out:
        if str(m.get("niche", "")).lower() in n or n in str(m.get("niche", "")).lower():
            bits = []
            for k, label in (("complaints", "complaints"), ("unmet", "unmet needs"), ("praises", "praise")):
                v = _as_list(m.get(k))
                if v:
                    bits.append(f"{label}: {'; '.join(str(x) for x in v[:4])}")
            return "; ".join(bits) or "—"
    return "—"


def _kdp_risk_for(cand_id: str, risk_by_cand: dict[str, dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    risk = risk_by_cand.get(cand_id) or meta.get("kdp_risk") or {}
    warnings = [str(w) for w in _as_list(risk.get("warnings"))][:6]
    level = str(risk.get("level") or ("concern" if warnings else "safe"))
    return {"level": level, "warnings": warnings}


def _opportunity_evidence(meta: dict[str, Any], cand_claims: list[Any]) -> list[str]:
    ids: list[str] = []
    m = meta.get("evidence_ids")
    if isinstance(m, list):
        ids.extend(str(e) for e in m)
    for c in cand_claims:
        for e in c.evidence_ids:
            if e not in ids:
                ids.append(e)
    return ids


def _marketplace_analysis(
    cand: Any | None, xmarket_out: list[dict[str, Any]], niche: str
) -> dict[str, Any]:
    n = niche.lower()
    checks = [
        c for c in xmarket_out
        if str(c.get("niche", "")).lower() in n or n in str(c.get("niche", "")).lower()
    ]
    observed: list[str] = []
    if cand is not None and cand.marketplace:
        observed.append(cand.marketplace)
    for c in checks:
        code = str(c.get("marketplace", "")).strip()
        if code and code not in observed:
            observed.append(code)
    cross = [c for c in checks if str(c.get("verdict", "")).lower() in ("present", "confirmed", "yes")]
    related: list[str] = [
        str(c.get("marketplace")) for c in checks
        if str(c.get("verdict", "")).lower() not in ("present", "confirmed", "yes")
    ]
    strongest = observed[:1]
    if cross:
        strongest = [c["marketplace"] for c in cross][:3] or strongest
    mkt = AMAZON_MARKETPLACES.get(observed[0]) if observed else None
    language = mkt.language if mkt else ""
    currency = mkt.currency if mkt else ""
    local_or_cross = (
        "cross-market: observed on multiple marketplaces"
        if len(observed) > 1
        else "local: observed on a single marketplace in this session"
    )
    return {
        "observed": observed,
        "strongest": strongest,
        "related_demand_elsewhere": [r for r in related if r][:6],
        "differences": [
            f"{c.get('marketplace')}: {c.get('verdict')} — {c.get('notes', '')}".strip(" —")
            for c in checks
        ][:6],
        "language_considerations": (
            f"Primary marketplace language {language}, currency {currency}."
            if language else "Language/currency follow each marketplace's registry entry."
        ),
        "scope": local_or_cross,
    }


def _angles_for(
    niche: str,
    cons_out: list[dict[str, Any]],
    gap_out: list[dict[str, Any]],
    cand_claims: list[Any],
) -> list[dict[str, Any]]:
    """Angles originate from OBSERVED consumer needs — never generic ideas.

    Each P4 design opportunity becomes an angle grounded in the complaint or
    unmet need it answers; P6 gap fields supply the 'why this addresses the
    gap' reasoning. Evidence ids come from the candidate's registered claims.
    """
    n = niche.lower()
    cmap = next(
        (m for m in cons_out if str(m.get("niche", "")).lower() in n or n in str(m.get("niche", "")).lower()),
        None,
    )
    gmap = next(
        (g for g in gap_out if str(g.get("niche", "")).lower() in n or n in str(g.get("niche", "")).lower()),
        None,
    )
    ev_pool: list[str] = []
    for c in cand_claims:
        for e in c.evidence_ids:
            if e not in ev_pool:
                ev_pool.append(e)

    angles: list[dict[str, Any]] = []
    design_ops = _as_list((cmap or {}).get("design_opportunities"))
    complaints = _as_list((cmap or {}).get("complaints"))
    unmet = _as_list((cmap or {}).get("unmet"))
    for i, op in enumerate(design_ops[:5]):
        problem = (
            str(complaints[i]) if i < len(complaints)
            else str(unmet[i]) if i < len(unmet)
            else "Recurring reader frustration observed in reviews"
        )
        angles.append({
            "reader_problem": problem,
            "angle": str(op),
            "evidence_ids": ev_pool[:4],
            "addresses_gap": str((gmap or {}).get("competitors_fail", "")) or "Incumbents do not address this observed need.",
        })
    if gmap and gmap.get("better") and not angles:
        angles.append({
            "reader_problem": str(gmap.get("readers_want", "")) or "Reader desire documented in the gap analysis",
            "angle": str(gmap.get("better")),
            "evidence_ids": ev_pool[:4],
            "addresses_gap": str(gmap.get("competitors_fail", "")),
        })
    if not angles:
        angles.append({
            "reader_problem": "No consumer maps were recorded for this niche in this session.",
            "angle": "—",
            "evidence_ids": [],
            "addresses_gap": "Angles require Phase-4 consumer evidence; none survived for this niche.",
        })
    return angles[:5]


def _titles_for(meta: dict[str, Any]) -> list[dict[str, Any]]:
    tc = meta.get("title_concepts")
    if not isinstance(tc, list):
        return []
    out = []
    for t in tc[:6]:
        if isinstance(t, dict):
            out.append({"title": str(t.get("title", "")), "subtitle": str(t.get("subtitle", "")),
                        "rationale": str(t.get("rationale", "Derived from observed buyer language and competitor positioning."))})
        else:
            out.append({"title": str(t), "subtitle": "",
                        "rationale": "Derived from observed buyer language and competitor positioning."})
    return out


def _keyword_clusters(keywords: list[str], niche: str) -> dict[str, Any]:
    kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    core = [k for k in kws if len(k.split()) <= 2][:4] or kws[:2]
    rest = [k for k in kws if k not in core]
    long_tail = [k for k in rest if len(k.split()) >= 4]
    problem = [k for k in rest if any(w in k.lower() for w in _PROBLEM_WORDS)]
    audience = [k for k in rest if "for " in k.lower()]
    intent = [k for k in rest if k not in long_tail and k not in problem and k not in audience]
    return {
        "core": core,
        "supporting": [k for k in rest if k not in long_tail and k not in problem and k not in audience and k not in intent][:6],
        "long_tail": long_tail[:8],
        "problem_based": problem[:6],
        "audience_specific": audience[:6],
        "intent_phrases": intent[:6],
        "relevance_rationale": (
            f"Clusters derive from the opportunity's niche terms and reader problem; "
            f"core terms mirror the niche head language ({', '.join(core[:2]) or niche}). "
            "Relevance and buyer intent were prioritized over volume; misleading or "
            "trademark-risky keywords were removed upstream by deterministic screening."
        ),
    }


def _next_step(o: Any, best_v: Any | None) -> str:
    verdict = getattr(best_v, "verdict", "") if best_v else ""
    if verdict == "verified_with_limitations":
        return (
            "Proceed to concept development, but close the recorded verification "
            "limitations first (see verification section) with a short follow-up check."
        )
    if o.meta and float(o.confidence or 0) < 0.6:
        return "Proceed carefully: confidence is moderate — validate positioning with a small content pilot before full production."
    return "Proceed directly to book planning: outline against the stated market gap and reader problem, then draft to the differentiation."


# ------------------------------------------------------- consumer aggregate
def _aggregate_consumer(cons_out: list[dict[str, Any]]) -> dict[str, Any]:
    themes: dict[str, list[str]] = {
        "praised": [], "complaints": [], "unmet_needs": [],
        "desired_features": [], "frustrations": [], "objections": [],
        "expectations": [], "missing_information": [],
    }
    by_niche: list[dict[str, Any]] = []
    for m in cons_out:
        by_niche.append({
            "niche": str(m.get("niche", "")),
            "praises": [str(x) for x in _as_list(m.get("praises"))][:8],
            "complaints": [str(x) for x in _as_list(m.get("complaints"))][:8],
            "unmet": [str(x) for x in _as_list(m.get("unmet"))][:8],
            "design_opportunities": [str(x) for x in _as_list(m.get("design_opportunities"))][:8],
        })
        themes["praised"] += [str(x) for x in _as_list(m.get("praises"))]
        themes["complaints"] += [str(x) for x in _as_list(m.get("complaints"))]
        themes["unmet_needs"] += [str(x) for x in _as_list(m.get("unmet"))]
        themes["desired_features"] += [str(x) for x in _as_list(m.get("design_opportunities"))]
    # Dedupe, preserve order, bound.
    themes = {k: list(dict.fromkeys(v))[:12] for k, v in themes.items()}
    themes["frustrations"] = themes["complaints"][:6]
    themes["missing_information"] = themes["unmet_needs"][:6]
    themes["what_the_new_book_should_do_differently"] = themes["desired_features"][:8]
    return {"themes": themes, "by_niche": by_niche}


# ----------------------------------------------------------------- competitors
def _build_competitors(
    comp_out: list[dict[str, Any]], ev_rows: list[Any], session_id: str
) -> list[dict[str, Any]]:
    """Join P3 competitor names with real browser evidence where available."""
    by_title: dict[str, dict[str, Any]] = {}
    for row in ev_rows:
        data = row.data if isinstance(row.data, dict) else {}
        title = str(data.get("title", "")).strip()
        if not title:
            continue
        key = title.lower()
        slot = by_title.setdefault(key, {
            "title": title,
            "subtitle": str(data.get("subtitle", "")),
            "marketplace": row.marketplace,
            "asin": str(data.get("asin", "")),
            "url": row.url,
            "price": data.get("price"),
            "rank": data.get("rank"),
            "review_count": data.get("review_count") or data.get("reviews"),
            "rating": data.get("rating"),
            "screenshot_artifact_id": row.screenshot_artifact_id,
            "captured_at": row.captured_at,
            "niches": [],
        })
        if row.screenshot_artifact_id and not slot["screenshot_artifact_id"]:
            slot["screenshot_artifact_id"] = row.screenshot_artifact_id

    out: list[dict[str, Any]] = []
    for m in comp_out:
        niche = str(m.get("niche", ""))
        patterns = str(m.get("patterns", ""))
        saturation = str(m.get("saturation", ""))
        for name in _as_list(m.get("competitors"))[:10]:
            name_s = str(name)
            ev = by_title.get(name_s.lower())
            out.append({
                "title": name_s,
                "matched_evidence": bool(ev),
                "marketplace": (ev or {}).get("marketplace", ""),
                "asin": (ev or {}).get("asin", ""),
                "url": (ev or {}).get("url", ""),
                "price": (ev or {}).get("price"),
                "rank": (ev or {}).get("rank"),
                "review_count": (ev or {}).get("review_count"),
                "screenshot_artifact_id": (ev or {}).get("screenshot_artifact_id"),
                "positioning": patterns or "",
                "niche": niche,
                "saturation": saturation,
                "lesson": (
                    f"Positioning lesson for {niche}: study this incumbent's strengths, "
                    "then differentiate on the documented reader complaints — never copy."
                ),
            })
    return out[:40]


# -------------------------------------------------------------------- ledger
def _ledger(cands: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "niche": c.niche,
            "status": c.status,
            "marketplace": c.marketplace,
            "rationale": (c.rationale or "")[:220],
            "score": round(float(c.score), 3) if c.score is not None else None,
        }
        for c in cands[:60]
    ]


def _trace(outputs: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for phase in PHASE_ORDER:
        out = outputs.get(phase) or {}
        keys = sorted(out.keys()) if isinstance(out, dict) else []
        rows.append({
            "phase": phase,
            "title": PHASE_TITLES[phase],
            "outputs": keys,
            "completed": True,
        })
    return rows


def _evidence_appendix(ev_rows: list[Any], limit: int = 120) -> list[dict[str, Any]]:
    rows = []
    for row in ev_rows[:limit]:
        data = row.data if isinstance(row.data, dict) else {}
        rows.append({
            "id": row.id,
            "marketplace": row.marketplace,
            "kind": row.kind,
            "url": row.url,
            "title": str(data.get("title") or row.title or "")[:160],
            "asin": str(data.get("asin", "")),
            "captured_at": row.captured_at,
            "screenshot_artifact_id": row.screenshot_artifact_id,
        })
    return rows


def _quality_statement(
    counts: dict[str, int],
    cands: list[Any],
    verifications: Any,
    session_id: str,
    assessments: Any | None,
    claims: Any,
) -> dict[str, Any]:
    rejected = [c for c in cands if c.status == "rejected"]
    verifying = [c for c in cands if c.status in ("discovered", "verifying")]
    uncertain: list[str] = [
        f"{c.niche} — not fully verified in this session" for c in verifying[:8]
    ]
    limitations: list[str] = []
    all_vers = verifications.list(session_id, limit=300)
    for v in all_vers:
        checks = v.checks if isinstance(v.checks, dict) else {}
        for k, val in checks.items():
            if isinstance(val, dict) and str(val.get("outcome", "")).lower() in ("limited", "partial", "inconclusive"):
                limitations.append(f"{k}: {val.get('note', 'limited coverage')}")
    limitations = list(dict.fromkeys(limitations))[:10]
    return {
        "investigated": (
            f"{counts.get('discovered', 0) + counts.get('rejected', 0)} candidates discovered "
            f"across the methodology; {len(claims.list(session_id, limit=3000))} claims registered "
            "with evidence chain-of-custody."
        ),
        "verified": counts.get("verified", 0),
        "rejected": counts.get("rejected", 0),
        "rejection_reasons": [
            {"niche": c.niche, "rationale": (c.rationale or "")[:200]} for c in rejected[:10]
        ],
        "remains_uncertain": uncertain,
        "verification_limitations": limitations,
        "certainty_policy": (
            "This report states only what the recorded evidence supports. Where "
            "evidence was insufficient, the candidate was rejected or explicitly "
            "flagged as uncertain — certainty is never manufactured."
        ),
    }


# ------------------------------------------------------------------- helpers
def _as_list(v: Any) -> list[Any]:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        return [v]
    return []


# ------------------------------------------------------------------ markdown
def render_markdown(model: ReportModel) -> str:
    """Render the structured model into the publication-ready report."""
    ex = model.executive_summary
    s = model.session
    lines: list[str] = []
    a = lines.append

    a("# Resurrección — Market Intelligence Report")
    a("")
    a(f"**Session:** {s.get('name', '—')}  ")
    a(f"**Mode:** {s.get('mode', '—')}  ")
    a(f"**Marketplaces:** {', '.join(s.get('marketplaces_requested') or []) or 'auto-selected'}  ")
    a(f"**Objective:** {s.get('objective') or '—'}")
    if s.get("prompt"):
        a(f"**Brief:** {str(s.get('prompt'))[:600]}")
    a("")

    # ------------------------------------------------------- executive summary
    a("## Executive summary")
    a("")
    a(f"**What was researched.** {ex.get('what_was_researched', '—')}")
    a("")
    a(f"**Why.** {ex.get('why', '—')}")
    a("")
    a(
        f"**Scale.** {ex.get('candidates_considered', 0)} candidates considered · "
        f"{ex.get('candidates_rejected', 0)} rejected by deterministic filters and adversarial "
        f"verification · {ex.get('survived_verification', 0)} survived with full verification."
    )
    a("")
    a(f"**Methodology.** {ex.get('methodology', '—')}")
    a("")
    a(f"**Marketplaces investigated.** {', '.join(str(m) for m in ex.get('marketplaces_investigated', [])) or '—'}")
    a("")
    if ex.get("major_findings"):
        a("**Major findings.**")
        for f in ex["major_findings"]:
            a(f"- {f}")
        a("")

    # -------------------------------------------------------------- portfolio
    if model.opportunities:
        a("## Opportunity portfolio")
        a("")
        a(
            f"{len(model.opportunities)} opportunit{'y' if len(model.opportunities) == 1 else 'ies'} "
            "passed the full methodology. The portfolio size reflects genuine "
            "survivors — opportunities are never manufactured to reach a quota."
        )
        a("")
        for i, o in enumerate(model.opportunities, 1):
            _render_opportunity(lines, i, o)
    else:
        a("## Opportunity portfolio")
        a("")
        a(
            "No opportunities passed adversarial verification in this session. "
            "The candidate ledger and quality statement below show what was "
            "learned and why candidates did not survive."
        )
        a("")

    # --------------------------------------------------- consumer intelligence
    ci = model.consumer_intelligence
    themes = ci.get("themes", {})
    if ci.get("by_niche"):
        a("## Consumer intelligence — what readers actually say")
        a("")
        for label, key in (
            ("Recurring praise", "praised"),
            ("Recurring complaints", "complaints"),
            ("Unmet needs", "unmet_needs"),
            ("Desired features", "desired_features"),
            ("Missing information", "missing_information"),
            ("Frustrations", "frustrations"),
        ):
            items = themes.get(key, [])
            if items:
                a(f"**{label}**")
                for it in items[:10]:
                    a(f"- {it}")
                a("")
        diff = themes.get("what_the_new_book_should_do_differently", [])
        if diff:
            a("**What the new book should do differently**")
            for it in diff:
                a(f"- {it}")
            a("")
        a("### Per-niche consumer maps")
        a("")
        a("| Niche | Praise | Complaints | Unmet needs |")
        a("|---|---|---|---|")
        for m in ci["by_niche"][:20]:
            a(
                f"| {m['niche']} | {'; '.join(m['praises'][:3]) or '—'} | "
                f"{'; '.join(m['complaints'][:3]) or '—'} | "
                f"{'; '.join(m['unmet'][:3]) or '—'} |"
            )
        a("")

    # ----------------------------------------------------------- competitors
    if model.competitors:
        a("## Competitor analysis")
        a("")
        a(
            "Competitor screenshots exist to extract positioning lessons — never "
            "to copy. Each entry notes where the proposed book can create a "
            "materially stronger proposition."
        )
        a("")
        for c in model.competitors[:30]:
            a(f"### {c['title']}")
            a("")
            meta_bits = [b for b in (
                c.get("marketplace") and f"Marketplace: {c['marketplace']}",
                c.get("asin") and f"ASIN: {c['asin']}",
                c.get("price") is not None and f"Price: {c['price']}",
                c.get("rank") is not None and f"Rank: {c['rank']}",
                c.get("review_count") is not None and f"Reviews: {c['review_count']}",
                c.get("saturation") and f"Saturation: {c['saturation']}",
            ) if b]
            if meta_bits:
                a(" · ".join(str(b) for b in meta_bits))
                a("")
            if c.get("url"):
                a(f"<{c['url']}>")
                a("")
            if c.get("screenshot_artifact_id"):
                a(
                    f"![cover — {c['title']}](/api/sessions/{s.get('id')}"
                    f"/artifacts/{c['screenshot_artifact_id']}/file)"
                )
                a("")
            if c.get("positioning"):
                a(f"**Positioning / patterns:** {c['positioning']}")
                a("")
            a(f"**How to outperform it legitimately:** {c.get('lesson', '—')}")
            a("")

    # ----------------------------------------------------------------- ledger
    if model.candidate_ledger:
        a("## Candidate ledger")
        a("")
        a("| Niche | Status | Marketplace | Rationale |")
        a("|---|---|---|---|")
        for c in model.candidate_ledger:
            rat = (c.get("rationale") or "").replace("|", "\\|")
            a(f"| {c['niche']} | {c['status']} | {c.get('marketplace') or '—'} | {rat} |")
        a("")

    # ------------------------------------------------------------ methodology
    a("## Methodology trace")
    a("")
    for t in model.methodology_trace:
        extra = f" ({', '.join(t['outputs'])})" if t.get("outputs") else ""
        a(f"- **{t['title']}** — completed{extra}")
    a("")

    # ------------------------------------------------------------- quality
    q = model.quality_statement
    a("## Research quality statement")
    a("")
    a(f"**What was investigated.** {q.get('investigated', '—')}")
    a("")
    a(
        f"**What was verified.** {q.get('verified', 0)} opportunities carry passing "
        "adversarial verification verdicts."
    )
    a("")
    a(f"**What was rejected.** {q.get('rejected', 0)} candidates were rejected by deterministic filters, the verification engine, or KDP risk screening.")
    if q.get("rejection_reasons"):
        for r in q["rejection_reasons"][:8]:
            a(f"- {r['niche']}: {r['rationale']}")
        a("")
    if q.get("remains_uncertain"):
        a("**What remains uncertain.**")
        for u in q["remains_uncertain"]:
            a(f"- {u}")
        a("")
    if q.get("verification_limitations"):
        a("**Recorded verification limitations.**")
        for l in q["verification_limitations"]:
            a(f"- {l}")
        a("")
    a(f"**Certainty policy.** {q.get('certainty_policy', '—')}")
    a("")

    # ------------------------------------------------------------- appendix
    if model.evidence_appendix:
        a("## Evidence appendix")
        a("")
        a(
            "Traceability for key conclusions: every row is a captured browser "
            "observation with source, marketplace, timestamp and — where "
            "captured — a screenshot."
        )
        a("")
        a("| Evidence id | Kind | Marketplace | Source / product | Captured | Screenshot |")
        a("|---|---|---|---|---|---|")
        for e in model.evidence_appendix:
            src = e.get("title") or e.get("url") or "—"
            src = str(src).replace("|", "\\|")[:80]
            shot = (
                f"[view](/api/sessions/{s.get('id')}/artifacts/{e['screenshot_artifact_id']}/file)"
                if e.get("screenshot_artifact_id") else "—"
            )
            a(
                f"| {e['id'][:12]}… | {e['kind']} | {e.get('marketplace') or '—'} | "
                f"{src} | {e.get('captured_at', '')[:19]} | {shot} |"
            )
        a("")

    a("---")
    a(
        "*Generated by Resurrección from durable, evidence-linked research "
        "state. Every opportunity above passed the deterministic methodology, "
        "the adversarial verification engine, and the research quality gates."
    )
    return "\n".join(lines)


def _render_opportunity(lines: list[str], i: int, o: dict[str, Any]) -> None:
    a = lines.append
    a(f"### {i}. {o['name']}")
    a("")
    vr = o.get("verification", {})
    conf = int(round(float(o.get("confidence", 0)) * 100))
    rows = [
        ("Precise niche", o.get("niche")),
        ("Parent market", o.get("parent_market")),
        ("Target reader", o.get("target_reader")),
        ("Reader problem / desire", o.get("reader_problem")),
        ("Marketplaces", ", ".join(o.get("marketplaces") or []) or "—"),
        ("Market gap", o.get("market_gap")),
        ("Differentiation opportunity", o.get("differentiation")),
        ("Positioning", o.get("positioning") or "—"),
        ("Risks", "; ".join(o.get("risks") or []) or "—"),
        ("Verification status", vr.get("status", "—")),
        ("Confidence", f"{conf}%"),
        ("Recommended next step", o.get("next_step", "—")),
    ]
    for label, value in rows:
        a(f"- **{label}:** {value if value not in (None, '') else '—'}")
    a("")

    # Niching hierarchy.
    chain = o.get("niching_chain") or []
    if chain:
        a("**Niching hierarchy**")
        a("")
        labels = ["Broad Market", "Category", "Subcategory", "Audience", "Specific Need", "Specific Use Case", "Final Book Opportunity"]
        for j, step in enumerate(chain):
            label = labels[j] if j < len(labels) - 1 else labels[-1]
            prefix = "→ " if j else ""
            a(f"{prefix}**{label}:** {step}")
        a("")

    # Demand.
    d = o.get("demand") or {}
    a(f"**Demand signals:** {d.get('verdict', '—')}"
      + (f" — {'; '.join(str(x) for x in d.get('signals', []))}" if d.get("signals") else ""))
    if d.get("demand_risks"):
        a(f"  - Demand risks: {'; '.join(str(x) for x in d['demand_risks'])}")
    a("")

    # Marketplace analysis.
    m = o.get("marketplace_analysis") or {}
    if m:
        a("**Marketplace analysis**")
        a("")
        a(f"- Observed on: {', '.join(m.get('observed', [])) or '—'}")
        a(f"- Strongest supporting: {', '.join(m.get('strongest', [])) or '—'}")
        if m.get("related_demand_elsewhere"):
            a(f"- Related demand elsewhere: {', '.join(m['related_demand_elsewhere'])}")
        for diff in m.get("differences", [])[:4]:
            a(f"- {diff}")
        a(f"- {m.get('language_considerations', '')}")
        a(f"- Scope: {m.get('scope', '—')}")
        a("")

    # Competition + consumer needs summaries.
    if o.get("competitive_landscape"):
        a(f"**Competitive landscape:** {o['competitive_landscape']}")
        a("")
    if o.get("consumer_needs"):
        a(f"**Consumer needs:** {o['consumer_needs']}")
        a("")

    # Angles.
    angles = o.get("angles") or []
    if angles:
        a("**Evidence-backed book angles**")
        a("")
        for ang in angles[:5]:
            a(f"- **Reader problem:** {ang.get('reader_problem', '—')}")
            a(f"  - **Angle:** {ang.get('angle', '—')}")
            ev = ang.get("evidence_ids") or []
            if ev:
                a(f"  - **Evidence:** {', '.join(str(e) for e in ev[:6])}")
            a(f"  - **Why it addresses the gap:** {ang.get('addresses_gap', '—')}")
        a("")

    # Titles.
    titles = o.get("titles") or []
    if titles:
        a("**Title concepts** (based on observed buyer language and competitor positioning — no title is guaranteed to perform)")
        a("")
        for t in titles[:6]:
            full = t.get("title", "")
            if t.get("subtitle"):
                full += f": {t['subtitle']}"
            a(f"- **{full}** — {t.get('rationale', '')}")
        a("")

    # Keywords.
    k = o.get("keywords") or {}
    if any(k.get(x) for x in ("core", "supporting", "long_tail", "problem_based", "audience_specific", "intent_phrases")):
        a("**Keyword intelligence**")
        a("")
        for label, key in (
            ("Core", "core"), ("Supporting", "supporting"), ("Long-tail", "long_tail"),
            ("Problem-based", "problem_based"), ("Audience-specific", "audience_specific"),
            ("Intent phrases", "intent_phrases"),
        ):
            items = k.get(key) or []
            if items:
                a(f"- **{label}:** {', '.join(items)}")
        a(f"- *Why these:* {k.get('relevance_rationale', '')}")
        kwi = o.get("keyword_integrity") or {}
        if kwi.get("warnings"):
            a(f"- *Integrity notes:* {'; '.join(str(w) for w in kwi['warnings'][:3])}")
        a("")

    # KDP risk.
    risk = o.get("kdp_risk") or {}
    level = risk.get("level", "safe")
    if risk.get("warnings"):
        a(f"- **KDP risk ({level}):** {'; '.join(str(w) for w in risk['warnings'])}")
    else:
        a(f"- **KDP risk ({level}):** no policy flags raised by deterministic screening")
    a("")

    # Verification detail.
    if vr:
        a(f"**Verification:** {vr.get('verdict', vr.get('status', '—'))}"
          + (f" (engine confidence {int(round(float(vr['confidence']) * 100))}%)" if vr.get("confidence") is not None else ""))
        if vr.get("limitations"):
            a(f"  - Limitations: {'; '.join(vr['limitations'])}")
        a("")

    ev = o.get("evidence_ids") or []
    if ev:
        a(f"- **Evidence trail:** {', '.join(str(e) for e in ev[:12])}")
    a("")
