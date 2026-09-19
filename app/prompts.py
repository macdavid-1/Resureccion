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
        "- Evidence must come from pages you actually inspected via actions. "
        "Never fabricate rankings, prices, review counts, or KDSpy numbers.\n"
        "- If evidence is missing, request more actions; do not guess.\n"
        "- Convert observations into concrete, actionable judgments.\n\n"
        f"PHASE CONTRACT — allowed actions: {', '.join(spec.allowed_actions)}.\n"
        f"Required output JSON fields: {', '.join(spec.output_contract)}.\n"
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

    if prior_phase_outputs:
        parts.append(
            "\nPRIOR PHASE DECISIONS: " + _compact(prior_phase_outputs, limit=4000)
        )

    # --- timing + anti-stall -------------------------------------------------
    t = timing.digest()
    parts.append(
        "\nTIMING: " + _compact(t)
        + f"\nStall policy: you have produced no new evidence for "
        f"{t['iterations_without_progress']} iteration(s); at 3 the orchestrator "
        "forces a pivot. If stuck (page failing, no results, repeated errors), "
        "PIVOT: choose a different query, marketplace, or candidate — never "
        "wait indefinitely."
    )

    # --- output format -------------------------------------------------------
    parts.append(
        "\nRESPOND WITH JSON ONLY, an object with exactly these keys: "
        + _compact(list(spec.output_contract))
        + " — plus optional key 'actions': a list of browser actions you want "
        "executed BEFORE your analysis is finalized, each "
        '{"action": "<one of the allowed actions>", "args": {...}}. '
        "If 'actions' is present and non-empty, the orchestrator will run them "
        "and re-prompt you with fresh evidence; keep analysis fields filled "
        "with your best current judgment in that case."
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
