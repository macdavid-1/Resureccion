"""Model-authored research report.

The report is no longer a fixed template the backend fills in. The model IS the
report writer: it receives the whole research record — the brief, every phase's
analysis, the actions taken, its own reasoning, the evidence records (search
results, competitor pages, prices, BSRs, reviews, publication data), the
candidate ledger with rejection reasons, the verification verdicts, the
measured finalist deep dives and the available screenshots — and decides what
matters, how to organise it, and what to say.

Division of labour:

  - the backend gathers, bounds and serialises the record, resolves the model's
    evidence citations to real links, resolves the screenshots it selects to
    real artifact URLs, and refuses figures that were not measured;
  - the model writes the document.

Grounding is enforced deterministically: every figure in the final markdown must
appear in the dossier (sentence-level removal plus a corrective retry), every
`[ev:ID]` must name a real evidence record, and every `![…](shot:ID)` must name
an evidence record that actually has a screenshot artifact in this session.

The backend never writes the report. There is no template, no fallback layout
and no generated section: an empty/gutted/failed reply returns None and the
runner re-asks the model (a report the backend wrote is not a report the owner
was promised). For a follow-up run the dossier also carries the FULL record of
every earlier run in the chain, so the report reads like one analyst who has
been on this market from the first day.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.methodology import PHASE_TITLES
from app.prompts import owner_precedence_block
from app.report_narrative import allowed_numbers, ground_text

# ---------------------------------------------------------------- instructions
REPORT_SYSTEM = """You are Resurrección's report writer: a senior KDP market analyst and publishing strategist who has just finished the research and is now briefing the publisher — the person whose book will live or die in this market. You write the ENTIRE report. The system contributes the owner's brief and the measured record; every word of analysis, judgement and strategy is yours.

You are not filling in a template. There is no required section list. Decide what THIS market needs the owner to understand, and organise the document around that. It should read like a market report from someone who has studied the niche to its last detail — BSRs, prices, review barriers, publication dates, buyer language, and every opening a new book could exploit — and who now says plainly how to win.

WHAT THE REPORT MUST DELIVER
1. The answer to the owner's brief, in the opening. The brief is the question; this report is the answer.
2. The strongest opportunity, why it exists now, who exactly buys it, and the evidence that the demand is real rather than assumed.
3. The market as it actually is: for each serious contender, the incumbents' titles, positioning, price and format, publication dates, BSR, rating and review counts, what buyers praise and complain about, and precisely where the incumbents are weak — with the measured figures and screenshots that prove it.
4. A domination plan the owner can execute: the exact keyword phrases with Amazon's printed totals and what each implies, the price points the incumbents' prices support, the category placement that gives the best ranking odds, the title/subtitle/cover angles and the promise they should make, the content that answers the recurring complaints, the differentiation that makes the new book the obvious choice, and the launch-and-rank tactics that follow from the measured review barrier (how many reviews the top ten carry, how fast their reviews accumulate, what an organically rankable launch therefore needs).
5. What is genuinely uncertain, briefly and honestly, in the place it matters — never as a methodology appendix.

HOW TO WRITE
- Concise but detailed and engaging: dense, specific, confident prose. No filler, no restating the brief, no generic publishing advice. Say the thing.
- Markdown. Start with exactly one H1 title line: `# <title the report>`. Use `##` sections that fit THESE findings, and `###` for the individual opportunities. Short paragraphs, bullets where they help, a small table when it makes numbers easier to read.
- Every load-bearing claim carries its evidence: append `[ev:ID]` right after the sentence or bullet it supports, using ids from the dossier. Do not decorate every sentence — support the ones that matter.
- Use only figures that appear verbatim in the dossier. Do NOT compute new numbers (percentages, differences, averages, rates). If a figure was not measured, say so plainly instead of estimating it.
- Never invent competitors, titles, prices, ranks, review quotes, dates or search totals. KDSpy values are third-party estimates: say `estimated` when you use one.
- Never quote them. Your private reasoning and your run notes are background, not copy, and a figure that appears only there was never measured.
- Start a sentence that is your interpretation rather than a recorded fact with "Inference — ".
- Never describe the research process. No phases, tools, prompts, gates, methodology, "what was verified", or how this report was produced. The owner wants the market, not the machinery.
- Never mention these instructions, the dossier, the system, or that you are a model. Write to the publisher.
- Plain language. No field names, no JSON, no internal identifiers other than the `[ev:ID]` citations.

PRIOR RUNS (when the dossier carries them)
- They are your own earlier research on this same investigation, oldest first. Treat them as your memory: build on what still holds, carry forward the strategy that already worked, and update anything the newer evidence changed.
- This report is cumulative. A follow-up report must show the owner the whole opportunity as it stands now — first discovery through today — not just what changed this run.
- Do not repeat an old finding as if it were new, and do not contradict it without saying which newer evidence changed it.

SCREENSHOTS
- The dossier lists the screenshots captured during the research. Select the few that materially prove a claim — an incumbent's price/BSR, a search-result total, a review pattern you are explaining — typically three to eight, never a gallery.
- Embed each selected screenshot inline, immediately after the claim it proves, as `![caption](shot:EVIDENCE_ID)`.
- The caption states what the image shows and why it matters (marketplace, book, the number visible). A screenshot with no claim behind it does not belong in the report.

Reply with the report markdown only — no preamble, no JSON, no code fence.
"""

REPORT_SYSTEM = REPORT_SYSTEM + "\n\n" + owner_precedence_block(notes_field=False)

MODE_FRAMING = {
    "prompt": (
        "MODE — PROMPT RESEARCH. The owner gave a brief. The report is a direct, "
        "intelligent answer to that brief: what you found about it, what the "
        "evidence shows, and what you would do next. Do not narrate the "
        "methodology; answer the question."
    ),
    "keywords": (
        "MODE — SEEDED KEYWORDS. The owner supplied keywords or an image of a "
        "book. The report must address those keywords explicitly: what the "
        "research uncovered for each, which ones reveal the strongest "
        "opportunity, which were dead ends and why, and the refined niches worth "
        "pursuing instead."
    ),
    "auto": (
        "MODE — AUTONOMOUS DISCOVERY. Nobody supplied a topic. The report must "
        "explain what the system found on its own, why these discoveries matter "
        "commercially, and which opportunities deserve the owner's attention "
        "first — with the evidence that justifies each."
    ),
}

_EMPTY_MODES = "MODE — report on the research exactly as the dossier presents it."


# ------------------------------------------------------------------- data in
@dataclass
class WritableReport:
    """A normalised, grounded report ready to persist."""

    title: str
    markdown: str
    figures: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    removed_figures: list[str] = field(default_factory=list)
    removed_evidence: list[str] = field(default_factory=list)
    removed_numbers: list[str] = field(default_factory=list)
    words: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "markdown": self.markdown,
            "figures": self.figures,
            "evidence_ids": self.evidence_ids,
            "removed_figures": self.removed_figures,
            "removed_evidence": self.removed_evidence,
            "removed_numbers": self.removed_numbers,
            "words": self.words,
        }


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _compact(value: Any, limit: int = 0) -> Any:
    """JSON-safe copy of `value`.

    `limit` <= 0 (the default) means NOTHING is trimmed: the writer gets the
    record exactly as it was captured. A missing review, a dropped search row
    or a clipped verdict message is a summary of evidence, and the report then
    argues from half a measurement.
    """
    if limit <= 0:
        return _json_copy(value)
    if isinstance(value, str):
        return value if len(value) <= limit else value[: limit - 1] + "…"
    if isinstance(value, list):
        out = [_compact(v, limit) for v in value[: max(1, limit // 6)]]
        if len(value) > len(out):
            out.append(f"…(+{len(value) - len(out)} more)")
        return out
    if isinstance(value, dict):
        return {str(k): _compact(v, limit) for k, v in value.items()}
    return value


def _json_copy(value: Any) -> Any:
    """Plain, JSON-safe copy (tuples become lists, anything odd becomes str)."""
    if isinstance(value, dict):
        return {str(k): _json_copy(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_copy(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _trim_evidence_data(data: dict[str, Any], limit: int = 0) -> dict[str, Any]:
    """The captured evidence data, complete unless a limit is asked for.

    Default 0 = every row and every string as captured (a review-research
    record carries its reviews, a search record its rows).
    """
    if limit <= 0:
        return _json_copy(data or {})
    out: dict[str, Any] = {}
    for key, value in (data or {}).items():
        if isinstance(value, list):
            rows = value[: max(1, limit // 6)]
            trimmed = [
                {k: (_clip(v, limit) if isinstance(v, str) else v) for k, v in row.items()}
                if isinstance(row, dict) else _clip(row, limit)
                for row in rows
            ]
            if len(value) > len(rows):
                trimmed.append(f"…(+{len(value) - len(rows)} more)")
            out[key] = trimmed
        elif isinstance(value, dict):
            out[key] = _trim_evidence_data(value, limit)
        elif isinstance(value, str):
            out[key] = _clip(value, limit)
        else:
            out[key] = value
    return out


def _screenshot_caption(ev: Any) -> str:
    data = getattr(ev, "data", None) or {}
    kind = str(getattr(ev, "kind", "") or "capture").replace("_", " ")
    market = str(getattr(ev, "marketplace", "") or "").upper()
    bits: list[str] = [market, kind]
    if data.get("query"):
        bits.append(f"search “{_clip(data['query'], 80)}”")
    total = data.get("total_results")
    if isinstance(total, int):
        approx = "over " if data.get("total_results_are_approximate") else ""
        bits.append(f"{approx}{total:,} results")
    if data.get("title"):
        bits.append(_clip(data["title"], 90))
    if data.get("price"):
        bits.append(str(data["price"]))
    if data.get("best_seller_rank"):
        bits.append(f"BSR {data['best_seller_rank']:,}")
    if data.get("review_count") is not None:
        bits.append(f"{data['review_count']:,} reviews")
    if data.get("publication_date"):
        bits.append(str(data["publication_date"]))
    return " · ".join(b for b in bits if b)


def _thought_excerpts(thoughts: Any, session_id: str) -> list[dict[str, Any]]:
    """The model's own reasoning for this run, in order and uncondensed.

    Prompt-only: never quoted and never persisted into the report body. The
    writer needs it to know what the researcher already ruled out and why; a
    summary of it would hand the analyst a summary of their own thinking.
    """
    if thoughts is None:
        return []
    out: list[dict[str, Any]] = []
    try:
        if hasattr(thoughts, "every"):
            for t in thoughts.every(session_id):
                detail = str(getattr(t, "detail", "") or "").strip()
                if not detail:
                    continue
                out.append({
                    "label": _clip(getattr(t, "label", ""), 120),
                    "at": str(getattr(t, "created_at", "") or ""),
                    "excerpt": detail,
                })
            return out
        rows = thoughts.recent(session_id, limit=200)
    except Exception:
        return []
    for row in rows:  # newest first -> reverse to keep the run's arc
        tid = row.get("id")
        try:
            full = thoughts.get(int(tid)) if tid is not None else None
        except Exception:
            full = None
        detail = str(getattr(full, "detail", "") or "").strip()
        if not detail:
            continue
        out.append({"label": _clip(row.get("label"), 120), "excerpt": detail})
    out.reverse()
    return out


def build_report_context(
    session_id: str,
    *,
    sessions: Any,
    candidates: Any,
    opportunities: Any,
    verifications: Any,
    claims: Any,
    browser_evidence: Any,
    observations: Any,
    notes: Any,
    thoughts: Any = None,
    trace: Any = None,
    artifacts: Any = None,
    model: dict[str, Any] | None = None,
    evidence: Any = None,
    phase_outputs: dict[str, Any] | None = None,
    backlog: list[dict[str, Any]] | None = None,
    owner_messages: Any = None,
    memory: Any = None,
    prior_runs: list[dict[str, Any]] | None = None,
    context_chars: int = 0,
    evidence_limit: int = 10_000_000,
) -> dict[str, Any]:
    """The complete research record, unbounded, for the report-writing model."""
    session = sessions.require(session_id)
    model = model or {}
    ex = dict(model.get("executive_summary") or {})
    quality = dict(model.get("quality_statement") or {})

    try:
        counts = sessions.counts(session_id)
    except Exception:
        counts = {}

    # ---- candidates: every row, with the recorded reason it died ----------
    cand_rows: list[dict[str, Any]] = []
    for cand in candidates.list(session_id, limit=10_000_000):
        meta = getattr(cand, "meta", None) or {}
        cand_rows.append({
            "candidate_id": cand.id,
            "niche": cand.niche,
            "status": cand.status,
            "marketplace": cand.marketplace,
            "score": cand.score,
            "rationale": str(cand.rationale or ""),
            "parent_market": meta.get("parent_market"),
            "chain": meta.get("chain"),
            "rejection_reasons": meta.get("rejection_reasons") or meta.get("reasons"),
        })
    # ---- verifications: the verdict AND the checks behind it --------------
    ver_rows: list[dict[str, Any]] = []
    for v in verifications.list(session_id, limit=10_000_000):
        ver_rows.append({
            "candidate_id": v.candidate_id,
            "verdict": v.verdict,
            "confidence": v.confidence,
            "checks": _compact(v.checks),
            "error": v.error,
        })

    # ---- evidence: every capture, with the measured numbers intact --------
    ev_rows: list[Any] = []
    try:
        ev_rows = browser_evidence.list(session_id, limit=evidence_limit)
    except Exception:
        ev_rows = []
    evidence_rows: list[dict[str, Any]] = []
    screenshots: list[dict[str, Any]] = []
    evidence_index: dict[str, dict[str, Any]] = {}
    for ev in ev_rows:
        artifact_id = getattr(ev, "screenshot_artifact_id", None)
        if artifact_id and artifacts is not None:
            try:
                if artifacts.get(session_id, artifact_id) is None:
                    artifact_id = None
            except Exception:
                artifact_id = None
        evidence_rows.append({
            "id": ev.id,
            "kind": ev.kind,
            "marketplace": ev.marketplace,
                            "title": str(ev.title or ""),
            "url": str(ev.url or ""),
            "captured_at": ev.captured_at,
            "has_screenshot": bool(artifact_id),
            "data": _trim_evidence_data(getattr(ev, "data", None) or {}),
        })
        evidence_index[ev.id] = {
            "title": _clip(ev.title or ev.url, 160),
            "kind": ev.kind,
            "marketplace": ev.marketplace,
        }
        if artifact_id:
            screenshots.append({
                "evidence_id": ev.id,
                "artifact_id": artifact_id,
                "image_url": f"/api/sessions/{session_id}/artifacts/{artifact_id}/file",
                "caption": _screenshot_caption(ev),
                "captured_at": ev.captured_at,
            })

    # ---- research journey: phases, notes, actions, reasoning --------------
    phases: list[dict[str, Any]] = []
    for phase, output in (phase_outputs or {}).items():
        if output:
            phases.append({
                "phase": phase,
                "title": PHASE_TITLES.get(str(phase), str(phase)),
                "output": _compact(output),
            })
    note_rows = []
    if notes is not None:
        try:
            for n in notes.list(session_id, limit=10_000_000):
                note_rows.append({
                    "phase": getattr(n, "phase", ""),
                    "niche": getattr(n, "niche", ""),
                    "text": str(getattr(n, "text", "") or ""),
                })
        except Exception:
            note_rows = []
    action_rows: list[dict[str, Any]] = []
    if trace is not None:
        try:
            for entry in trace.tail(session_id, limit=10_000_000):
                action_rows.append({
                    "at": getattr(entry, "created_at", ""),
                    "kind": getattr(entry, "kind", ""),
                    "text": str(getattr(entry, "text", "") or ""),
                })
        except Exception:
            action_rows = []
    reasoning = _thought_excerpts(thoughts, session_id)

    owner_rows: list[dict[str, Any]] = []
    if owner_messages is not None:
        # Every message, not a page of them; duck-typed stores that do not
        # accept a limit get the plain call.
        try:
            journal = owner_messages.journal(session_id, limit=10_000_000)
        except TypeError:
            try:
                journal = owner_messages.journal(session_id)
            except Exception:
                journal = []
        except Exception:
            journal = []
        for m in journal:
            owner_rows.append({
                "at": getattr(m, "created_at", ""),
                "text": str(getattr(m, "text", "") or ""),
            })

    obs_rows: list[dict[str, Any]] = []
    if observations is not None:
        try:
            for ob in observations.list(session_id, limit=10_000_000):
                obs_rows.append({
                    "source": getattr(ob, "source", ""),
                    "kind": getattr(ob, "kind", ""),
                    "content": str(getattr(ob, "content", "") or ""),
                    "at": getattr(ob, "created_at", ""),
                })
        except Exception:
            obs_rows = []

    claim_rows: list[dict[str, Any]] = []
    try:
        for c in claims.list(session_id, limit=10_000_000):
            claim_rows.append({
                "candidate_id": getattr(c, "candidate_id", ""),
                "statement": str(getattr(c, "statement", "") or ""),
                "evidence_ids": list(getattr(c, "evidence_ids", []) or []),
                "status": getattr(c, "status", ""),
            })
    except Exception:
        claim_rows = []

    ctx: dict[str, Any] = {
        "today": _today(),
        "session": {
            "name": session.name,
            "mode": session.mode,
            "objective": session.objective,
            "owner_brief": session.prompt,
            "marketplaces": list(session.marketplaces or []),
            "status": session.status,
            "elapsed_minutes": round(float(session.elapsed_seconds or 0) / 60),
        },
        "mode_instruction": MODE_FRAMING.get(str(session.mode), _EMPTY_MODES),
        "accounting": {
            "counts": counts,
            "candidates_considered": ex.get("candidates_considered"),
            "candidates_rejected": ex.get("candidates_rejected"),
            "survived_verification": ex.get("survived_verification"),
            "demand_verdicts": ex.get("demand_verdicts"),
            "what_was_verified": quality.get("verified"),
            "what_was_rejected": quality.get("rejected"),
            "rejection_reasons": quality.get("rejection_reasons"),
            "synthesis_rejections": quality.get("synthesis_rejections"),
            "remains_uncertain": quality.get("remains_uncertain"),
            "estimate_policy": quality.get("estimate_policy"),
        },
        "research_journey": {
            "phases": phases,
            "notes": note_rows,
            "actions": action_rows,
            "reasoning": reasoning,
            # Guidance the owner sent DURING the run. It shaped what was
            # pursued, so the report must know it.
            "owner_guidance": owner_rows,
        },
        "candidates": cand_rows,
        "verifications": ver_rows,
        "opportunities": model.get("opportunities") or [],
        "compact_opportunities": [
            o.to_dict() for o in opportunities.list(session_id, limit=60)
        ],
        "headroom_ranking": model.get("headroom_ranking") or [],
        "competitors": model.get("competitors") or [],
        "consumer_intelligence": model.get("consumer_intelligence") or {},
        "finalists": model.get("finalist_sections") or [],
        "adjacent": model.get("adjacent_ledger") or [],
        "backlog": backlog or [],
        "observations": obs_rows,
        "claims": claim_rows,
        "evidence": evidence_rows,
        "screenshots": screenshots,
        "evidence_index": evidence_index,
        "research_notes": model.get("research_notes") or [],
        # The model's own long-running memory, complete: the reasoning behind
        # every judgement it is about to write down.
        "memory": _memory_rows(memory, session_id),
        # Earlier runs of this investigation (oldest first) with their FULL
        # records — never summaries: the report must read like one analyst who
        # has followed the market from the first discovery to today.
        "prior_runs": list(prior_runs or []),
        "focus_pivots": model.get("focus_pivots") or [],
        "validation": model.get("validation") or {},
        "context_notes": [],
    }
    return _fit_context(ctx, context_chars)


def _memory_rows(memory: Any, session_id: str) -> list[dict[str, Any]]:
    """The run's long-running memory, verbatim and in order (no cap)."""
    if memory is None:
        return []
    try:
        entries = memory.list(session_id)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for e in entries:
        text = str(getattr(e, "text", "") or "")
        if not text.strip():
            continue
        out.append({
            "kind": getattr(e, "kind", "") or "memory",
            "phase": getattr(e, "phase", "") or "",
            "at": getattr(e, "created_at", "") or "",
            "text": text,
        })
    return out


def _today() -> str:
    from app.timeutil import iso_now

    return iso_now()[:10]


def _context_size(ctx: dict[str, Any]) -> int:
    try:
        return len(json.dumps(ctx, ensure_ascii=False, default=str))
    except Exception:
        return len(str(ctx))


def _thin(rows: list[Any], keep: int) -> list[Any]:
    """Sample evenly across a list, keeping both ends.

    Evidence arrives newest-first. Keeping only one end would either lose the
    discoveries that started the run or the pages that finished it, so the
    condensed dossier keeps a spread across the whole session.
    """
    if keep >= len(rows) or keep <= 0:
        return rows
    step = len(rows) / keep
    return [rows[min(len(rows) - 1, int(i * step))] for i in range(keep)]


def _fit_context(ctx: dict[str, Any], budget: int) -> dict[str, Any]:
    """Shrink the bulky, lowest-signal lists until the dossier fits.

    Never drops the accounting, the candidates, the opportunities, the measured
    finalist data or the screenshot catalogue: those are the report's spine.
    The journey and raw captures are what get condensed, and the condensation
    is recorded in `context_notes` so the model knows the record is complete in
    substance even where it was shortened for the prompt.
    """
    # 0 (or less) = no budget: the dossier is handed over whole.
    if budget <= 0:
        return ctx
    budget = max(20_000, int(budget))
    if _context_size(ctx) <= budget:
        return ctx
    notes: list[str] = ctx.setdefault("context_notes", [])
    for fraction in (0.7, 0.5, 0.35, 0.25, 0.18, 0.12):
        evidence = ctx.get("evidence") or []
        if evidence:
            keep = max(40, int(len(evidence) * fraction))
            if keep < len(evidence):
                ctx["evidence"] = _thin(evidence, keep)
                notes.append(
                    f"browser captures condensed: {len(evidence) - keep} of "
                    f"{len(evidence)} records were omitted to fit the prompt; "
                    "the rest are sampled across the whole run (earliest and "
                    "newest included), and the actions/notes keep the omitted "
                    "observations in summary form"
                )
        obs = ctx.get("observations") or []
        if obs:
            ctx["observations"] = obs[-max(30, int(len(obs) * fraction)):]
        claims_rows = ctx.get("claims") or []
        if claims_rows:
            ctx["claims"] = claims_rows[-max(30, int(len(claims_rows) * fraction)):]
        journey = ctx.get("research_journey") or {}
        if journey.get("actions"):
            journey["actions"] = journey["actions"][-max(40, int(len(journey["actions"]) * fraction)):]
        if journey.get("reasoning"):
            journey["reasoning"] = journey["reasoning"][-max(6, int(len(journey["reasoning"]) * fraction)):]
        if _context_size(ctx) <= budget:
            return ctx
    # Last resort: shrink each phase output's text.
    for limit in (8000, 4000, 2000):
        journey = ctx.get("research_journey") or {}
        phases = journey.get("phases") or []
        for row in phases:
            row["output"] = _compact(row.get("output"), limit)
        if _context_size(ctx) <= budget:
            break
    notes.append("the dossier was condensed to fit the prompt; nothing measured was altered")
    return ctx


def context_json(ctx: dict[str, Any]) -> str:
    return json.dumps(ctx, ensure_ascii=False, default=str)


def prompt_context(ctx: dict[str, Any]) -> dict[str, Any]:
    """The dossier as the model should see it.

    Internal plumbing (artifact ids, resolved image URLs, the citation index) is
    omitted: the model selects screenshots as `shot:EVIDENCE_ID` and cites
    `[ev:ID]`, and the backend resolves both.
    """
    view = {k: v for k, v in ctx.items() if k != "evidence_index"}
    view["screenshots"] = [
        {k: s.get(k) for k in ("evidence_id", "caption", "captured_at")}
        for s in (ctx.get("screenshots") or []) if isinstance(s, dict)
    ]
    return view


def user_prompt(ctx: dict[str, Any]) -> str:
    return (
        "RESEARCH DOSSIER — the complete record of this run. Everything here was "
        "measured, observed, decided or reasoned during the research; anything "
        "not here was not established.\n"
        + context_json(prompt_context(ctx))
        + "\n\nTASK — write the final report now, following the instructions in "
        "your system prompt: answer the owner's objective, lead with the "
        "strongest opportunity, show what competitors are doing and what they "
        "get wrong, ground every load-bearing claim with [ev:ID], and embed only "
        "the screenshots that prove something as ![caption](shot:EVIDENCE_ID). "
        + (
            " 'prior_runs' holds your own earlier work on this investigation, "
            "oldest first and in full: build on it, keep what still holds, and "
            "make this report cumulative — the owner should read it as one "
            "analyst's account of the whole opportunity, updated to today."
            if ctx.get("prior_runs") else ""
        )
        + " Reply with the report markdown only."
    )


def correction_prompt(ctx: dict[str, Any], removed_numbers: list[str],
                      removed_figures: list[str]) -> str:
    bits = []
    if removed_numbers:
        bits.append(
            "these figures did not appear anywhere in the dossier and were "
            "removed: " + ", ".join(sorted(set(removed_numbers))[:20])
        )
    if removed_figures:
        bits.append(
            "these screenshot references named no captured screenshot and were "
            "dropped: " + ", ".join(sorted(set(removed_figures))[:10])
        )
    if not bits:
        bits.append("the reply was empty or did not survive evidence grounding")
    return (
        user_prompt(ctx)
        + "\n\nCORRECTION REQUIRED — your previous report was not usable: "
        + "; ".join(bits)
        + ". Rewrite it using ONLY figures and evidence ids present in the "
        "dossier. Do not compute new numbers. Keep the same analytical quality "
        "and length. Reply with the report markdown only."
    )


# --------------------------------------------------------------- normalising
_FENCE_RE = re.compile(r"^```(?:markdown|md)?\s*\n(.*?)\n```\s*$", re.DOTALL)
_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
# Citations and screenshot tokens carry hex ids. Grounding scans for numbers,
# so they are swapped for digit-free placeholders before it runs and restored
# afterwards — otherwise every sentence citing evidence would look like it
# quoted an unmeasured figure.
_TOKEN_RE = re.compile(
    r"!\[([^\]]*)\]\(\s*shot:([A-Za-z0-9_\-]+)\s*\)"
    r"|\[ev:([A-Za-z0-9_\-]+)\]"
)
_PLACEHOLDER_RE = re.compile("\u2401([a-j]+)\u2401")


def _placeholder(index: int) -> str:
    letters = "".join(chr(97 + int(d)) for d in str(index))
    return f"\u2401{letters}\u2401"


def _placeholder_index(match: re.Match[str]) -> int:
    return int("".join(str(ord(c) - 97) for c in match.group(1)))


def normalize_report(
    raw: str,
    *,
    session_id: str,
    allowed: set[str],
    screenshots: dict[str, str],
    evidence_index: dict[str, dict[str, Any]],
    fallback_title: str = "Market Intelligence Report",
) -> WritableReport | None:
    """Turn one model reply into a grounded, renderable report.

    `screenshots` maps evidence id -> artifact URL for this session;
    `evidence_index` is the set of evidence records the report may cite.
    Returns None when nothing usable survives.
    """
    text = (raw or "").strip()
    if not text:
        return None
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()

    title = ""
    match = _H1_RE.search(text)
    if match:
        title = _clip(" ".join(match.group(1).split()), 160)
        text = (text[: match.start()] + text[match.end():]).lstrip("\n").strip()
    if not title:
        title = fallback_title

    tokens: list[tuple[str, str, str]] = []

    def _stash(m: re.Match[str]) -> str:
        if m.group(2):
            tokens.append(("shot", m.group(2), " ".join((m.group(1) or "").split())))
        else:
            tokens.append(("ev", m.group(3), ""))
        return _placeholder(len(tokens) - 1)

    text = _TOKEN_RE.sub(_stash, text)
    grounded, removed_numbers = ground_text(text, allowed)
    body_len = len(grounded.strip())
    if body_len < 200:
        # Nothing usable survived: the runner re-asks the model rather than
        # shipping a fragment (and never writes a report itself).
        return None
    if len(text.strip()) > 400 and body_len < int(len(text.strip()) * 0.35):
        # Grounding gutted the document (most of it was unmeasured claims).
        return None

    removed_figures: list[str] = []
    removed_evidence: list[str] = []
    figures: list[str] = []
    cited: list[str] = []

    def _restore(m: re.Match[str]) -> str:
        kind, evidence_id, caption = tokens[_placeholder_index(m)]
        if kind == "shot":
            url = screenshots.get(evidence_id)
            if not url:
                removed_figures.append(evidence_id)
                return ""
            if evidence_id not in figures:
                figures.append(evidence_id)
            return f"![{caption or 'Research capture'}]({url})"
        row = evidence_index.get(evidence_id)
        if not row:
            removed_evidence.append(evidence_id)
            return ""
        if evidence_id not in cited:
            cited.append(evidence_id)
        label = _clip(row.get("title") or evidence_id, 70).replace("]", ")").replace("[", "(")
        kind_txt = str(row.get("kind") or "evidence").replace("_", " ")
        market = str(row.get("marketplace") or "").upper()
        tooltip = " · ".join(x for x in (market, kind_txt, label) if x).replace('"', "'")
        return f"[ev:{evidence_id}](/api/sessions/{session_id}/evidence/{evidence_id} \"{tooltip}\")"

    grounded = _PLACEHOLDER_RE.sub(_restore, grounded)
    return WritableReport(
        title=title,
        markdown=grounded.strip(),
        figures=figures,
        evidence_ids=cited,
        removed_figures=removed_figures,
        removed_evidence=removed_evidence,
        removed_numbers=removed_numbers,
        words=len(re.findall(r"[A-Za-z0-9']+", grounded)),
    )


def allowed_for(ctx: dict[str, Any]) -> set[str]:
    """Every figure the report may quote: the dossier minus annotations.

    The model's reasoning and its run notes are memory, not measurements. They
    ARE shown to the writer (they explain the journey and carry observations
    no schema field holds), but a figure that exists only there must never be
    quotable as a measured fact.
    """
    sanitised = dict(ctx)
    journey = dict(ctx.get("research_journey") or {})
    journey.pop("reasoning", None)
    journey.pop("notes", None)
    journey.pop("owner_guidance", None)
    sanitised["research_journey"] = journey
    sanitised["research_notes"] = []
    return allowed_numbers(json.dumps(sanitised, ensure_ascii=False, default=str))


def screenshot_map(ctx: dict[str, Any]) -> dict[str, str]:
    """evidence id -> inline image URL, for the screenshots the run captured."""
    out: dict[str, str] = {}
    for shot in ctx.get("screenshots") or []:
        if not isinstance(shot, dict):
            continue
        evidence_id = str(shot.get("evidence_id") or "")
        url = str(shot.get("image_url") or "")
        if evidence_id and url:
            out[evidence_id] = url
    return out
