"""Analyst narrative — the model WRITES the report, the backend OWNS the numbers.

Division of labor (the architectural decision from the spec):

  - Deterministic backend  — every accounting figure (candidate counts,
    rejection reasons, evidence references, verification outcomes,
    surviving opportunities) is computed from durable stores by
    app.report_model. The model never calculates or recomputes these.
  - The model              — interprets the verified records in plain
    language: why niches look promising, what competitors reveal, where
    demand comes from, and what risks remain. It writes like a market-
    intelligence analyst, not a template.

Grounding is enforced DETERMINISTICALLY, not hoped for:

  - The prompt carries only the verified digest, so the model literally
    cannot see numbers that were not measured.
  - Every number the model emits is checked against the numbers present in
    the digest. Unknown figures cause one corrective retry; if they
    persist, the offending sentences are removed before rendering — an
    invented figure never reaches the owner.
  - Inferences must be labeled by the model ("Inference — ..."), keeping
    observed evidence distinguishable from interpretation.

Any failure (model down, contract broken, grounding hopeless) returns an
empty narrative and the report renders deterministically — the narrative is
additive, never a dependency.
"""
from __future__ import annotations

import json
import re
from typing import Any

NARRATIVE_FIELDS = ("overview", "opportunity_analyses", "risks")

NARRATIVE_SYSTEM = """You are a senior market-intelligence analyst writing for a non-developer publisher.

You will receive SESSION DATA: the verified, deterministically-computed record of a completed research session. Everything in it is real; anything NOT in it was not measured.

Write ONE JSON object with exactly these keys:
  "overview":            3-5 markdown paragraphs — the analyst's read of the whole session: what the research found, why the promising niches look promising, what the competitors reveal, where the demand comes from, and how the portfolio fits together. Mention specific books, readers and signals from the data.
  "opportunity_analyses": an object keyed by the EXACT "niche" string copied verbatim from each entry in SESSION DATA opportunities, each value 2-4 markdown paragraphs interpreting THAT opportunity: the reader it serves, why the gap is real (cite the competitor/price/review facts), what the demand evidence shows, and what could still go wrong.
  "risks":               2-4 markdown paragraphs: what remains uncertain, which risks matter most, what was NOT measured in this session, and what to verify next.

GROUND RULES (non-negotiable):
1. Never invent numbers. Every figure you write must appear verbatim in SESSION DATA. If a figure you want is absent, write "not measured in this session" instead.
2. Never invent evidence ids, quotes, review text, prices, ranks or competitors.
3. The accounting block is FINAL: if you repeat counts, copy them exactly.
4. Label interpretation: start any sentence that is your inference (not a fact from the data) with "Inference — ".
5. Plain language for a non-developer: no JSON, no field names like "observed_at_evidence_ids", no code identifiers. Full sentences and short paragraphs; markdown body text only (no headings).
6. Do not restate the whole dossier — ADD interpretation. The deterministic sections already carry the raw figures.
7. KDSpy Pro numbers are third-party ESTIMATES, never measurements. If you mention one, say it is an estimate and lean on the measured evidence (search result totals, review counts, category paths) for any conclusion.
8. "backend_research" per opportunity is measured: the Amazon result totals, competition labels and store category paths were captured by the browser. Interpret them — why a keyword looks winnable, which store category fits — without inventing any new figure.

Output ONLY the JSON object."""


# ------------------------------------------------------------------- digest
_KEEP_OPP = (
    "name", "niche", "parent_market", "target_reader", "reader_problem",
    "market_gap", "differentiation", "positioning", "risks", "confidence",
    "niching_chain", "evidence_ids", "marketplaces", "backend_research",
)
_KEEP_OPP_NESTED = (
    "demand", "verification", "whitespace", "kdp_risk",
    "demand_accommodation", "marketplace_analysis",
)


def build_digest(model: dict[str, Any]) -> dict[str, Any]:
    """The verified research data the analyst model gets to see.

    Deterministic selection from the report model: accounting figures,
    opportunity dossiers, competitor observations, consumer intelligence,
    the candidate ledger with rejection reasons, headroom, adjacency and
    the evidence appendix. Nothing raw, nothing invented.
    """
    ex = dict(model.get("executive_summary") or {})
    q = dict(model.get("quality_statement") or {})
    opps: list[dict[str, Any]] = []
    for o in model.get("opportunities") or []:
        if not isinstance(o, dict):
            continue
        slim = {k: o.get(k) for k in _KEEP_OPP if k in o}
        for k in _KEEP_OPP_NESTED:
            if o.get(k):
                slim[k] = o.get(k)
        opps.append(slim)
    competitors = [
        {k: c.get(k) for k in (
            "title", "niche", "marketplace", "asin", "price", "rank",
            "review_count", "saturation", "positioning",
        ) if c.get(k) not in (None, "")}
        for c in (model.get("competitors") or []) if isinstance(c, dict)
    ]
    return {
        "accounting": {
            "candidates_considered": ex.get("candidates_considered"),
            "candidates_rejected": ex.get("candidates_rejected"),
            "survived_verification": ex.get("survived_verification"),
            "opportunities": len(opps),
            "marketplaces_investigated": ex.get("marketplaces_investigated"),
            "demand_verdicts": ex.get("demand_verdicts"),
            "what_was_verified": q.get("verified"),
            "what_was_rejected": q.get("rejected"),
            "rejection_reasons": q.get("rejection_reasons"),
            "remains_uncertain": q.get("remains_uncertain"),
            "verification_limitations": q.get("verification_limitations"),
            "estimate_policy": q.get("estimate_policy"),
            "certainty_policy": q.get("certainty_policy"),
        },
        "session": {
            "objective": ex.get("why"),
            "brief": ex.get("what_was_researched"),
            "methodology": ex.get("methodology"),
            "major_findings": ex.get("major_findings"),
        },
        "opportunities": opps,
        "competitors": competitors,
        "consumer_intelligence": model.get("consumer_intelligence") or {},
        "candidate_ledger": model.get("candidate_ledger") or [],
        "headroom_ranking": model.get("headroom_ranking") or [],
        "adjacent_ledger": model.get("adjacent_ledger") or [],
        "evidence_appendix": [
            {k: e.get(k) for k in ("id", "kind", "marketplace", "title", "captured_at")
             if e.get(k) not in (None, "")}
            for e in (model.get("evidence_appendix") or []) if isinstance(e, dict)
        ][:60],
    }


def digest_json(digest: dict[str, Any]) -> str:
    return json.dumps(digest, ensure_ascii=False, default=str)


# ---------------------------------------------------------------- grounding
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def allowed_numbers(data: str) -> set[str]:
    """Every number that literally appears in the verified data."""
    out: set[str] = set()
    for m in _NUM_RE.finditer(data):
        tok = m.group(0).replace(",", "")
        out.add(tok)
        try:
            f = float(tok)
        except ValueError:
            continue
        if f == int(f):
            out.add(str(int(f)))
    return out


def _benign(num: float) -> bool:
    """Structural small integers and years are not "figures"."""
    return (0 <= num <= 12 and num == int(num)) or (1900 <= num <= 2100)


def unknown_numbers(text: str, allowed: set[str]) -> list[str]:
    bad: list[str] = []
    for m in _NUM_RE.finditer(text):
        tok = m.group(0)
        norm = tok.replace(",", "")
        if norm in allowed:
            continue
        try:
            f = float(norm)
        except ValueError:
            bad.append(tok)
            continue
        if not _benign(f):
            bad.append(tok)
    return list(dict.fromkeys(bad))


def ground_text(text: str, allowed: set[str]) -> tuple[str, list[str]]:
    """Remove every sentence/bullet citing a number absent from the data.

    Returns (grounded_text, removed_figure_tokens). Structure-preserving:
    bullets, tables and headings are dropped whole; prose loses only the
    offending sentence.
    """
    removed: list[str] = []
    out_lines: list[str] = []
    for line in str(text).splitlines():
        stripped = line.strip()
        structural = stripped.startswith(("-", "|", "#", ">", "*"))
        if structural:
            bad = unknown_numbers(line, allowed)
            if bad:
                removed += bad
                continue
            out_lines.append(line)
            continue
        kept_sentences: list[str] = []
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            bad = unknown_numbers(sentence, allowed)
            if bad:
                removed += bad
                continue
            kept_sentences.append(sentence)
        if kept_sentences:
            out_lines.append(" ".join(kept_sentences))
    return "\n".join(out_lines).strip(), list(dict.fromkeys(removed))


# --------------------------------------------------------------- normalize
def _clean_section(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    # The renderer owns document structure: headings inside a narrative
    # section would duplicate the deterministic section titles.
    lines = [ln for ln in value.splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines).strip()


def normalize_narrative(
    content: Any, *, allowed: set[str]
) -> tuple[dict[str, Any], list[str]]:
    """Validate + ground one model reply into renderable sections.

    Returns (narrative, violations_found). `narrative` is empty when
    nothing usable survived. opportunity_analyses is keyed by the LOWERCASE
    exact niche string so the renderer can look it up against the
    deterministic dossier.
    """
    if not isinstance(content, dict):
        return {}, ["narrative reply was not an object"]
    overview = _clean_section(content.get("overview"))
    risks = _clean_section(content.get("risks"))
    analyses_raw = content.get("opportunity_analyses")
    analyses: dict[str, str] = {}
    if isinstance(analyses_raw, dict):
        for niche, text in analyses_raw.items():
            key = str(niche).strip().lower()
            cleaned = _clean_section(text)
            if key and cleaned:
                analyses[key] = cleaned

    unresolved: list[str] = []
    overview, removed = ground_text(overview, allowed)
    unresolved += [f"overview: {t}" for t in removed]
    risks, removed = ground_text(risks, allowed)
    unresolved += [f"risks: {t}" for t in removed]
    grounded_analyses: dict[str, str] = {}
    for key, text in analyses.items():
        grounded, removed = ground_text(text, allowed)
        unresolved += [f"{key}: {t}" for t in removed]
        if grounded:
            grounded_analyses[key] = grounded

    narrative: dict[str, Any] = {}
    if overview:
        narrative["overview"] = overview
    if grounded_analyses:
        narrative["opportunity_analyses"] = grounded_analyses
    if risks:
        narrative["risks"] = risks
    return narrative, unresolved


def user_prompt(digest: str) -> str:
    return (
        "SESSION DATA (verified, deterministic — the ONLY facts you may use):\n"
        + digest
        + "\n\nWrite the analyst narrative now, as the JSON object specified."
    )


def correction_prompt(digest: str, violations: list[str]) -> str:
    return (
        user_prompt(digest)
        + "\n\nCORRECTION REQUIRED: your previous reply cited figures that are "
        "NOT in the session data and were removed: "
        + ", ".join(sorted(set(violations))[:20])
        + ". Remove them or replace them with wording from the data "
        "(or 'not measured in this session'). Every remaining number must "
        "appear verbatim in SESSION DATA."
    )
