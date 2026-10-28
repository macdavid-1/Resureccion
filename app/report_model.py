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
  Keyword plan             ← keywords ranked by measured search evidence
  KDP categories           ← recommended categories from competitor pages
  Demand vs competition    ← standing ratio verdict from the filters
  KDP risk                 ← deterministic risk assessments
  Evidence appendix        ← browser evidence ledger
  Quality statement        ← counts, verification outcomes, limitations

Nothing in this module fabricates: where data is absent the model says so.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.integrity import KIND_FILTER, KIND_VERIFICATION
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
    adjacent_ledger: list[dict[str, Any]] = field(default_factory=list)
    headroom_ranking: list[dict[str, Any]] = field(default_factory=list)
    methodology_trace: list[dict[str, Any]] = field(default_factory=list)
    focus_pivots: list[dict[str, Any]] = field(default_factory=list)
    evidence_appendix: list[dict[str, Any]] = field(default_factory=list)
    quality_statement: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    # Analyst prose written BY THE MODEL from the verified data above
    # (app.report_narrative). The model never computes figures; these are
    # interpretation sections rendered alongside the deterministic ones.
    narrative: dict[str, Any] = field(default_factory=dict)

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
            "adjacent_ledger": self.adjacent_ledger,
            "headroom_ranking": self.headroom_ranking,
            "methodology_trace": self.methodology_trace,
            "focus_pivots": self.focus_pivots,
            "evidence_appendix": self.evidence_appendix,
            "quality_statement": self.quality_statement,
            "validation": self.validation,
            "narrative": self.narrative,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=1)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReportModel":
        m = cls()
        list_fields = (
            "opportunities", "competitors", "candidate_ledger",
            "adjacent_ledger", "headroom_ranking", "methodology_trace",
            "focus_pivots", "evidence_appendix",
        )
        for f in (
            "schema_version", "session", "generated_at", "executive_summary",
            "opportunities", "consumer_intelligence", "competitors",
            "candidate_ledger", "adjacent_ledger", "headroom_ranking",
            "methodology_trace", "focus_pivots", "evidence_appendix",
            "quality_statement", "validation", "narrative",
        ):
            default: Any = [] if f in list_fields else {}
            setattr(m, f, data.get(f, default))
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
    focus_pivots: list[dict[str, Any]] | None = None,
    whitespace_by_candidate: dict[str, Any] | None = None,
    backlog: list[dict[str, Any]] | None = None,
    adjacency: list[dict[str, Any]] | None = None,
    evidence: Any | None = None,
) -> ReportModel:
    """Assemble the structured report model from durable state. Deterministic."""
    ws = dict(whitespace_by_candidate or {})
    session = sessions.require(session_id)
    cands = candidates.list(session_id, limit=300)
    opps = opportunities.list(session_id, limit=50)
    outputs = phase_outputs or {}
    cand_by_id = {c.id: c for c in cands}

    # Chain-of-custody: every evidence id the report may CITE must actually
    # exist in this session. The model sometimes echoes invented ids (e.g.
    # "evidence_space_facts_uk"); quoting them would both mislead the owner
    # and trip the validator's "evidence references do not exist" failure.
    known_ids: set[str] | None = None
    evidence_records_total = 0
    if browser_evidence is not None and evidence is not None:
        known_ids = set()
        for store_ in (browser_evidence, evidence):
            if store_ is None:
                continue
            try:
                rows = store_.list(session_id, limit=5000)
                known_ids |= {str(e.id) for e in rows}
                evidence_records_total += len(rows)
            except Exception:
                pass
    # Accounting figures the executive summary must state: how many evidence
    # records and how many chain-of-custody claims the run actually produced
    # (the quality gates read the same durable stores).
    try:
        claims_total = len(claims.list(session_id, limit=3000))
    except Exception:
        claims_total = 0

    # Flat lookups -----------------------------------------------------------
    risk_by_cand: dict[str, dict[str, Any]] = {}
    kwi_by_cand: dict[str, dict[str, Any]] = {}
    demand_acc_by_cand: dict[str, dict[str, Any]] = {}
    if assessments is not None:
        for a in assessments.list(session_id, kind="kdp_risk", limit=800):
            risk_by_cand.setdefault(a.subject_id, a.metrics or {})
        for a in assessments.list(session_id, kind="keyword_integrity", limit=800):
            kwi_by_cand.setdefault(a.subject_id, a.metrics or {})
        # The standing demand-vs-competition verdict (recorded pass or fail
        # by the demand_accommodation filter) — rendered for every dossier.
        for a in assessments.list(session_id, kind=KIND_FILTER, limit=800):
            if a.stage == "demand_accommodation":
                demand_acc_by_cand.setdefault(a.subject_id, {
                    "outcome": a.outcome,
                    "reason": a.reason,
                    "metrics": a.metrics or {},
                })
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
    # Final-candidate deep dive: measured backend keywords + the separate
    # Kindle Store / Books category maps, indexed by the niche they belong to.
    dive_by_niche = _dive_index(ev_rows)

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
            "keyword_plan": _keyword_plan(o.keywords, ev_rows),
            "category_plan": _category_plan(ev_ids, ev_rows),
            "backend_research": dive_by_niche.get(_norm_kw(o.niche)) or {},
            "demand_accommodation": demand_acc_by_cand.get(o.candidate_id) or {},
            "whitespace": ws.get(o.candidate_id) or {},
            "evidence_ids": _only_known(ev_ids, known_ids)[:24],
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
        executive_summary=_exec_summary(
            session, counts, demand_out, outputs, dossiers,
            evidence_records=evidence_records_total, claims_registered=claims_total,
        ),
        opportunities=dossiers,
        consumer_intelligence=_aggregate_consumer(cons_out, niches_out),
        competitors=competitors,
        candidate_ledger=_ledger(cands),
        adjacent_ledger=_adjacent_ledger(
            cands, ws, opportunities_ids={o.candidate_id for o in opps},
            backlog=backlog or [], adjacency=adjacency or [],
            known_ids=known_ids,
        ),
        headroom_ranking=_headroom_ranking(dossiers),
        methodology_trace=_trace(outputs),
        focus_pivots=_pivot_rows(focus_pivots),
        evidence_appendix=_evidence_appendix(ev_rows),
        quality_statement=_quality_statement(
            counts, cands, verifications, session_id, assessments, claims,
            focus_pivots=focus_pivots,
        ),
    )
    return model


# --------------------------------------------------------------- exec summary
def _exec_summary(
    session: Any,
    counts: dict[str, int],
    demand_out: list[dict[str, Any]],
    outputs: dict[str, Any],
    dossiers: list[dict[str, Any]],
    evidence_records: int = 0,
    claims_registered: int = 0,
) -> dict[str, Any]:
    # Blank verdicts are dropped: a column of empty strings is not a finding,
    # and repeating it made the summary report "eight empty demand verdicts".
    verdicts = [
        str(d.get("verdict", "")).strip()
        for d in demand_out
        if isinstance(d, dict) and str(d.get("verdict", "")).strip()
    ]
    findings: list[str] = []
    for d in dossiers[:10]:
        findings.append(
            f"{d['name']} — {d['niche']} for {d['target_reader']} "
            f"(confidence {int(round(d['confidence'] * 100))}%)"
        )
    if not findings:
        # No synthesized portfolio yet — still hand the owner what the run
        # DID establish, from recorded verdicts, so "major findings" never
        # renders empty when demand research produced something.
        for d in demand_out[:8]:
            if not isinstance(d, dict):
                continue
            niche = str(d.get("niche") or "").strip()
            verdict = str(d.get("verdict") or "").strip()
            if niche and verdict:
                findings.append(f"{niche} — {verdict}")
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
        "candidates_considered": counts.get("total")
        or (
            counts.get("discovered", 0) + counts.get("rejected", 0)
            + counts.get("verifying", 0) + counts.get("verified", 0)
        ),
        "candidates_rejected": counts.get("rejected", 0),
        "survived_verification": counts.get("verified", 0),
        "demand_verdicts": verdicts,
        "major_findings": findings,
        "evidence_records": int(evidence_records or 0),
        "claims_registered": int(claims_registered or 0),
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
                "verdict": _plain(d.get("verdict")) or "not assessed",
                "signals": [_plain(s) for s in _as_list(d.get("signals")) if _plain(s)][:8],
                "demand_risks": [_plain(s) for s in _as_list(d.get("risks")) if _plain(s)][:6],
            }
    return {"verdict": "not separately assessed", "signals": [], "demand_risks": []}


def _comp_summary(comp_out: list[dict[str, Any]], niche: str) -> str:
    n = niche.lower()
    for m in comp_out:
        if str(m.get("niche", "")).lower() in n or n in str(m.get("niche", "")).lower():
            parts = []
            comps = _as_list(m.get("competitors"))
            if comps:
                names = ", ".join(_plain(c) for c in comps[:6] if _plain(c))
                parts.append(f"{len(comps)} competitors inspected ({names})")
            for k, label in (("saturation", "saturation"), ("pricing", "pricing"), ("patterns", "patterns")):
                if m.get(k):
                    parts.append(f"{label}: {_plain(m[k])}")
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
                    rendered = [s for s in (_plain(x) for x in v[:4]) if s]
                    if rendered:
                        bits.append(f"{label}: {'; '.join(rendered)}")
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
            f"{c.get('marketplace')}: {_plain(c.get('verdict'))} — {_plain(c.get('notes', ''))}".strip(" —")
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
            _plain(complaints[i]) if i < len(complaints)
            else _plain(unmet[i]) if i < len(unmet)
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


def _norm_kw(s: str) -> str:
    """Normalized keyword/query text for matching (case/punct/space-insensitive)."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def _ev_field(e: Any, name: str, default: Any = None) -> Any:
    """Evidence rows are BrowserEvidence objects from the store (dicts in
    unit tests) — read either shape interchangeably."""
    if isinstance(e, dict):
        return e.get(name, default)
    v = getattr(e, name, default)
    return default if v is None else v


def _median_int(values: list[int]) -> int | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return int(s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2)


def _keyword_plan(keywords: list[str], ev_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank opportunity keywords against MEASURED Amazon search evidence.

    Keywords the research actually searched (matching search_results
    evidence) carry their captured result count and the median review count
    of the top results (the entry barrier), and rank first — lightest
    barrier first. Keywords never searched follow, honestly marked, so an
    unmeasured keyword is never presented as validated.
    """
    exact: dict[str, dict[str, Any]] = {}
    partial: list[tuple[str, dict[str, Any]]] = []
    for e in ev_rows:
        if _ev_field(e, "kind") != "search_results":
            continue
        data = _ev_field(e, "data") or {}
        nq = _norm_kw(str(data.get("query") or ""))
        if not nq:
            continue
        exact.setdefault(nq, e)
        partial.append((nq, e))
    plan: list[dict[str, Any]] = []
    for kw in keywords or []:
        k = str(kw).strip()
        if not k:
            continue
        nk = _norm_kw(k)
        match = exact.get(nk)
        if match is None:
            for nq, e in partial:
                if nk in nq or nq in nk:
                    match = e
                    break
        if match is not None:
            d = _ev_field(match, "data") or {}
            results = list(d.get("results") or [])[:10]
            reviews = [int(r.get("review_count") or 0) for r in results
                       if r.get("review_count") is not None]
            total = d.get("total_results")
            plan.append({
                "keyword": k,
                "measured": True,
                "result_count": int(d.get("result_count") or 0),
                # Amazon's own printed total — the real competition figure
                # (captured count above is only how many rows were pulled).
                "total_results": total if isinstance(total, int) else None,
                "total_results_are_approximate": d.get(
                    "total_results_are_approximate"
                ),
                "results_text": str(d.get("results_text") or ""),
                "top10_review_median": _median_int(reviews),
                "marketplace": str(_ev_field(match, "marketplace") or ""),
                "evidence_id": str(_ev_field(match, "id") or ""),
            })
        else:
            plan.append({
                "keyword": k, "measured": False, "result_count": None,
                "total_results": None, "top10_review_median": None,
                "marketplace": "", "evidence_id": "",
            })
    measured = [p for p in plan if p["measured"]]
    unmeasured = [p for p in plan if not p["measured"]]
    # Measured first: lightest entry barrier wins; ties → fewest competing
    # results (Amazon's own total when it was printed), then alphabetical.
    measured.sort(key=lambda p: (
        p["top10_review_median"] is None,
        p["top10_review_median"] if p["top10_review_median"] is not None else 0,
        p.get("total_results") if p.get("total_results") is not None
        else (p["result_count"] if p["result_count"] is not None else 0),
        p["keyword"].lower(),
    ))
    return measured + unmeasured


def _category_plan(ev_ids: list[str], ev_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Recommended KDP categories from observed competitor product pages.

    Pages linked to the opportunity are preferred; when none are linked it
    falls back to session-wide product pages and SAYS SO — the report never
    presents a fallback as direct linkage.
    """
    want = {str(x) for x in ev_ids}
    pages = [e for e in ev_rows
             if _ev_field(e, "kind") == "product_page"
             and str(_ev_field(e, "id")) in want]
    basis = "linked"
    if not pages:
        pages = [e for e in ev_rows if _ev_field(e, "kind") == "product_page"]
        basis = "session_fallback"
    stats: dict[str, dict[str, Any]] = {}
    for e in pages:
        d = _ev_field(e, "data") or {}
        path = [str(c).strip() for c in (d.get("category_path") or []) if str(c).strip()]
        cat = (path[-1] if path else str(d.get("best_seller_category") or "").strip())
        if not cat:
            continue
        s = stats.setdefault(cat, {
            "category": cat, "top_level": path[0] if path else "",
            "observed_books": 0, "_reviews": [],
        })
        s["observed_books"] += 1
        if d.get("review_count") is not None:
            s["_reviews"].append(int(d.get("review_count") or 0))
    items: list[dict[str, Any]] = []
    for s in stats.values():
        med = _median_int(s.pop("_reviews"))
        items.append({**s, "median_reviews": med})
    # Where most observed incumbents actually rank first; on ties the
    # lighter review barrier is friendlier for a new entrant.
    items.sort(key=lambda s: (-s["observed_books"], s["median_reviews"] if s["median_reviews"] is not None else -1))
    return {"basis": basis, "items": items[:5]}


def _dive_index(ev_rows: list[Any]) -> dict[str, dict[str, Any]]:
    """Index the final-candidate deep-dive evidence by normalized niche.

    Two evidence records exist per finalist: `keyword_research` (measured
    backend keywords) and `category_research` (the separate Kindle Store
    and Books category trees). A candidate the dive never reached simply has
    no entry — absence is rendered as absence, never as an empty claim.
    """
    out: dict[str, dict[str, Any]] = {}
    for e in ev_rows:
        kind = _ev_field(e, "kind")
        if kind not in ("keyword_research", "category_research"):
            continue
        d = _ev_field(e, "data") or {}
        key = _norm_kw(str(d.get("niche") or ""))
        if not key:
            continue
        slot = out.setdefault(key, {
            "niche": str(d.get("niche") or ""),
            "keywords": [], "keyword_evidence_id": "", "department": "",
            "ranked_by": "",
            "books": [], "kindle_store": [], "category_evidence_id": "",
            "category_note": "",
        })
        if kind == "keyword_research" and not slot["keywords"]:
            slot["keywords"] = [
                k for k in (d.get("keywords") or []) if isinstance(k, dict)
            ]
            slot["keyword_evidence_id"] = str(_ev_field(e, "id") or "")
            slot["department"] = str(d.get("department") or "")
            slot["ranked_by"] = str(d.get("ranked_by") or "")
        elif kind == "category_research" and not (
            slot["books"] or slot["kindle_store"]
        ):
            slot["books"] = [
                k for k in (d.get("books") or []) if isinstance(k, dict)
            ]
            slot["kindle_store"] = [
                k for k in (d.get("kindle_store") or []) if isinstance(k, dict)
            ]
            slot["category_evidence_id"] = str(_ev_field(e, "id") or "")
            slot["category_note"] = str(d.get("note") or "")
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
def _aggregate_consumer(
    cons_out: list[dict[str, Any]], niches_out: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    themes: dict[str, list[str]] = {
        "praised": [], "complaints": [], "unmet_needs": [],
        "desired_features": [], "frustrations": [], "objections": [],
        "expectations": [], "missing_information": [],
    }
    by_niche: list[dict[str, Any]] = []
    for m in cons_out:
        by_niche.append({
            "niche": str(m.get("niche", "")),
            "praises": [_plain(x) for x in _as_list(m.get("praises")) if _plain(x)][:8],
            "complaints": [_plain(x) for x in _as_list(m.get("complaints")) if _plain(x)][:8],
            "unmet": [_plain(x) for x in _as_list(m.get("unmet")) if _plain(x)][:8],
            "design_opportunities": [_plain(x) for x in _as_list(m.get("design_opportunities")) if _plain(x)][:8],
        })
        themes["praised"] += [_plain(x) for x in _as_list(m.get("praises")) if _plain(x)]
        themes["complaints"] += [_plain(x) for x in _as_list(m.get("complaints")) if _plain(x)]
        themes["unmet_needs"] += [_plain(x) for x in _as_list(m.get("unmet")) if _plain(x)]
        themes["desired_features"] += [_plain(x) for x in _as_list(m.get("design_opportunities")) if _plain(x)]
    # Dedupe, preserve order, bound.
    themes = {k: list(dict.fromkeys(v))[:12] for k, v in themes.items()}
    themes["frustrations"] = themes["complaints"][:6]
    themes["missing_information"] = themes["unmet_needs"][:6]
    themes["what_the_new_book_should_do_differently"] = themes["desired_features"][:8]
    # Every niche the run researched appears here, even when Phase 4 never
    # captured reader language for it: a blank table told the owner "nothing
    # was known" about niches the rest of the report discusses. The row is
    # present and explicitly marked as not captured — never a dash that
    # reads like data.
    seen_niches = {str(m.get("niche") or "").strip().lower() for m in by_niche}
    for n in niches_out or []:
        name = str(n.get("niche") or "").strip()
        if not name or name.lower() in seen_niches:
            continue
        seen_niches.add(name.lower())
        by_niche.append({
            "niche": name,
            "praises": [], "complaints": [], "unmet": [],
            "design_opportunities": [],
            "not_captured": True,
        })
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


def _adjacent_ledger(
    cands: list[Any],
    ws: dict[str, Any],
    *,
    opportunities_ids: set[str],
    backlog: list[dict[str, Any]],
    adjacency: list[dict[str, Any]],
    known_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Surviving-but-unfunded demand: candidates the run discovered and did
    NOT turn into an opportunity, plus capacity backlog and measured
    adjacency neighbours. Each row carries its headroom score and evidence
    ids so the owner can fund the next run from exactly this list."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(
        niche: str, *, status: str, origin: str, score: dict[str, Any] | None,
        evidence_ids: list[Any], rationale: str, marketplace: str = "",
    ) -> None:
        key = " ".join(str(niche or "").lower().split())
        if not key or key in seen:
            return
        seen.add(key)
        sc = score or {}
        rows.append({
            "niche": str(niche)[:200],
            "status": status,
            "marketplace": marketplace,
            "origin": origin,
            "whitespace": sc.get("score"),
            "measured": bool(sc.get("measured")),
            "evidence_ids": _only_known(evidence_ids or [], known_ids)[:6],
            "rationale": str(rationale or "")[:200],
        })

    for c in cands:
        if c.status == "rejected" or c.id in opportunities_ids:
            continue
        sc = ws.get(c.id) or {}
        _add(
            c.niche,
            status=c.status,
            origin=str((c.meta or {}).get("via") or "session"),
            score=sc,
            evidence_ids=sc.get("evidence_ids")
            or (c.meta or {}).get("seed_evidence_ids") or [],
            rationale=c.rationale,
            marketplace=c.marketplace,
        )
    for b in backlog:
        if not isinstance(b, dict):
            continue
        _add(
            str(b.get("niche") or ""),
            status="backlog",
            origin="capacity_backlog",
            score=None,
            evidence_ids=[],
            rationale=str(b.get("parent") or b.get("parent_market") or b.get("reason") or ""),
            marketplace=str(b.get("marketplace") or ""),
        )
    for a in adjacency:
        if not isinstance(a, dict) or a.get("promoted"):
            continue
        sc = a.get("whitespace") if isinstance(a.get("whitespace"), dict) else {}
        if not sc.get("measured"):
            continue  # only measured neighbours are worth funding next
        _add(
            str(a.get("term") or ""),
            status="measured",
            origin="adjacency_measured",
            score=sc,
            evidence_ids=sc.get("evidence_ids") or a.get("evidence_ids") or [],
            rationale=str(a.get("parent") or ""),
            marketplace=str(a.get("marketplace") or ""),
        )
    rows.sort(key=lambda r: (
        -(r["whitespace"] if isinstance(r["whitespace"], (int, float)) else -1.0),
        r["niche"],
    ))
    return rows[:60]


def _headroom_ranking(dossiers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The portfolio ordered by demand ÷ competition — an ORDERING signal
    printed next to the confidence order, never a replacement for it."""
    if len(dossiers) < 2:
        return []
    if not any((d.get("whitespace") or {}).get("score") is not None for d in dossiers):
        return []
    ordered = sorted(
        dossiers,
        key=lambda d: -(
            float((d.get("whitespace") or {}).get("score"))
            if (d.get("whitespace") or {}).get("score") is not None else -1.0
        ),
    )
    return [
        {
            "rank": i,
            "name": d.get("name"),
            "niche": d.get("niche"),
            "whitespace": (d.get("whitespace") or {}).get("score"),
            "measured": bool((d.get("whitespace") or {}).get("measured")),
            "confidence": d.get("confidence"),
        }
        for i, d in enumerate(ordered, 1)
    ]


def _trace(outputs: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for phase in PHASE_ORDER:
        out = outputs.get(phase) or {}
        keys = sorted(out.keys()) if isinstance(out, dict) else []
        # Honest accounting: a phase that handed over nothing is reported as
        # having produced no output instead of "completed" with an empty
        # contract list — the owner must be able to see WHERE research fell
        # short, not just that the methodology ran.
        rows.append({
            "phase": phase,
            "title": PHASE_TITLES[phase],
            "outputs": keys,
            "completed": bool(keys) and any(bool(v) for v in out.values()),
        })
    return rows


def _pivot_rows(pivots: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Strategic focus pivots (autonomous re-discovery) for the report."""
    rows: list[dict[str, Any]] = []
    for p in pivots or []:
        if not isinstance(p, dict):
            continue
        rows.append({
            "round": p.get("round"),
            "at": p.get("at") or "",
            "rejected": p.get("rejected", 0),
            "dead_niches": list(p.get("dead_niches") or []),
            "unresolved_niches": list(p.get("unresolved_niches") or []),
        })
    return rows


# Evidence kinds rendered in plain language for a non-developer reader.
# `kdspy_panel` is explicitly marked as an estimate: a third-party
# extension's numbers are never presented as observations.
_KIND_LABELS = {
    "search_results": "search results",
    "product_page": "product page",
    "reviews": "reader reviews",
    "autocomplete": "search suggestions",
    "related_products": "related products",
    "page_state": "page snapshot",
    "census": "category census",
    "autocomplete_frontier": "buyer-language frontier",
    "demand_language": "buyer language",
    "kdspy_panel": "KDSpy Pro panel (third-party estimate)",
    "keyword_research": "backend keyword research",
    "category_research": "store category research",
}


def _kind_label(kind: Any) -> str:
    k = str(kind or "")
    return _KIND_LABELS.get(k, k.replace("_", " "))


def _competition_of(total: int | None, captured: Any = None) -> str:
    """Plain-language competition bucket for a measured keyword.

    Amazon's own total is the input; when it was never printed the keyword
    is honestly 'not measured' instead of being guessed from the number of
    rows a capture happened to pull.
    """
    if not isinstance(total, int):
        return "not measured"
    from app.final_dive import competition_label

    return competition_label(total)


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
    focus_pivots: list[dict[str, Any]] | None = None,
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

    # Rejection reasons must be the recorded DECISION, not the creation
    # hint: filters and the verification engine each persist their reason as
    # a durable integrity assessment keyed by candidate id.
    rej_reasons: dict[str, str] = {}
    if assessments is not None:
        for kind in (KIND_FILTER, KIND_VERIFICATION):
            for a in assessments.list(session_id, kind=kind, limit=800):
                if a.outcome == "fail" and a.subject_id and a.reason:
                    rej_reasons.setdefault(str(a.subject_id), str(a.reason))

    total_candidates = counts.get("total") or (
        counts.get("discovered", 0) + counts.get("rejected", 0)
        + counts.get("verifying", 0) + counts.get("verified", 0)
    )
    return {
        "investigated": (
            f"{total_candidates} candidates researched across the methodology; "
            f"{len(claims.list(session_id, limit=3000))} claims registered "
            "with evidence chain-of-custody."
        ),
        "verified": counts.get("opportunities", 0),
        "rejected": counts.get("rejected", 0),
        "rejection_reasons": [
            {
                "niche": c.niche,
                "rationale": (
                    rej_reasons.get(c.id)
                    or (c.rationale or "rejected by deterministic screening")
                )[:200],
            }
            for c in rejected[:10]
        ],
        "focus_pivots": [
            (
                f"Round {p.get('round') or '—'}: {int(p.get('rejected', 0) or 0)} "
                "candidate(s) from the earlier focus were rejected; deep research "
                "and niche-down produced no verified opportunity, so discovery "
                "re-entered on a new focus."
            )
            for p in (focus_pivots or []) if isinstance(p, dict)
        ],
        "remains_uncertain": uncertain,
        "verification_limitations": limitations,
        "estimate_policy": (
            "Figures read from the KDSpy Pro extension (its sales, keyword and "
            "competition estimates) are third-party estimates: they are shown "
            "and labelled as estimates, they never count as evidence on their "
            "own, and no candidate is verified or rejected on them. Every "
            "verdict in this report rests on pages the research browser "
            "actually opened."
        ),
        "certainty_policy": (
            "This report states only what the recorded evidence supports. Where "
            "evidence was insufficient, the candidate was rejected or explicitly "
            "flagged as uncertain — certainty is never manufactured."
        ),
    }


# ------------------------------------------------------------------- helpers
def _ws_text(ws: Any) -> str:
    """Headroom score as prose (unmeasured is stated, never implied zero)."""
    if not isinstance(ws, dict) or ws.get("score") is None:
        return "unmeasured (no comparable evidence captured)"
    d = ws.get("demand")
    c = ws.get("competition")
    detail = f"demand {d:.2f} ÷ competition {c:.2f}" if isinstance(d, (int, float)) and isinstance(c, (int, float)) else ""
    return f"{float(ws['score']):.2f}" + (f" ({detail})" if detail else "")


def _as_list(v: Any) -> list[Any]:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        return [v]
    return []


def _only_known(ids: list[Any], known: set[str] | None) -> list[str]:
    """Keep evidence ids that exist in this session (order-preserving).

    `known is None` means the stores were not wired — keep everything and
    let the validator's existence check decide.
    """
    out: list[str] = []
    for i in ids:
        s = str(i)
        if (known is None or s in known) and s not in out:
            out.append(s)
    return out


# Friendly labels for the structured records phase outputs emit. The owner
# is not a developer: an evidence record must read as a sentence, never as
# `{'asin': '0241477905', 'observed_at_evidence_ids': [...]}`.
_PLAIN_LABELS = {
    "title": "title", "subtitle": "subtitle", "query": "search",
    "asin": "ASIN", "marketplace": "marketplace", "price": "price",
    "rating": "rating", "review_count": "reviews", "reviews": "reviews",
    "best_seller_rank": "best-seller rank", "rank": "rank",
    "category_path": "category", "positioning": "positioning",
    "notes": "note", "note": "note", "verdict": "verdict",
    "result_count": "results found", "outcome": "outcome",
    "total_results": "Amazon's total results",
    "total_results_are_approximate": "approximate total",
    "results_text": "results line printed on the page",
    "captured_results": "results captured",
    "competition": "competition",
    "demand_score": "demand score",
    "suggestion_hits": "search-suggestion hits",
    "department": "store searched",
    "store": "store",
    "breadcrumb": "category path",
    "ranked_by": "how these were ranked",
    "phrases_mined": "candidate phrases found",
    "requested": "keywords requested",
    "books": "Books store categories",
    "kindle_store": "Kindle Store categories",
    "top10_review_median": "median reviews, top 10",
    "top_asins": "top competing ASINs",
    "cover": "cover", "url": "source", "niche": "niche",
    "complaints": "complaints", "praises": "praise", "unmet": "unmet needs",
    "evidence_ids": "evidence", "observed_at_evidence_ids": "evidence",
}
_PLAIN_LEADS = ("title", "niche", "query", "name")


def _plain(v: Any, depth: int = 0) -> str:
    """Turn any structured record into one plain-language line.

    Deterministic formatting only — no inference, no invented values:
      {"title": "Ocean: Facts", "price": "\u00a35.99", "rating": 4.8}
    becomes
      "Ocean: Facts (price \u00a35.99, rating 4.8)"
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (str, int, float)):
        return str(v)
    if isinstance(v, list):
        return "; ".join(s for s in (_plain(x, depth + 1) for x in v) if s)
    if isinstance(v, dict):
        lead = next((str(v.get(k)) for k in _PLAIN_LEADS if v.get(k)), "")
        parts: list[str] = []
        for k, val in v.items():
            key = str(k)
            if key.startswith(_PLAIN_LEADS) and lead:
                continue
            if key.startswith("_") or val in (None, "", [], {}):
                continue
            label = _PLAIN_LABELS.get(key, key.replace("_", " "))
            if key in ("evidence_ids", "observed_at_evidence_ids"):
                ids = [str(i) for i in _as_list(val)]
                if ids:
                    shown = ", ".join(i[:10] + ("\u2026" if len(i) > 10 else "") for i in ids[:3])
                    parts.append(f"{label} {shown}")
                continue
            parts.append(f"{label} {_plain(val, depth + 1)}")
        body = ", ".join(parts)
        if lead:
            return f"{lead} ({body})" if body else lead
        return body
    return str(v)


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
        f"verification · {ex.get('survived_verification', 0)} survived with full verification · "
        f"{ex.get('evidence_records', 0)} evidence records captured · "
        f"{ex.get('claims_registered', 0)} claims registered with evidence chain-of-custody."
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

    # ------------------------------------------- analyst overview (AI prose)
    narrative = model.narrative if isinstance(model.narrative, dict) else {}
    if narrative.get("overview"):
        a("## Analyst's overview")
        a("")
        a(
            "*Written by the research model from the verified data in this "
            "report. Observed facts come from the evidence records; "
            "interpretations are labeled.*"
        )
        a("")
        for line in str(narrative["overview"]).splitlines():
            a(line)
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
        analyses = (
            narrative.get("opportunity_analyses")
            if isinstance(narrative.get("opportunity_analyses"), dict) else {}
        )
        for i, o in enumerate(model.opportunities, 1):
            analysis = analyses.get(str(o.get("niche") or "").strip().lower(), "")
            _render_opportunity(lines, i, o, analysis=analysis)
    else:
        a("## Opportunity portfolio")
        a("")
        a(
            "No opportunities passed adversarial verification in this session. "
            "The candidate ledger and quality statement below show what was "
            "learned and why candidates did not survive."
        )
        a("")

    # ------------------------------------------------------- headroom ranking
    if model.headroom_ranking:
        a("## Headroom ranking (demand ÷ competition)")
        a("")
        a(
            "The same portfolio ordered by measured headroom — observed "
            "demand divided by incumbent resistance. Deterministic ordering "
            "from captured evidence only; it never overrides a filter, gate "
            "or verification verdict."
        )
        a("")
        a("| Rank | Opportunity | Headroom | Confidence |")
        a("|---|---|---|---|")
        for r in model.headroom_ranking:
            score = r.get("whitespace")
            score_txt = f"{float(score):.2f}" if isinstance(score, (int, float)) else "unmeasured"
            name = str(r.get("name") or r.get("niche") or "—").replace("|", "\\|")
            conf = r.get("confidence")
            conf_txt = f"{int(round(float(conf) * 100))}%" if isinstance(conf, (int, float)) else "—"
            a(f"| {r.get('rank')} | {name} | {score_txt} | {conf_txt} |")
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
            def _cell(key: str) -> str:
                vals = m.get(key) or []
                if vals:
                    return "; ".join(str(v) for v in vals[:3])
                # Honest cell: never an em-dash that reads like a measurement.
                return (
                    "not captured in this session"
                    if m.get("not_captured") else "none recorded"
                )

            niche_txt = str(m.get("niche") or "—").replace("|", "\\|")
            a(
                f"| {niche_txt} | {_cell('praises')} | "
                f"{_cell('complaints')} | {_cell('unmet')} |"
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

    # ------------------------------------- adjacent opportunities (ledger)
    if model.adjacent_ledger:
        a("## Adjacent opportunities not yet researched")
        a("")
        a(
            f"{len(model.adjacent_ledger)} market(s) this run discovered or "
            "measured but did not fund — survivors beyond the candidate cap, "
            "measured adjacency neighbours, and the capacity backlog. Ranked "
            "by headroom with the evidence ids that back each row."
        )
        a("")
        a(
            "**One-click follow-up:** open this session in the dashboard and "
            "press *Start follow-up research* — it seeds a new run with these "
            "niches through the existing keywords mode, so each report funds "
            "the next, deeper one."
        )
        a("")
        a("| Niche | Origin | Status | Headroom | Evidence ids |")
        a("|---|---|---|---|---|")
        for r in model.adjacent_ledger:
            niche = str(r.get("niche") or "—").replace("|", "\\|")
            score = r.get("whitespace")
            score_txt = f"{float(score):.2f}" if isinstance(score, (int, float)) else "unmeasured"
            ev = ", ".join(str(e)[:10] for e in (r.get("evidence_ids") or [])[:3]) or "—"
            a(
                f"| {niche} | {r.get('origin') or 'session'} | "
                f"{r.get('status') or '—'} | {score_txt} | {ev} |"
            )
        a("")

    # --------------------------------- risks & open questions (AI prose)
    if narrative.get("risks"):
        a("## Risks, caveats and open questions")
        a("")
        for line in str(narrative["risks"]).splitlines():
            a(line)
        a("")

    # ------------------------------------------------------------ methodology
    a("## Methodology trace")
    a("")
    for t in model.methodology_trace:
        if t.get("completed"):
            extra = f" ({', '.join(t['outputs'])})" if t.get("outputs") else ""
            a(f"- **{t['title']}** — completed{extra}")
        else:
            a(
                f"- **{t['title']}** — produced no output this session "
                "(research ended before this phase handed over analysis)"
            )
    for p in model.focus_pivots:
        a(
            f"- **Strategic pivot (round {p.get('round') or '—'})** — deep "
            "research and niche-down left no verified candidate, so discovery "
            "re-entered on a new market focus"
        )
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
    if q.get("focus_pivots"):
        a("**Strategic focus pivots (autonomous re-discovery).**")
        for p in q["focus_pivots"]:
            a(f"- {p}")
        a("")
    if q.get("verification_limitations"):
        a("**Recorded verification limitations.**")
        for l in q["verification_limitations"]:
            a(f"- {l}")
        a("")
    if q.get("estimate_policy"):
        a(f"**Estimates vs observations.** {q['estimate_policy']}")
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
                f"| {e['id'][:12]}… | {_kind_label(e['kind'])} | {e.get('marketplace') or '—'} | "
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


def _render_opportunity(
    lines: list[str], i: int, o: dict[str, Any], *, analysis: str = ""
) -> None:
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
        ("Headroom (demand ÷ competition)", _ws_text(o.get("whitespace"))),
        ("Recommended next step", o.get("next_step", "—")),
    ]
    for label, value in rows:
        a(f"- **{label}:** {value if value not in (None, '') else '—'}")
    a("")

    # Analyst's take — model-written interpretation of THIS opportunity.
    if analysis:
        a("**Analyst's take:**")
        a("")
        for line in str(analysis).splitlines():
            a(line)
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

    # Demand vs competition — the standing ratio verdict from the
    # deterministic demand_accommodation filter (recorded pass or fail).
    da = o.get("demand_accommodation") or {}
    if da:
        m = da.get("metrics") or {}
        verdict = (
            "accommodated" if da.get("outcome") == "pass"
            else "needs more demand research"
        )
        a("**Demand vs competition:** "
          f"{m.get('distinct_competitor_asins', '—')} incumbent(s), "
          f"{m.get('deep_review_moats', '—')} with deep review moats · "
          f"{m.get('supported_demand_claims', '—')} evidenced demand claim(s), "
          f"{m.get('demand_signals', '—')} demand signal(s) → **{verdict}**")
        if da.get("outcome") != "pass" and da.get("reason"):
            a(f"  - {da['reason']}")
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

    # Recommended KDP categories — derived from observed competitor pages.
    cat_plan = o.get("category_plan") or {}
    cat_items = cat_plan.get("items") or []
    if cat_items:
        a("**Recommended KDP categories** (where the observed competitors "
          "actually rank — entry barrier = median reviews among the books "
          "observed there)")
        a("")
        for c in cat_items:
            rev = c.get("median_reviews")
            line = (f"- **{c.get('category', '—')}** — "
                    f"{c.get('observed_books', 0)} observed incumbent(s)"
                    + (f", median {rev} reviews" if rev is not None else ""))
            top = str(c.get("top_level") or "")
            if top and top != c.get("category"):
                line += f" · top-level: {top}"
            a(line)
        if cat_plan.get("basis") == "session_fallback":
            a("- *Derived from session-wide competitor pages — none were "
              "directly linked to this opportunity.*")
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

    # Keyword plan — ranked by MEASURED search evidence.
    kp = o.get("keyword_plan") or []
    if kp:
        a("**Keyword plan** (ranked by measured Amazon search evidence — "
          "lighter top-10 review barriers first; keywords the research never "
          "searched are marked, never assumed)")
        a("")
        a("| Rank | Keyword | Amazon results | Competition | Median reviews (top 10) | Marketplace |")
        a("|---|---|---|---|---|---|")
        for i, e in enumerate(kp, 1):
            kw = str(e.get("keyword", "")).replace("|", "\\|")
            if e.get("measured"):
                med = e.get("top10_review_median")
                total = e.get("total_results")
                if isinstance(total, int):
                    approx = (
                        "over " if e.get("total_results_are_approximate") else ""
                    )
                    total_txt = f"{approx}{total:,}"
                else:
                    total_txt = (
                        f"{e.get('result_count', 0)} captured"
                        " (Amazon total not printed)"
                    )
                a(f"| {i} | {kw} | {total_txt} "
                  f"| {_competition_of(total if isinstance(total, int) else None, e.get('result_count'))} "
                  f"| {med if med is not None else '—'} "
                  f"| {e.get('marketplace') or '—'} |")
            else:
                a(f"| {i} | {kw} | not searched | not searched | not searched | — |")
        a("")

    # Finalist deep dive — measured backend keywords + BOTH store category
    # maps (Kindle Store and Books keep separate category systems).
    br = o.get("backend_research") or {}
    bkw = [k for k in (br.get("keywords") or []) if isinstance(k, dict)]
    if bkw:
        dept = str(br.get("department") or "books")
        dept_label = "Kindle Store" if dept == "kindle" else "Books store"
        a("**Backend keywords researched for this niche**")
        a("")
        a(
            "Every phrase below came from Amazon's own search suggestions "
            "(language shoppers actually type) and was then searched for "
            "real. **Amazon results** is Amazon's own printed result total "
            "for that search — the crowd you would be entering against — "
            f"measured in the {dept_label} search."
        )
        a("")
        if br.get("ranked_by"):
            a(f"*Ranking: {br['ranked_by']}.*")
            a("")
        a("| # | Backend keyword | Competition | Amazon results | Median reviews, top 10 | Demand |")
        a("|---|---|---|---|---|---|")
        for i, k in enumerate(bkw, 1):
            kw_txt = str(k.get("keyword") or "—").replace("|", "\\|")
            demand = k.get("demand_score")
            demand_txt = str(demand) if demand is not None else "—"
            if k.get("measured"):
                total = k.get("total_results")
                if isinstance(total, int):
                    approx = "over " if k.get("total_results_are_approximate") else ""
                    total_txt = f"{approx}{total:,}"
                else:
                    total_txt = str(k.get("results_text") or "not printed by Amazon")
                med = k.get("top10_review_median")
                a(
                    f"| {i} | {kw_txt} | {k.get('competition') or 'not measured'} | "
                    f"{total_txt} | {med if med is not None else '—'} | {demand_txt} |"
                )
            else:
                a(
                    f"| {i} | {kw_txt} | not searched in this run | — | — | "
                    f"{demand_txt} |"
                )
        a("")
        if br.get("keyword_evidence_id"):
            a(f"- **Evidence record:** {br['keyword_evidence_id']}")
            a("")

    books_rows = [r for r in (br.get("books") or []) if isinstance(r, dict)]
    kindle_rows = [r for r in (br.get("kindle_store") or []) if isinstance(r, dict)]
    if books_rows or kindle_rows:
        a("**Store categories researched for this niche**")
        a("")
        a(
            "Amazon runs the Kindle Store and the Books store as two "
            "separate category systems, so both were mapped independently "
            "from real product pages in each department."
        )
        a("")
        if br.get("category_note"):
            a(f"*{br['category_note']}*")
            a("")
        for label, rows in (("Books store", books_rows), ("Kindle Store", kindle_rows)):
            if not rows:
                a(f"- **{label}:** not captured in this run")
                a("")
                continue
            a(f"**{label}** ({len(rows)} observed)")
            a("")
            for r in rows[:10]:
                cat = str(r.get("category") or "—")
                bc = str(r.get("breadcrumb") or "")
                if bc and bc != cat:
                    a(f"- {cat} — found under {bc}")
                else:
                    a(f"- {cat}")
            a("")
        if br.get("category_evidence_id"):
            a(f"- **Evidence record:** {br['category_evidence_id']}")
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
