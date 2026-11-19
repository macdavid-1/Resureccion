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
    OPPORTUNITY_FIELDS,
    P1_DISCOVERY,
    P2_NICHING,
    P3_COMPETITION,
    P9_SYNTHESIS,
    PHASE_ORDER,
    PHASE_TITLES,
    SPECS,
    PhaseTiming,
    depth_priority_text,
)
from app.methodology import (
    PHASES_ALLOWING_EXTERNAL as EXTERNAL_PHASES,
)
from app.research_memory import render_memory


def _compact(value: Any, limit: int = 0) -> str:
    """Compact JSON rendering of `value`.

    `limit` <= 0 (the default) means NO truncation: the model gets the whole
    record. A truncated digest is a summary, and a summary of evidence is not
    evidence — the model then reasons from half a measurement. Callers that
    genuinely need a cap (a closed set of field names, a small header) pass one
    explicitly.
    """
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(value)
    if limit > 0 and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


# Per-record size cap when rendering digests. 0 = a whole record, however big:
# a truncated capture is a summary, and the model must see what was measured.
_DIGEST_RECORD_LIMIT = 0


# ------------------------------------------------------- owner precedence
def owner_precedence_block(*, notes_field: bool = True) -> str:
    """The owner-over-defaults rule, shared by every system prompt.

    The owner of the research outranks the defaults in these prompts: their
    brief, their mid-run messages, any input at all wins a conflict. The only
    exception is input that is evidently a mistake — then the model keeps to
    the default and says so, instead of silently doing something nobody meant.
    """
    discrepancy = (
        'Record the discrepancy in "notes" (the optional top-level key) so '
        "the owner can correct you."
        if notes_field else
        "Do not silently absorb it: say so in one sentence, in the part of "
        "your answer where it belongs, so the owner can correct you."
    )
    return (
        "OWNER PRECEDENCE — this rule outranks everything below it:\n"
        "The owner of this research is the authority on this run. Every "
        "instruction in this prompt is a DEFAULT, not a law.\n"
        "- If anything the owner says — the brief, a posted message, a "
        "correction, a preference, a direction, an objection, any input at "
        "all, at any time — contradicts, narrows or overrides anything in "
        "this prompt, directly or indirectly, FOLLOW THE OWNER, even when "
        "you believe the default would be better.\n"
        "- The one exception: if the owner's input is evidently a MISTAKE — a "
        "typo, a slipped word, a contradiction with what they said moments "
        "earlier, a request that cannot be what they meant — do NOT silently "
        "obey it. Keep to the instruction it contradicted. " + discrepancy + "\n"
        "- Silence is not a contradiction: where the owner said nothing, "
        "these instructions stand.\n"
    )


MEMORY_INSTRUCTION = (
    "LONG-RUNNING MEMORY — your own record of this research, kept by YOU:\n"
    "The fields of the output contract store what the backend can measure; the "
    "memory you keep stores what YOU know. Use the 'remember' action to write "
    "an entry whenever something would be expensive to rediscover: a finding "
    "and the evidence behind it, a decision and the reason for it, a niche you "
    "ruled out (and exactly why), a doubt you still have, the next thing you "
    "intend to check, or a correction to something you wrote earlier.\n"
    "Every entry is stored verbatim and handed back to you IN FULL on every "
    "step, oldest first, for the rest of this run — and a follow-up run "
    "inherits all of it. Nothing is summarized, nothing is dropped, and there "
    "is no length limit: write as much detail as your future self will need. "
    "Do not restate what a field already carries; write the reasoning, the "
    "context and the judgement that fields cannot hold.\n"
    "You may also consolidate at any point in the optional top-level key "
    "'memory' (a string, or a list of {\"text\": \"…\", \"kind\": \"…\"}) — "
    "most usefully just before you commit a phase: what you learned, what you "
    "decided, what you still owe. Consolidating before a commit costs one "
    "field and saves you from re-deriving it later."
)


NOTES_INSTRUCTION = (
    "NOTES — the escape hatch for what no field holds:\n"
    "The output contract names what the backend can store structurally. You "
    "will learn other things that fit no field: a dead end worth remembering, "
    "a quirk in a category tree, a promising tangent, why a niche is softer "
    "than it looks, a conflict between the owner and these instructions. Put "
    "them in the OPTIONAL top-level key \"notes\": a list of short strings, or "
    "objects {\"text\": \"…\", \"niche\": \"…\", \"kind\": \"…\"}.\n"
    "Notes are stored durably, travel with the session, and are handed back "
    "to you when the final report is written — write them for your future "
    "self, in your own words. Never put a fabricated figure in a note: notes "
    "are annotations, never evidence, and no note may claim a measurement you "
    "did not capture."
)


def continuation_phase_note(phase: str) -> str:
    """What a continuation changes about a phase's fresh-run guidance.

    The methodology's guidance is written for a run that begins with nothing.
    A follow-up begins with its previous run's whole record, so for the two
    phases that build a market pool from scratch the guidance must not send
    the model looking for unrelated new markets.
    """
    if phase == P1_DISCOVERY:
        return (
            "THIS RUN IS A CONTINUATION — the guidance above is the fresh-run "
            "default. The previous run's signals, candidates and measurements "
            "are already in SESSION BRIEF: continue THAT investigation. Do not "
            "generate a broad pool of unrelated market signals and do not open "
            "a fresh autocomplete/keyword sweep to find new markets. If the "
            "record already says what comes next, answer with 'goto_phase' "
            "and no actions — you choose the first move, not the methodology."
        )
    if phase == P2_NICHING:
        return (
            "THIS RUN IS A CONTINUATION — narrow the candidates the previous "
            "run left open (its record names them), never a new pool of broad "
            "markets. A follow-up may owe this phase nothing at all."
        )
    return ""


def build_system_prompt(phase: str, *, continuation: bool = False) -> str:
    spec = SPECS[phase]
    return (
        owner_precedence_block()
        + "\n"
        + "You are the reasoning core of Resurrección, a rigorous KDP market "
        "research agent. You operate INSIDE a fixed, deterministic research "
        "methodology; you do not invent your own process. You are now in "
        f"{PHASE_TITLES[phase]}.\n\n"
        "METHODOLOGY LAW:\n"
        f"- {depth_priority_text()}\n"
        "- HARD RULES — these outrank everything else in this prompt, "
        "including \"be creative\" and any attractive region the census or a "
        "chart suggests: (1) NEVER propose or research a journal, logbook, "
        "notebook or diary product, in any phrasing (\"grief journal\", "
        "\"5-minute journaling prompts\", \"gratitude log book\"); (2) NEVER "
        "propose a generic market or a generic audience (\"self help\", "
        "\"books for women\", \"mindfulness\", \"parenting for beginners\") — "
        "a niche must name WHO has WHAT problem in WHICH context. "
        "A candidate that breaks either rule is refused before it is "
        "researched and does not count as output, so proposing one wastes "
        "the phase.\n"
        "- Two modes of work: EXPLORE (request browser actions — they run and "
        "fresh evidence comes back) and COMMIT (no actions, complete output). "
        "Only a commit ends the phase; an explore step can never also close it.\n"
        "- Depth beats breadth: open competitor product pages and read reviews "
        "instead of re-running searches you already ran. Re-capturing the same "
        "source is a duplicate, not research, and does not count as progress.\n"
        "- Evidence must come from pages you actually inspected via actions. "
        "Never fabricate rankings, prices, review counts, or KDSpy numbers.\n"
        "- EVERY analysis item you emit must carry 'evidence_ids': the ids from "
        "EVIDENCE SO FAR that the finding rests on. A finding with no id is a "
        "model assertion: it is recorded as unsupported, it counts against the "
        "session's integrity, and it can make the final report provisional. "
        "If a claim has no captured evidence behind it, do not assert it — say "
        "what is missing instead.\n"
        "- KDSpy Pro figures are third-party ESTIMATES, never measurements: "
        "treat them as leads. They may never decide a candidate on their own — "
        "confirm anything that matters with a real page capture (search result "
        "totals, product pages, reviews), and call any figure you repeat an "
        "estimate.\n"
        "- Competition is measured, not guessed: Amazon prints its own OVERALL "
        "total on every search page (\"1-16 of over 30,000 results\") and the "
        "capture stores it as 'result_count' / 'total_results' once the "
        "shipping-country notice is dismissed. Quote THAT number when you judge "
        "competition, and prefer searches in the relevant department ('books' "
        "or 'kindle'). 'results_captured' is only how many rows the snapshot "
        "pulled — it is never market size. A capture whose 'result_count' is "
        "null has NO usable competition figure: scroll the results (or open "
        "page 2) and capture again instead of assuming the row count.\n"
        "- The search-result total is UNCLEAR while the shipping-country "
        "notice (\"We're showing you items that ship to …\") is on the page: "
        "it covers the printed count, so a number read through it is a guess. "
        "The backend dismisses the notice before every capture, so a captured "
        "total is already clean — check 'results_count_clear' in the evidence "
        "and never quote a total from a capture where it is false. The moment "
        "you SEE the notice in a screenshot, dismiss it yourself, before you "
        "read or judge anything on that page: {\"action\": \"click\", \"args\": "
        "{\"target\": \"Dismiss\"}} — and again every time it reappears.\n"
        "- You drive a REAL browser with no restricted list of places or verbs: "
        "'navigate' (any url), 'click' (target), 'type' (target, text), "
        "'press' (key), 'scroll' (direction, amount), 'hover', "
        "'select_option' (target, value), 'evaluate' (js — real page "
        "JavaScript), 'wait_for' (target or seconds), 'go_back', 'go_forward', "
        "'reload' and 'screenshot'. 'browser' runs any of those by name "
        "({\"op\": \"scroll\", \"direction\": \"down\"}). Click by visible label "
        "(\"Dismiss\") or by CSS selector when the label is ambiguous.\n"
        "- SCROLL whenever what you need may be below the fold — more results, "
        "review text, the printed result total, a competitor's detail rows, "
        "KDSpy's panel — and scroll again before you conclude a page \"has "
        "nothing\". Type into Amazon's own search box and read its "
        "autocomplete suggestions as they appear; click KDSpy's injected "
        "controls to open it on a search page. Use 'evaluate' when a number, "
        "panel or hidden section is on the page but no verb reaches it.\n"
        "- REVIEWS ARE THE DEEPEST BUYER SIGNAL. 'read_reviews' (asin) opens "
        "the book, scrolls to the review section, expands the truncated "
        "bodies, reads the printed rating distribution and screenshots what "
        "it read; the evidence it returns carries the review text plus the "
        "star split and your own counts of critical/verified reviews. Use it "
        "on the leading competitor AND the best-rated smaller one for every "
        "serious candidate — their complaints differ and the gap usually sits "
        "between them. Turn recurring complaints into book-design decisions; "
        "never declare a niche's readers satisfied without their reviews.\n"
        "- Avoid tunnel vision: depth means chasing the MEDIUM (reviews, the "
        "leader's exact angle, the price ladder, the category a book actually "
        "ranks in, the language buyers use), not re-running one search. When a "
        "candidate keeps looking strong, spend extra passes on its variants "
        "(audience, format, promise) and on one adjacent niche. A candidate "
        "that survives that is worth more than ten that survived one search.\n"
        "- Your own records are editable mid-run: 'add_candidate' (niche, "
        "marketplace, rationale, score) for a niche you actually found while "
        "browsing, 'update_candidate' "
        "(candidate_id or niche, plus any of niche, marketplace, rationale, "
        "score, status), 'delete_candidate', 'update_opportunity' / "
        "'delete_opportunity' (opportunity_id or title), 'update_evidence' "
        "(evidence_id + data patch, or total_results to correct a "
        "competition figure), 'delete_evidence', 'update_claim' / "
        "'delete_claim'. Correct a row the evidence contradicts instead of "
        "leaving a known-wrong record for the report.\n"
        "- If evidence is missing, request more actions; do not guess.\n"
        "- Amazon sign-in is optional. When a page hits a sign-in wall the "
        "action fails and YOU decide: skip that source (e.g. review mining) "
        "and continue, or try another marketplace/page. When reviews or "
        "other data are visible without signing in, capture them.\n"
        "- Convert observations into concrete, actionable judgments.\n"
        "- " + MEMORY_INSTRUCTION.replace("\n", " ") + "\n"
        "- " + NOTES_INSTRUCTION.replace("\n", " ") + "\n"
        "- PHASE SWITCHING: you may leave this phase at will and jump to any "
        "other phase of the methodology — the usual reason is that "
        "verification left the owner's niche target unmet and discovery must "
        "run again. Reply with the optional key \"goto_phase\": \"<phase>\" "
        f"(one of: {', '.join(PHASE_ORDER)}) and NO 'actions' in that reply. "
        "Nothing is lost by switching: verified candidates stay verified and "
        "are never re-verified, rejected niches stay rejected, and every "
        "phase's analysis so far travels with you as PRIOR PHASE DECISIONS. "
        "Do not use it to skip verification or to dodge a phase whose input "
        "you still owe.\n\n"
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
        + f"Required output JSON fields: {', '.join(spec.output_contract)}. "
        "Optional keys: 'actions', 'activity_note', 'goto_phase', 'notes', "
        "'memory' (your long-running memory entry for this step).\n"
        f"Phase guidance: {spec.guidance}"
        + (
            "\n" + continuation_phase_note(phase)
            if continuation and continuation_phase_note(phase) else ""
        )
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
    depth_note: str = "",
    census_digest: list[dict[str, Any]] | None = None,
    market_memory: list[dict[str, Any]] | None = None,
    headroom_priority: list[str] | None = None,
    adjacency_note: str = "",
    target_note: str = "",
    recent_actions: list[dict[str, Any]] | None = None,
    continuation: bool = False,
    brief_limit: int = 0,
    memory_entries: list[Any] | None = None,
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
    # The owner brief can be large on purpose: a follow-up run's prompt is the
    # previous run's whole record (its brief, every owner message, the report,
    # the measurements, notes, reasoning and trace). The caller passes the
    # budget that section needs — a generic digest cap must never decide how
    # much of a continuation the model gets to see.
    if continuation and prompt_text:
        # Handed over as TEXT, not as an escaped JSON string: the record is a
        # document (markdown report, quoted reasoning, instructions), and
        # escaping every newline and quote of it makes it markedly harder to
        # read and act on. The small header stays structured.
        header = {k: v for k, v in brief.items() if k not in ("owner_brief",)}
        parts.append(
            "SESSION BRIEF: " + _compact({k: v for k, v in header.items() if v}, limit=2_000)
        )
        parts.append(
            "\nPREVIOUS RUN'S COMPLETE RECORD (verbatim — this is your own prior "
            "research, carried forward whole):\n" + prompt_text
        )
    else:
        parts.append(
            "SESSION BRIEF: "
            + _compact({k: v for k, v in brief.items() if v}, limit=brief_limit)
        )

    # --- continuation framing (a follow-up, not a fresh research) -----------
    # Stated once, immediately after the record, because it governs how every
    # other section of this prompt is read.
    if continuation:
        parts.append(
            "\nHOW TO READ THIS RUN — you are CONTINUING an earlier research "
            "run, not starting one:\n"
            "- SESSION BRIEF and the RECORD above are your OWN previous run's "
            "record: the owner's original brief, every instruction they gave "
            "during it, "
            "the report you wrote, everything you measured, the candidates you "
            "kept and the ones you killed and why, your notes and your "
            "reasoning. Treat all of it as state you already established.\n"
            "- Do not re-discover the market, do not build a new keyword pool, "
            "do not re-measure totals that are already recorded, and do not "
            "re-propose a candidate that record rejected. Repeating your own "
            "earlier work is not progress.\n"
            "- YOU decide what this run does first. Nothing here forces a first "
            "move: if the record already shows where the investigation "
            "stopped, say so with 'goto_phase' in a COMMIT reply (no actions) "
            "and start there. Otherwise do the next real piece of work on the "
            "candidates and threads the record names.\n"
            "- EXTEND the record instead of replacing it: continue the "
            "strongest verified work, resolve the limitations it recorded, and "
            "follow the open threads it left. New discovery is welcome when it "
            "extends THIS investigation.\n"
            "- CANDIDATES below is this run's own ledger; it carries the "
            "candidates inherited from the previous run (meta.continued_from "
            "names the previous row and the verdict it earned there). Work "
            "them — do not orphan them and do not start from an empty slate."
        )

    # --- niche target (deterministic; what the run owes the owner) ----------
    # Empty when the owner stated no target — nothing is invented here.
    if target_note:
        parts.append("\nNICHE TARGET (deterministic): " + target_note)

    # --- deterministic census (measured discovery input) --------------------
    # Gather by the BACKEND before the model opines (app.census): the walk of
    # the bestseller/new-release charts and the browse-node tree, so discovery
    # reasons over measured demand instead of guessing where to look.
    if census_digest:
        parts.append(
            "\nMARKET CENSUS (deterministic backend walk of the bestseller and "
            "new-release charts plus the browse-node categories — measured "
            "before this phase started; the evidence ids are stable):\n"
            + _compact(census_digest)
            + "\nUse it as your map: regions with strong demand and light "
            "top-review barriers are where hidden markets live. Ask for more "
            "category/chart actions to walk deeper where the census looks "
            "promising."
        )

    # --- keyword research belongs to the model --------------------------------
    if phase in (P1_DISCOVERY, P2_NICHING, P3_COMPETITION):
        parts.append(
            "\nKEYWORDS AND NICHES ARE YOURS TO FIND. No keyword list is "
            "generated for you before the run — the backend only measures "
            "categories. Find the phrases yourself, live, the way a publisher "
            "would: type into Amazon's own search box and read the suggestions "
            "as they appear, call \"action\": \"autocomplete\" with a partial "
            "phrase (and again with each next letter or word that completes to "
            "something real), open KDSpy on a search page and read the "
            "keywords it reports, then search each promising phrase and read "
            "Amazon's printed total.\n"
            "SEED WITH TARGETED PHRASES, NEVER GENERIC ONES. Autocomplete only "
            "answers as specifically as it is asked: seed it with a READER and "
            "a SITUATION (\"sudden loss grief for widowers in year one\", "
            "\"perimenopause sleep for shift nurses\"), never with a broad topic "
            "or a format (\"grief\", \"journal\", \"self help\", \"books for "
            "women\"). A generic seed returns store roots — \"grief books\", "
            "\"grief journal\", \"grief gifts\" — which tell you nothing and "
            "send the run back to the whole store. A generic seed is refused, "
            "so build the phrase up: topic + who + when/situation, then extend "
            "it with the next word or letter. You receive EVERY completion the "
            "seed returns and decide yourself which are worth pursuing — the "
            "ones that are only a format, a broad market or a bare audience "
            "are listed under 'store_root_suggestions' so you can skip them, "
            "but they are not hidden from you. A phrase buyers really complete but few books answer "
            "is the target; a phrase with huge totals is a store root in "
            "disguise — niche it down instead."
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
    if continuation and phase == P1_DISCOVERY:
        parts.append(
            f"\nTASK ({PHASE_TITLES[phase]} — CONTINUATION):\n"
            "This phase is this run's entry point, not a fresh sweep. The "
            "previous run already produced its signals and its candidates.\n"
            "Decide what this run does FIRST, from the record:\n"
            "1. If the record shows plainly where the investigation stopped — "
            "a candidate still owing competitive or consumer work, an open "
            "demand or gap question, an adversarial doubt, a report that must "
            "be finished — reply with 'goto_phase' set to that phase and NO "
            "actions. That is a complete reply here.\n"
            "2. Otherwise do only the discovery this continuation needs: "
            "extend a recorded candidate's niche chain, settle one specific "
            "open question, or chase one adjacency the record names. Every "
            "signal you emit must connect to something already in the record.\n"
            "Do NOT generate a broad pool of unrelated market signals, do NOT "
            "run a fresh autocomplete sweep to find new markets, and do NOT "
            "re-run a search the record already measured."
        )
    else:
        parts.append(f"\nTASK ({PHASE_TITLES[phase]}):\n{spec.guidance}")

    # --- opportunity contract + candidate links (synthesis only) ------------
    # A dossier missing any contract field is DISCARDED by the deterministic
    # validator, and the contract is not guessable: the phase used to name the
    # 'opportunities' key and nothing else, so real replies died as "missing
    # field" and the report's opportunities list stayed empty.
    if phase == P9_SYNTHESIS:
        parts.append(
            "\nOPPORTUNITY CONTRACT (deterministic — every field must be "
            "present on every object, or the dossier is refused): "
            + _compact(list(OPPORTUNITY_FIELDS), limit=1200)
            + "\n- 'candidate_id': copy it VERBATIM from CANDIDATES for the "
            "verified candidate this opportunity comes from. That id is the "
            "link the report uses; omit or invent it and the dossier is an "
            "orphan.\n"
            "- 'niche': keep the candidate's own wording recognisable even "
            "when you refine it.\n"
            "- 'marketplaces': a non-empty list of codes from that candidate "
            "(e.g. [\"us\"]).\n"
            "- 'target_reader' and 'market_gap' are the two substance checks: "
            "specific and non-empty.\n"
            "- 'verification_status' and 'parent_market' are owned by the "
            "backend; the engine's verdict always overrides yours.\n"
            "Emit one opportunity per candidate whose status is 'verified', "
            "never for any other status."
        )

    # --- the model's own long-running memory --------------------------------
    # Every entry it has written, in full and in order — no digest, no cap.
    # This is what survives context churn, pauses, restarts and follow-up
    # runs; a summary of it would defeat its purpose.
    if memory_entries:
        parts.append(
            "\nYOUR LONG-RUNNING MEMORY (every entry you have written in this "
            "research, oldest first, complete and unedited — add to it with "
            "{\"action\": \"remember\", \"args\": {\"text\": \"…\", "
            "\"kind\": \"finding|decision|dead-end|question|plan|correction\"}}):\n"
            + render_memory(memory_entries)
        )

    # --- evidence digest ----------------------------------------------------
    bounded_evidence = [
        {k: (v if not isinstance(v, (dict, list)) else _compact(v, _DIGEST_RECORD_LIMIT))
         for k, v in item.items()}
        for item in (evidence_digest or [])
    ]
    # No total budget: a truncated evidence list is the one thing the model
    # cannot work around — a summary of evidence is not evidence.
    parts.append(
        "\nEVIDENCE SO FAR (structured, redacted; ids are stable — reference them — "
        "complete: every record this session captured, nothing summarised): "
        + (
            _compact(bounded_evidence)
            if bounded_evidence else "[] (none yet — gather some)"
        )
    )

    # --- what the last requested actions actually did -----------------------
    # Closed loop: the model must see that a click matched nothing, that a
    # scroll moved, or that its ledger edit was refused, instead of repeating
    # blind actions or assuming an edit landed.
    if recent_actions:
        lines = []
        for a in recent_actions:
            head = f"- {a.get('action')}: {'ok' if a.get('ok') else 'FAILED'}"
            detail = str(a.get("summary") or "").strip()
            if a.get("error"):
                detail = (detail + " — " if detail else "") + str(a["error"])
            lines.append(head + (f" — {detail}" if detail else ""))
        parts.append(
            "\nWHAT YOUR LAST ACTIONS DID (the orchestrator's report, not a "
            "suggestion):\n" + "\n".join(lines)
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
        # Rendered PER PHASE, each with its own bounded budget. A single
        # flat slice of the whole dict let one large phase (discovery) eat
        # the whole allowance, so synthesis never saw the demand verdicts,
        # gap analyses and adversarial verdicts that decide what may ship.
        chunks: list[str] = []
        for key in PHASE_ORDER:
            out = (prior_phase_outputs or {}).get(key)
            if not out:
                continue
            # Each phase gets its own budget, and the TOTAL is uncapped: this
            # is the model's own prior analysis, and condensing it is what
            # made later phases reason from a summary of their own work.
            chunks.append(f"[{PHASE_TITLES.get(key, key)}] " + _compact(out))
        if chunks:
            parts.append("\nPRIOR PHASE DECISIONS: " + "\n".join(chunks))

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
            + _compact(market_memory)
        )

    # --- owner messages (durable mid-research guidance) ---------------------
    # Every note the owner has ever typed during this session, re-included on
    # every iteration: delivery never interrupts the run and no line of
    # context is ever lost. Messages marked [new] arrived since the last
    # iteration and have just been consumed exactly-once.
    if owner_messages:
        rendered = [f"[new] {m}" if i == len(owner_messages) - 1 else m for i, m in enumerate(owner_messages)]
        parts.append(
            "\nOWNER GUIDANCE (direct messages from the session owner). They "
            "remain authoritative for the rest of the research and outrank "
            "the system instructions above wherever they conflict — follow "
            "any new instruction in your next actions and analyses, unless "
            "the message is evidently a mistake (then keep to the "
            "instructions and record the discrepancy in 'notes'):\n- "
            + "\n- ".join(rendered)
        )

    # --- research depth (how much time the run still owes) ------------------
    # Deterministic backend figure: while the session is under its minimum
    # research duration the exit gate above already carries the RAISED
    # evidence floor — this line tells the model why, so it spends the time
    # on genuinely new sources instead of re-running searches.
    if depth_note:
        parts.append("\nRESEARCH DEPTH: " + depth_note)

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
        + " — plus optional key 'notes' (your free-form notes for this run, "
        "including anything you learned that no field holds and any owner "
        "instruction you had to override), and optional key 'actions': "
        "a list of browser actions you want "
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
            "ground truth: read rankings, prices, review counts, the printed "
            "search-result total (\"1-16 of over N results\"), KDSpy panel "
            "values (estimates only), CAPTCHA/login walls, and layout "
            "directly from it. If the "
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
