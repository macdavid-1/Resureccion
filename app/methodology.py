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
        ),
        output_contract=(
            "signals",       # list of {topic, audience, hint, marketplace, source}
            "observations",  # what was actually seen on pages
        ),
        guidance=(
            "Generate a BROAD pool of market signals. Draw on: Amazon search "
            "results, categories, subcategories, autocomplete suggestions, "
            "bestseller and new-release pages, competitor books, related "
            "products, review patterns, KDSpy data, marketplace differences, "
            "observed consumer language, recurring reader problems, "
            "underserved audiences, and emerging topics. Be creative. Do NOT "
            "prematurely filter — weak-looking signals may hide gems. Every "
            "signal must name a concrete topic and audience."
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
        guidance=(
            "For every serious candidate, identify the relevant competitors "
            "and inspect MULTIPLE competing books (never judge from one). "
            "Record: titles, subtitles, covers, descriptions, positioning, "
            "pricing, rankings where observable, publication dates, review "
            "counts, review patterns, recurring complaints, recurring praise, "
            "apparent weaknesses, and saturation signals. Note whether the "
            "top sellers are saturated rehashes or strong incumbents."
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
            "record_observation", "wait",
        ),
        output_contract=(
            "consumer_maps",  # list of {niche, praises[], complaints[], unmet[], design_opportunities[]}
        ),
        guidance=(
            "Treat reviews as PRIMARY consumer evidence. Collect "
            "representative positive AND negative patterns. Identify what "
            "readers actually wanted, what disappointed them, what they "
            "praised, missing content, confusing explanations, poor "
            "organization, insufficient depth, outdated information, audience "
            "mismatch, and unfulfilled promises. Do NOT merely summarize: "
            "convert each recurring complaint into a concrete book-design "
            "opportunity."
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
        ),
        output_contract=(
            "demand_assessments",  # list of {niche, verdict, signals[], risks[]}
        ),
        guidance=(
            "Determine whether evidence suggests GENUINE buyer demand. "
            "Distinguish actual demand from curiosity, casual browsing, "
            "temporary trends, one-book anomalies, and raw search activity. "
            "Use ranking stability, review velocity, breadth of competing "
            "titles, and KDSpy data where available. NEVER declare demand "
            "merely because a keyword exists. Downgrade or reject candidates "
            "whose demand rests on a single anomaly."
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
            "capture_evidence", "record_observation", "wait",
        ),
        output_contract=(
            "gap_analyses",  # list of {niche, exists, sells, readers_want, competitors_fail, better, why_choose}
        ),
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
        guidance=(
            "Where useful, spot-check the strongest candidates on additional "
            "marketplaces to determine whether the signal is local, "
            "language-specific, marketplace-specific, broadly recurring, or "
            "emerging elsewhere. Do NOT mechanically visit every marketplace "
            "and do NOT require identical performance across markets — "
            "absence in one market is data, not disqualification."
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
        guidance=(
            "ONLY now synthesize final opportunities from verified candidates. "
            "Target 5-10 opportunities for an autonomous session — but NEVER "
            "manufacture opportunities to hit a quota; quality precedes "
            "quantity. Each opportunity must already have survived "
            "adversarial verification: the owner must not need to redo basic "
            "validation."
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
        }
