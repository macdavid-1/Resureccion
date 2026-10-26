"""The Resurrección KDP research methodology — deterministic, explicit, enforced.

This module is the LAW of the research process. The AI model provides
intelligence *inside* each phase; it never invents its own methodology. The
orchestrator (`app.research_runner`) executes the phases defined here in
order, enforces exit gates before advancing, tracks timing, and rejects
model output that violates phase contracts.

The 9 phases (per the owner's specification):

1.  opportunity_discovery   — broad pool of market signals, no premature filtering
2.  aggressive_niching      — narrow every promising market until further
                              narrowing would destroy demand
3.  competitive_landscape   — many competitors per candidate, not one
4.  consumer_intelligence   — reviews as primary evidence → design opportunities
5.  demand_validation       — distinguish real demand from curiosity/noise
6.  opportunity_gap         — concrete reason for the book to exist
7.  cross_market_validation — spot-check signal across marketplaces (where useful)
8.  adversarial_verification— deliberately try to KILL the strongest candidates
9.  opportunity_synthesis   — only now produce final opportunities (5–10 target,
                              never manufactured)

Depth priority (enforced in prompts and budget policy):
    research integrity > evidence quality > depth > opportunity quality >
    breadth > efficiency > speed
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Phases
# --------------------------------------------------------------------------- #

P1_DISCOVERY = "opportunity_discovery"
P2_NICHING = "aggressive_niching"
P3_COMPETITION = "competitive_landscape"
P4_CONSUMER = "consumer_intelligence"
P5_DEMAND = "demand_validation"
P6_GAP = "opportunity_gap"
P7_CROSS_MARKET = "cross_market_validation"
P8_ADVERSARIAL = "adversarial_verification"
P9_SYNTHESIS = "opportunity_synthesis"

PHASE_ORDER: tuple[str, ...] = (
    P1_DISCOVERY,
    P2_NICHING,
    P3_COMPETITION,
    P4_CONSUMER,
    P5_DEMAND,
    P6_GAP,
    P7_CROSS_MARKET,
    P8_ADVERSARIAL,
    P9_SYNTHESIS,
)

PHASE_TITLES: dict[str, str] = {
    P1_DISCOVERY: "Phase 1 — Opportunity Discovery",
    P2_NICHING: "Phase 2 — Aggressive Niching",
    P3_COMPETITION: "Phase 3 — Competitive Landscape",
    P4_CONSUMER: "Phase 4 — Consumer Intelligence",
    P5_DEMAND: "Phase 5 — Demand Validation",
    P6_GAP: "Phase 6 — Opportunity-Gap Analysis",
    P7_CROSS_MARKET: "Phase 7 — Cross-Market Validation",
    P8_ADVERSARIAL: "Phase 8 — Adversarial Verification",
    P9_SYNTHESIS: "Phase 9 — Opportunity Synthesis",
}

# Map methodology phases onto the session-level PHASES for the dashboard.
SESSION_PHASE_BY_METHOD_PHASE: dict[str, str] = {
    P1_DISCOVERY: "discovery",
    P2_NICHING: "exploration",
    P3_COMPETITION: "exploration",
    P4_CONSUMER: "exploration",
    P5_DEMAND: "verification",
    P6_GAP: "verification",
    P7_CROSS_MARKET: "verification",
    P8_ADVERSARIAL: "verification",
    P9_SYNTHESIS: "synthesis",
}


# --------------------------------------------------------------------------- #
# Iteration policy (deterministic budgets per phase)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PhaseSpec:
    """Deterministic contract for one methodology phase."""

    key: str
    # Session agent-states this phase may legitimately use.
    agent_states: tuple[str, ...]
    # Hard cap on model iterations for this phase (deterministic budget).
    max_iterations: int
    # Minimum evidence pieces the phase should have gathered before exit
    # (0 = no minimum; quality gates still apply).
    min_evidence: int
    # Which candidate statuses this phase reads/writes.
    consumes: tuple[str, ...]
    produces: tuple[str, ...]
    # The actions the model may request during this phase (whitelist).
    allowed_actions: tuple[str, ...]
    # What the phase's final answer must contain (JSON contract).
    output_contract: tuple[str, ...]
    guidance: str
    # Minimum items the phase's PRIMARY output contract field (the first
    # entry of output_contract) must hold before the phase may COMMIT.
    # 0 = the phase may legitimately conclude with an empty list (the
    # deterministic pipeline downstream decides instead of the model).
    # The runner additionally gates the floor on the phase's upstream input
    # (see ResearchRunner._output_floor): an analysis phase with no
    # candidates yet is not forced to invent any.
    min_output_items: int = 0


@dataclass(frozen=True)
class ModeBudgetProfile:
    """Deterministic, env-tunable budget profile for one research mode.

    The three starting modes do not need the same shape of research:
    - 'prompt'   (detailed brief): scope is already known, so discovery can be
                 short and the budget moves into competition, consumer
                 intelligence and adversarial verification.
    - 'keywords' (rough concepts): balanced.
    - 'auto'     (NO topic given — the owner pressed Start Research): the run
                 must find markets the owner did not know existed, so Phase 1
                 and Phase 2 get the largest share and early competitive work
                 is lighter.

    Iteration caps override the per-phase spec while RUNNING (the spec value
    stays the contract shown by /api/methodology); `extra_calls` raises the
    per-session model-call ceiling above RunnerConfig's base, scaled by the
    hard time budget available (see ResearchRunner._model_call_ceiling).
    """

    mode: str
    extra_calls: int
    iterations: dict[str, int]

    def iterations_for(self, phase: str, default: int) -> int:
        return max(1, int(self.iterations.get(phase, default)))


# Iteration caps per mode, keyed by phase (PHASE_ORDER order). Values are a
# deliberate rebalancing of the SAME total budget — no phase gets starved,
# the shape of the research changes with what the owner actually supplied.
MODE_PROFILES: dict[str, ModeBudgetProfile] = {
    "prompt": ModeBudgetProfile(
        mode="prompt",
        extra_calls=0,
        iterations={
            P1_DISCOVERY: 8,          # the brief already scopes discovery
            P2_NICHING: 10,
            P3_COMPETITION: 16,       # …so depth moves downstream
            P4_CONSUMER: 16,
            P5_DEMAND: 10,
            P6_GAP: 8,
            P7_CROSS_MARKET: 8,
            P8_ADVERSARIAL: 14,
            P9_SYNTHESIS: 8,
        },
    ),
    "keywords": ModeBudgetProfile(
        mode="keywords",
        extra_calls=20,
        iterations={
            P1_DISCOVERY: 14,
            P2_NICHING: 13,
            P3_COMPETITION: 14,
            P4_CONSUMER: 14,
            P5_DEMAND: 10,
            P6_GAP: 8,
            P7_CROSS_MARKET: 8,
            P8_ADVERSARIAL: 12,
            P9_SYNTHESIS: 8,
        },
    ),
    "auto": ModeBudgetProfile(
        mode="auto",
        extra_calls=40,
        iterations={
            P1_DISCOVERY: 16,         # no topic: discovery must search wider
            P2_NICHING: 14,           # …and niching is where candidates born
            P3_COMPETITION: 12,       # lighter early competitive work
            P4_CONSUMER: 12,
            P5_DEMAND: 10,
            P6_GAP: 8,
            P7_CROSS_MARKET: 8,
            P8_ADVERSARIAL: 12,
            P9_SYNTHESIS: 8,
        },
    ),
}


def mode_profile(mode: str) -> ModeBudgetProfile:
    """Budget profile for a session mode (unknown mode → the balanced one).

    Env overrides (read per call, deterministic):
    - MODE_ITERATIONS_<mode>=16:14:12:12:10:8:8:12:8 — nine integers in
      PHASE_ORDER order.
    An unparsable value is ignored (the profile default stands): a typo must
    never stop research.
    """
    import os

    profile = MODE_PROFILES.get(mode) or MODE_PROFILES["keywords"]
    raw = (os.environ.get(f"MODE_ITERATIONS_{profile.mode}") or "").strip()
    if not raw:
        return profile
    try:
        values = [int(v) for v in raw.replace(",", ":").split(":")]
    except ValueError:
        return profile
    if len(values) != len(PHASE_ORDER) or any(v <= 0 for v in values):
        return profile
    return ModeBudgetProfile(
        mode=profile.mode,
        extra_calls=profile.extra_calls,
        iterations=dict(zip(PHASE_ORDER, values)),
    )


# External (non-Amazon) research actions. Deliberately NOT a new phase: they
# land in the existing evidence table with stable ids and flow through the
# normal evidence -> claims -> filters -> gates pipeline untouched. The
# runner gates WHERE they may fire (see ResearchRunner._external_action_ok).
EXTERNAL_ACTIONS: tuple[str, ...] = (
    "forum_search",      # community complaint / unmet-need language
    "google_suggest",    # un-Amazon-anchored demand phrasing
    "goodreads_check",   # independent want-to-read / rating evidence
)

# Which phases may request them (the model-facing whitelist). P1's use is
# additionally gated at runtime to autonomous (auto) runs AFTER Amazon-side
# anchors exist — unanchored forum noise may never set a run's direction.
PHASES_ALLOWING_EXTERNAL: tuple[str, ...] = (
    P1_DISCOVERY, P4_CONSUMER, P5_DEMAND, P6_GAP,
)


SPECS: dict[str, PhaseSpec] = {
    P1_DISCOVERY: PhaseSpec(
        key=P1_DISCOVERY,
        agent_states=("planning", "exploring", "observing"),
        max_iterations=12,
        min_evidence=8,
        consumes=(),
        produces=("signal",),
        allowed_actions=(
            "search_marketplace", "open_category", "autocomplete",
            "bestsellers", "new_releases", "related_products",
            "kdspy_data", "capture_evidence", "record_observation", "wait",
            *EXTERNAL_ACTIONS,
        ),
        output_contract=(
            "signals",       # list of {topic, audience, hint, marketplace, source}
            "observations",  # what was actually seen on pages
        ),
        min_output_items=1,  # discovery with no signal is not discovery
        guidance=(
            "Generate a BROAD pool of market signals. Draw on: Amazon search "
            "results, categories, subcategories, autocomplete suggestions, "
            "bestseller and new-release pages, the deterministic category "
            "CENSUS already gathered for this run, the autocomplete FRONTIER "
            "demand-language terms, competitor books, related products, "
            "review patterns, KDSpy data, marketplace differences, observed "
            "consumer language, recurring reader problems, underserved "
            "audiences, and emerging topics. Prefer parts of the census and "
            "frontier with strong demand signals and light competition — the "
            "whole point is finding markets nobody is serving. Be creative. "
            "Do NOT prematurely filter — weak-looking signals may hide gems. "
            "Every signal must name a concrete topic and audience. When no "
            "topic was given, let the measured census (not familiarity) "
            "decide where to look, and expand outward from surprising "
            "high-headroom regions. External sources (forums, Google "
            "suggestions, Goodreads) are a SECOND wave only: use them once "
            "Amazon-side anchors exist, never as the opening move."
        ),
    ),
    P2_NICHING: PhaseSpec(
        key=P2_NICHING,
        agent_states=("analyzing", "exploring"),
        max_iterations=10,
        min_evidence=6,
        consumes=("signal", "discovered"),
        produces=("discovered",),
        allowed_actions=(
            "search_marketplace", "autocomplete", "open_category",
            "related_products", "capture_evidence", "record_observation", "wait",
        ),
        output_contract=(
            "niches",  # list of {niche, parent_market, chain, evidence_ids, demand_hints}
        ),
        # THE candidate-starvation guard: niching is the ONE place candidates
        # are born, so it may never commit an empty list while Phase 1 handed
        # it signals. An empty commit here means the whole pass ends with
        # zero candidates no matter how much evidence was gathered.
        min_output_items=1,
        guidance=(
            "Narrow EVERY promising broad market aggressively along the chain: "
            "broad market → category → subcategory → audience → specific "
            "problem/desire → specific use case → specific reader segment → "
            "book opportunity. Keep niching while demand evidence persists; "
            "stop narrowing when further narrowing would materially reduce "
            "commercial viability. BAN generic markets like 'productivity' — "
            "a niche must name WHO has WHAT problem in WHICH context. Verify "
            "search demand exists for the narrow form before keeping it."
        ),
    ),
    P3_COMPETITION: PhaseSpec(
        key=P3_COMPETITION,
        agent_states=("observing", "analyzing"),
        max_iterations=14,
        min_evidence=10,
        consumes=("discovered",),
        produces=("discovered", "rejected"),
        allowed_actions=(
            "search_marketplace", "open_product", "related_products",
            "capture_evidence", "record_observation", "wait",
        ),
        output_contract=(
            "competitive_maps",  # list of {niche, competitors[], saturation, pricing, patterns}
        ),
        min_output_items=1,
        guidance=(
            "For every serious candidate, identify the relevant competitors "
            "and inspect MULTIPLE competing books (never judge from one). "
            "Record: titles, subtitles, covers, descriptions, positioning, "
            "pricing, rankings where observable, publication dates, review "
            "counts, review patterns, recurring complaints, recurring praise, "
            "apparent weaknesses, and saturation signals. Note whether the "
            "top sellers are saturated rehashes or strong incumbents. Open "
            "each incumbent's product page (open_product) so its category "
            "path and best-seller rank are captured — the report derives "
            "recommended KDP categories and entry barriers from exactly "
            "these pages."
        ),
    ),
    P4_CONSUMER: PhaseSpec(
        key=P4_CONSUMER,
        agent_states=("observing", "analyzing"),
        max_iterations=14,
        min_evidence=10,
        consumes=("discovered",),
        produces=("discovered", "rejected"),
        allowed_actions=(
            "open_product", "read_reviews", "capture_evidence",
            "record_observation", "wait", "forum_search", "goodreads_check",
        ),
        output_contract=(
            "consumer_maps",  # list of {niche, praises[], complaints[], unmet[], design_opportunities[]}
        ),
        min_output_items=1,
        guidance=(
            "Treat reviews as PRIMARY consumer evidence. Collect "
            "representative positive AND negative patterns. Identify what "
            "readers actually wanted, what disappointed them, what they "
            "praised, missing content, confusing explanations, poor "
            "organization, insufficient depth, outdated information, audience "
            "mismatch, and unfulfilled promises. Where Amazon reviews are "
            "thin or absent (often true in the hidden niches), community "
            "threads and Goodreads carry the same signal — request "
            "forum_search / goodreads_check for the candidate's audience and "
            "language; whether a source is worth mining is YOUR judgment, not "
            "the backend's. Do NOT merely summarize: convert each recurring "
            "complaint into a concrete book-design opportunity."
        ),
    ),
    P5_DEMAND: PhaseSpec(
        key=P5_DEMAND,
        agent_states=("analyzing",),
        max_iterations=8,
        min_evidence=6,
        consumes=("discovered",),
        produces=("discovered", "rejected"),
        allowed_actions=(
            "search_marketplace", "autocomplete", "kdspy_data",
            "open_product", "capture_evidence", "record_observation", "wait",
            *EXTERNAL_ACTIONS,
        ),
        output_contract=(
            "demand_assessments",  # list of {niche, verdict, signals[], risks[]}
        ),
        min_output_items=1,
        guidance=(
            "Determine whether evidence suggests GENUINE buyer demand. "
            "Distinguish actual demand from curiosity, casual browsing, "
            "temporary trends, one-book anomalies, and raw search activity. "
            "Use ranking stability, review velocity, breadth of competing "
            "titles, and KDSpy data where available. NEVER declare demand "
            "merely because a keyword exists. Downgrade or reject candidates "
            "whose demand rests on a single anomaly. Validate KEYWORDS "
            "concretely: for every serious candidate run search_marketplace "
            "with the EXACT keyword phrases you intend to recommend "
            "(prioritize the strongest candidates; one action per phrase) — "
            "the report ranks keywords by the result counts and top-10 "
            "review barriers captured from these searches, and marks "
            "unsearched keywords as unmeasured."
        ),
    ),
    P6_GAP: PhaseSpec(
        key=P6_GAP,
        agent_states=("analyzing",),
        max_iterations=8,
        min_evidence=4,
        consumes=("discovered",),
        produces=("discovered", "rejected"),
        allowed_actions=(
            "search_marketplace", "open_product", "read_reviews",
            "capture_evidence", "record_observation", "wait", "forum_search",
        ),
        output_contract=(
            "gap_analyses",  # list of {niche, exists, sells, readers_want, competitors_fail, better, why_choose}
        ),
        min_output_items=1,
        guidance=(
            "For every serious candidate answer precisely: What exists? What "
            "sells? What do readers want? What do competitors fail to "
            "provide? What could be materially better? Why would a reader "
            "choose the NEW book over every incumbent? A candidate without a "
            "concrete, evidence-backed reason to exist must be rejected."
        ),
    ),
    P7_CROSS_MARKET: PhaseSpec(
        key=P7_CROSS_MARKET,
        agent_states=("exploring", "observing"),
        max_iterations=10,
        min_evidence=4,
        consumes=("discovered",),
        produces=("discovered",),
        allowed_actions=(
            "search_marketplace", "open_product", "autocomplete",
            "capture_evidence", "record_observation", "wait",
        ),
        output_contract=(
            "cross_market_checks",  # list of {niche, marketplace, verdict, notes}
        ),
        min_output_items=1,
        guidance=(
            "Where useful, spot-check the strongest candidates on additional "
            "marketplaces to determine whether the signal is local, "
            "language-specific, marketplace-specific, broadly recurring, or "
            "emerging elsewhere. Do NOT mechanically visit every marketplace "
            "and do NOT require identical performance across markets — "
            "absence in one market is data, not disqualification. While "
            "there, spot-check the strongest keywords of leading candidates "
            "on the additional marketplaces so cross-market keyword demand "
            "is measured, not assumed."
        ),
    ),
    P8_ADVERSARIAL: PhaseSpec(
        key=P8_ADVERSARIAL,
        agent_states=("analyzing", "verifying"),
        max_iterations=12,
        min_evidence=8,
        consumes=("discovered",),
        produces=("verified", "rejected"),
        allowed_actions=(
            "search_marketplace", "open_product", "read_reviews",
            "kdspy_data", "capture_evidence", "record_observation", "wait",
        ),
        output_contract=(
            "verdicts",  # list of {niche, verdict: pass|downgrade|reject, contradictions[], reasons}
        ),
        # 0 on purpose: the deterministic integrity pipeline that runs right
        # after this phase is the authoritative verdict, whatever the model
        # asserts (or omits) here.
        min_output_items=0,
        guidance=(
            "Deliberately try to KILL your own strongest candidates. For each: "
            "What evidence contradicts it? Is demand actually weak? Is "
            "competition stronger than believed? Are rankings misleading? "
            "Are the top competitors unusual outliers? Is the niche too "
            "narrow or already saturated? Is the apparent gap commercially "
            "irrelevant? Could demand vanish quickly? Is this a risky KDP "
            "category (content-policy, quality-claim, or account-health "
            "risks)? A candidate that fails must be REJECTED or DOWNGRADED — "
            "never protected."
        ),
    ),
    P9_SYNTHESIS: PhaseSpec(
        key=P9_SYNTHESIS,
        agent_states=("synthesizing", "reporting"),
        max_iterations=8,
        min_evidence=0,
        consumes=("verified",),
        produces=("verified",),
        allowed_actions=("record_observation", "wait"),
        output_contract=(
            "opportunities",  # full opportunity objects (see OPPORTUNITY_FIELDS)
        ),
        # 0 on purpose: a session whose candidates all died must be able to
        # conclude honestly with no opportunity rather than manufacture one.
        min_output_items=0,
        guidance=(
            "ONLY now synthesize final opportunities from verified candidates. "
            "Target 5-10 opportunities for an autonomous session — but NEVER "
            "manufacture opportunities to hit a quota; quality precedes "
            "quantity. Each opportunity must already have survived "
            "adversarial verification: the owner must not need to redo basic "
            "validation. Order recommended keywords by the measured search "
            "evidence already captured (fewer results and lighter top-10 "
            "review barriers first); never present an unmeasured keyword as "
            "validated."
        ),
    ),
}


# --------------------------------------------------------------------------- #
# Opportunity contract
# --------------------------------------------------------------------------- #

OPPORTUNITY_FIELDS: tuple[str, ...] = (
    "niche",                  # precise niche
    "parent_market",          # parent market
    "target_reader",          # target reader
    "reader_problem",         # reader problem/desire
    "marketplaces",           # marketplaces (codes)
    "evidence_ids",           # supporting evidence
    "competitive_landscape",  # competitor landscape summary
    "consumer_needs",         # consumer needs
    "market_gap",             # market gap
    "differentiation",        # differentiation opportunity
    "risks",                  # risks
    "keywords",               # relevant keywords
    "title_concepts",         # title concepts
    "positioning",            # possible positioning
    "confidence",             # 0.0-1.0
    "verification_status",    # adversarial verdict that produced it
)


def validate_opportunity(obj: dict) -> tuple[bool, list[str]]:
    """Structural validation of a synthesized opportunity. Deterministic."""
    problems: list[str] = []
    for f in OPPORTUNITY_FIELDS:
        if f not in obj:
            problems.append(f"missing field: {f}")
    if problems:
        return False, problems
    if not str(obj.get("niche", "")).strip():
        problems.append("niche must be non-empty")
    if not str(obj.get("target_reader", "")).strip():
        problems.append("target_reader must name a specific reader")
    if not str(obj.get("market_gap", "")).strip():
        problems.append("market_gap must be concrete")
    conf = obj.get("confidence")
    if not isinstance(conf, (int, float)) or not (0.0 <= float(conf) <= 1.0):
        problems.append("confidence must be between 0.0 and 1.0")
    mkt = obj.get("marketplaces")
    if not isinstance(mkt, list) or not mkt:
        problems.append("marketplaces must be a non-empty list")
    return len(problems) == 0, problems


# --------------------------------------------------------------------------- #
# Timing / depth policy
# --------------------------------------------------------------------------- #

def depth_priority_text() -> str:
    return (
        "Priority order (never invert): 1. research integrity, "
        "2. evidence quality, 3. depth, 4. opportunity quality, "
        "5. breadth, 6. efficiency, 7. speed. A six-hour session is "
        "acceptable if the research genuinely requires it. Never stop early "
        "just because some data was collected; stop when the phase's exit "
        "gate is genuinely met."
    )


@dataclass
class PhaseTiming:
    """Deterministic timing exposure for the model."""

    phase: str
    started_at: str
    elapsed_seconds: float = 0.0
    session_elapsed_seconds: float = 0.0
    iterations: int = 0
    attempts_since_progress: int = 0

    def digest(self) -> dict:
        return {
            "phase": self.phase,
            "phase_elapsed_seconds": round(self.elapsed_seconds, 1),
            "session_elapsed_seconds": round(self.session_elapsed_seconds, 1),
            "iterations_this_phase": self.iterations,
            "iterations_without_progress": self.attempts_since_progress,
        }


# Stall policy: after this many model iterations without new evidence,
# candidates, or state change, the runner forces a pivot.
STALL_LIMIT = 3


# --------------------------------------------------------------------------- #
# Strategic focus pivot (autonomous re-discovery)
# --------------------------------------------------------------------------- #

def focus_pivot_directive(
    *,
    round_no: int,
    dead_niches: list[str],
    unresolved_niches: list[str],
    has_brief: bool,
) -> str:
    """Deterministic framing injected into every prompt of the next pass.

    Triggered when a full pass (deep research, aggressive niching,
    adversarial verification, integrity pipeline) leaves ZERO verified
    candidates: even niche-down could not rescue the focus. The directive
    restarts discovery on a new focus while keeping the methodology, the
    owner's brief, and every integrity verdict final. Deterministic text —
    the model reasons INSIDE it, never around it.
    """
    dead = "; ".join(n[:80] for n in dead_niches[:6]) or "the earlier candidates"
    lines = [
        (
            f"Research round {round_no} concluded with ZERO verified "
            "opportunities: deep research, aggressive niching, adversarial "
            "verification and the deterministic integrity pipeline killed "
            f"every candidate from that focus ({dead}). Niche-down was already "
            "attempted and did not rescue it."
        ),
        (
            "The earlier phase decisions of this session belong to the "
            "abandoned focus; their rejections are final. Do NOT re-propose "
            "those niches or their niching chains — this pass starts from a "
            "genuinely different place."
        ),
    ]
    if unresolved_niches:
        lines.append(
            "Still-unresolved candidates from the dead focus (status is in "
            "CANDIDATES) may be re-niched or dropped, but never carry the "
            "pass on their own: " + "; ".join(n[:80] for n in unresolved_niches[:4])
        )
    if has_brief:
        lines.append(
            "SCOPE: the owner's brief remains authoritative — pivot INSIDE "
            "it: explore different parent markets, audiences and needs the "
            "brief permits; never substitute an unrelated market."
        )
    else:
        lines.append(
            "SCOPE: no topic was specified — explore genuinely different "
            "parent markets and audiences than the abandoned focus."
        )
    lines.append(
        "How to pivot: restart from broad market signals with different "
        "parent markets and reader groups, and gather FRESH evidence for "
        "every new claim even where similar evidence exists from the dead "
        "focus. If the new focus also fails adversarial verification, say so "
        "plainly in the report — never manufacture an opportunity."
    )
    return "\n".join(lines)


class Methodology:
    """Runtime facade over the deterministic methodology.

    The runner uses SPECS directly; this class exists so the API and future
    consumers can describe/enforce the methodology without reaching into
    module internals, and so the methodology stays a single source of truth.
    """

    def __init__(self, config: Any = None) -> None:
        self.config = config

    @property
    def phases(self) -> tuple[str, ...]:
        return PHASE_ORDER

    def spec(self, phase: str) -> PhaseSpec:
        try:
            return SPECS[phase]
        except KeyError:
            raise KeyError(f"unknown methodology phase {phase!r}") from None

    def session_phase(self, method_phase: str) -> str:
        """Map a methodology phase onto the session-level PHASES value."""
        return SESSION_PHASE_BY_METHOD_PHASE.get(method_phase, "exploration")

    def describe(self) -> dict[str, Any]:
        """Full owner-facing description (used by /api/methodology)."""
        return {
            "priority_order": [
                "research integrity", "evidence quality", "depth",
                "opportunity quality", "breadth", "efficiency", "speed",
            ],
            "depth_priority_law": depth_priority_text(),
            "stall_limit": STALL_LIMIT,
            "phases": [
                {
                    "key": p.key,
                    "title": PHASE_TITLES.get(p.key, p.key),
                    "order": i + 1,
                    "max_iterations": p.max_iterations,
                    "min_evidence": p.min_evidence,
                    "min_output_items": p.min_output_items,
                    "allowed_actions": list(p.allowed_actions),
                    "output_contract": list(p.output_contract),
                    "consumes": list(p.consumes),
                    "produces": list(p.produces),
                    "agent_states": list(p.agent_states),
                    "guidance": p.guidance,
                }
                for i, p in enumerate(PHASE_ORDER)
                for p in (SPECS[p],)
            ],
            "opportunity_contract": list(OPPORTUNITY_FIELDS),
            "external_actions": list(EXTERNAL_ACTIONS),
            "phases_allowing_external": list(PHASES_ALLOWING_EXTERNAL),
            "mode_budget_profiles": {
                mode: {
                    "extra_model_calls": p.extra_calls,
                    "phase_iterations": dict(p.iterations),
                }
                for mode, p in MODE_PROFILES.items()
            },
        }
