"""Per-phase prompt builders for the research agent.

The methodology (app/methodology.py) defines WHAT each phase requires; these
builders translate that into model prompts. Prompts always include:

- the phase contract (allowed actions, output fields, guidance),
- a compact evidence digest (never raw pages — structured, redacted records),
- deterministic timing information (phase elapsed, session elapsed,
  iterations, stall counter),
- the depth-priority law and the anti-stall pivot rule.

The model reasons INSIDE the contract; the backend owns the lifecycle.
"""
from __future__ import annotations

import json
from typing import Any

from app.methodology import (
    PHASES_ALLOWING_EXTERNAL as EXTERNAL_PHASES,
    PHASE_TITLES,
    SPECS,
    PhaseTiming,
    depth_priority_text,
)


def _compact(value: Any, limit: int = 2400) -> str:
    """Compact JSON rendering bounded in size for prompt inclusion."""
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


# Per-record size cap when rendering digests: a single huge evidence record
# (e.g. a full search result set) must not crowd out the rest of the prompt.
_DIGEST_RECORD_LIMIT = 900


def build_system_prompt(phase: str) -> str:
    spec = SPECS[phase]
    return (
        "You are the reasoning core of Resurrección, a rigorous KDP market "
        "research agent. You operate INSIDE a fixed, deterministic research "
        "methodology; you do not invent your own process. You are now in "
        f"{PHASE_TITLES[phase]}.\n\n"
        "METHODOLOGY LAW:\n"
        f"- {depth_priority_text()}\n"
        "- Two modes of work: EXPLORE (request browser actions — they run and "
        "fresh evidence comes back) and COMMIT (no actions, complete output). "
        "Only a commit ends the phase; an explore step can never also close it.\n"
        "- Depth beats breadth: open competitor product pages and read reviews "
        "instead of re-running searches you already ran. Re-capturing the same "
        "source is a duplicate, not research, and does not count as progress.\n"
        "- Evidence must come from pages you actually inspected via actions. "
        "Never fabricate rankings, prices, review counts, or KDSpy numbers.\n"
        "- If evidence is missing, request more actions; do not guess.\n"
        "- Amazon sign-in is optional. When a page hits a sign-in wall the "
        "action fails and YOU decide: skip that source (e.g. review mining) "
        "and continue, or try another marketplace/page. When reviews or "
        "other data are visible without signing in, capture them.\n"
        "- Convert observations into concrete, actionable judgments.\n\n"
        f"PHASE CONTRACT — allowed actions: {', '.join(spec.allowed_actions)}.\n"
        + (
            "External sources (forums / Google suggestions / Goodreads): "
            "allowed in this phase as CORROBORATION only — they enter the same "
            "evidence table, go through redaction, claims, filters and gates, "
            "and can never override a verdict. "
            + (
                "In Phase 1 they are additionally gated by the orchestrator: "
                "autonomous runs only, and only AFTER Amazon-side anchors "
                "exist — unanchored forum noise may not set the run's direction.\n"
                if phase == "opportunity_discovery" else
                "Use them where Amazon's own signal is thin (unmet-need "
                "language, independent demand counts).\n"
            )
            if phase in EXTERNAL_PHASES else ""
        )
        + f"Required output JSON fields: {', '.join(spec.output_contract)}.\n"
        f"Phase guidance: {spec.guidance}"
    )


def build_user_prompt(
    phase: str,
    *,
    mode: str,
    objective: str,
    prompt_text: str,
    marketplaces: list[str],
    marketplace_mode: str,
    evidence_digest: list[dict[str, Any]],
    candidates_digest: list[dict[str, Any]],
    prior_phase_outputs: dict[str, Any] | None,
    timing: PhaseTiming,
    images_note: str = "",
    screenshot_attached: bool = False,
    owner_messages: list[str] | None = None,
    pivot_directive: str | None = None,
    phase_gate: dict[str, Any] | None = None,
    census_digest: list[dict[str, Any]] | None = None,
    frontier_terms: list[str] | None = None,
    market_memory: list[dict[str, Any]] | None = None,
    headroom_priority: list[str] | None = None,
    adjacency_note: str = "",
) -> str:
    spec = SPECS[phase]
    parts: list[str] = []

    # --- session brief -----------------------------------------------------
    brief: dict[str, Any] = {
        "mode": mode,
        "objective": objective or None,
        "owner_brief": prompt_text or None,
        "marketplaces": marketplaces,
        "marketplace_mode": marketplace_mode,
    }
    if images_note:
        brief["reference_images"] = images_note
    parts.append("SESSION BRIEF: " + _compact({k: v for k, v in brief.items() if v}))

    # --- deterministic census (measured discovery input) --------------------
    # Gather by the BACKEND before the model opines (app.census): the walk of
    # the bestseller/new-release charts and the browse-node tree, so discovery
    # reasons over measured demand instead of guessing where to look.
    if census_digest:
        parts.append(
            "\nMARKET CENSUS (deterministic backend walk of the bestseller and "
            "new-release charts plus the browse-node categories — measured "
            "before this phase started; the evidence ids are stable):\n"
            + _compact(census_digest, limit=3000)
            + "\nUse it as your map: regions with strong demand and light "
            "top-review barriers are where hidden markets live. Ask for more "
            "category/chart actions to walk deeper where the census looks "
            "promising."
        )

    # --- autocomplete frontier (demand language) ----------------------------
    if frontier_terms:
        parts.append(
            "\nAUTOCOMPLETE FRONTIER (phrases real buyers actually type, mined "
            "deterministically from Amazon's suggestion stream):\n"
            + _compact(list(frontier_terms)[:60], limit=2400)
            + "\nThese are demand-language seeds: a long phrase with thin "
            "results is hidden demand in plain sight. Search the ones that "
            "match your signals and niche them down."
        )

    # --- strategic focus pivot (autonomous re-discovery) --------------------
    # Present only when the previous research pass died with zero verified
    # candidates and the session re-entered discovery on a new focus. The
    # directive is deterministic (app.methodology.focus_pivot_directive).
    if pivot_directive:
        parts.append(
            "\nSTRATEGIC PIVOT — this pass is a fresh start on a NEW focus "
            "(the previous one failed adversarial verification entirely):\n"
            + pivot_directive
        )

    # --- deterministic exit gate (explore vs commit) -----------------------
    # Shown so the model knows exactly how much research the phase still owes
    # before it may close, instead of discovering it by being rejected.
    if phase_gate:
        g = phase_gate
        floor = int(g.get("min_output_items") or 0)
        parts.append(
            "\nPHASE EXIT GATE (deterministic — the orchestrator, not you, "
            "decides when this phase ends): " + _compact(g) + "\n"
            "- EXPLORE: a reply carrying a non-empty 'actions' list runs those "
            "actions and re-prompts you with fresh evidence. It never ends the phase.\n"
            "- COMMIT: reply with NO 'actions' key, with the contract fully "
            "filled, to end the phase.\n"
            f"- output: '{g.get('primary_output', '')}' must hold at least "
            f"{floor} item(s)"
            + (
                " before commit — this phase has upstream input to work from, "
                "so it may not conclude empty."
                if floor > 0 else
                " — an empty list is acceptable here if that is genuinely "
                "what the evidence shows."
            )
            + "\n- Only genuinely NEW sources open the gate: re-running a search "
              "you already ran re-captures the same source and does not count."
        )

    # --- phase-specific instruction ----------------------------------------
    parts.append(f"\nTASK ({PHASE_TITLES[phase]}):\n{spec.guidance}")

    # --- evidence digest ----------------------------------------------------
    bounded_evidence = [
        {k: (v if not isinstance(v, (dict, list)) else _compact(v, _DIGEST_RECORD_LIMIT))
         for k, v in item.items()}
        for item in (evidence_digest or [])
    ]
    parts.append(
        "\nEVIDENCE SO FAR (structured, redacted; ids are stable — reference them): "
        + (_compact(bounded_evidence) if bounded_evidence else "[] (none yet — gather some)")
    )

    # --- candidates digest --------------------------------------------------
    bounded_candidates = [
        {k: (v if not isinstance(v, (dict, list)) else _compact(v, _DIGEST_RECORD_LIMIT))
         for k, v in item.items()}
        for item in (candidates_digest or [])
    ]
    parts.append(
        "\nCANDIDATES: "
        + (_compact(bounded_candidates) if bounded_candidates else "[] (none yet)")
    )

    # --- headroom ordering + adjacency (deterministic; orders, never rules) --
    if headroom_priority:
        parts.append(
            "\nHEADROOM PRIORITY (deterministic demand÷competition score — it "
            "only orders your work and the report; filters, gates and "
            "verification still decide what survives):\n- "
            + "\n- ".join(headroom_priority)
            + "\nSpend iterations on the highest-headroom candidates first, "
            "and keep watching for adjacent markets beside them."
        )
    if adjacency_note:
        parts.append("\nADJACENCY: " + adjacency_note)

    if prior_phase_outputs:
        parts.append(
            "\nPRIOR PHASE DECISIONS: " + _compact(prior_phase_outputs, limit=4000)
        )

    # --- cross-session market memory ----------------------------------------
    # One owner-scoped knowledge base accumulated over earlier runs. Fresh
    # rows (< MEMORY_FRESH_DAYS) are measured facts to REUSE; stale rows are
    # orientation hints that must be cheaply re-verified before being relied
    # on — never presented in the report as current.
    if market_memory:
        parts.append(
            "\nMARKET MEMORY from earlier runs (fresh = reuse as fact, no need "
            "to re-measure; stale = cheap re-verification before relying on "
            "it — it is a hint, not a fact):\n"
            + _compact(market_memory, limit=3000)
        )

    # --- owner messages (durable mid-research guidance) ---------------------
    # Every note the owner has ever typed during this session, re-included on
    # every iteration: delivery never interrupts the run and no line of
    # context is ever lost. Messages marked [new] arrived since the last
    # iteration and have just been consumed exactly-once.
    if owner_messages:
        rendered = [f"[new] {m}" if i == len(owner_messages) - 1 else m for i, m in enumerate(owner_messages)]
        parts.append(
            "\nOWNER GUIDANCE (direct messages from the session owner; they "
            "remain authoritative for the rest of the research — follow any "
            "new instruction in your next actions and analyses): "
            + _compact(rendered, limit=2000)
        )

    # --- timing + anti-stall -------------------------------------------------
    t = timing.digest()
    parts.append(
        "\nTIMING: " + _compact(t)
        + f"\nStall policy: {t['iterations_without_progress']} iteration(s) have "
        "produced no NEW distinct evidence; at 3 the orchestrator forces the "
        "phase to commit whatever it has and moves on. If stuck (page failing, "
        "no results, repeated errors, or you keep re-running the same query), "
        "PIVOT: choose a different query, marketplace, page type, or candidate — "
        "never wait indefinitely and never repeat a search you already ran."
    )

    # --- output format -------------------------------------------------------
    parts.append(
        "\nRESPOND WITH JSON ONLY, an object with exactly these keys: "
        + _compact(list(spec.output_contract))
        + " — plus optional key 'actions': a list of browser actions you want "
        "executed BEFORE your next analysis, each "
        '{"action": "<one of the allowed actions>", "args": {...}}. '
        "If 'actions' is present and non-empty this is an EXPLORE step: the "
        "orchestrator runs the actions and re-prompts you with fresh evidence; "
        "keep the analysis fields filled with your best current judgment, but "
        "know the phase stays open. Leave 'actions' out entirely to COMMIT and "
        "end the phase."
    )
    parts.append(
        "\nOPERATOR VISIBILITY: additionally include the optional key "
        "'activity_note': ONE concise sentence (max ~140 chars) describing "
        "what you are doing right now and why — e.g. 'Investigating the "
        "competitor cluster around grief planners because three independent "
        "demand signals were observed.' This is shown to the owner as the "
        "live research trace; it is an operational summary, NOT your private "
        "reasoning — never expose chain-of-thought in it."
    )

    # --- vision ---------------------------------------------------------------
    if screenshot_attached:
        parts.append(
            "\nSCREENSHOT: The most recent image attached below is the live "
            "browser viewport from the last action. Use it as primary visual "
            "ground truth: read rankings, prices, review counts, KDSpy panel "
            "values, CAPTCHA/login walls, and layout directly from it. If the "
            "screenshot and the structured evidence disagree, trust the "
            "screenshot and request a re-capture."
        )
    else:
        parts.append(
            "\nSCREENSHOT: none attached — if the phase would benefit from "
            "visual confirmation, request an action that navigates/opens a "
            "page and one will be attached next iteration."
        )
    return "\n".join(parts)
