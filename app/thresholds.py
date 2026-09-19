"""Minimum evidence thresholds for the research integrity system.

Every threshold here is a deliberate quality gate, not an arbitrary number.
Each documents WHY it exists and is env-configurable so the owner can tune
it, but defaults are set high enough that research cannot "pass" on thin
data. The integrity layer uses these to decide whether a candidate may
advance, be downgraded, or must be rejected.

Design principle: the methodology must not manufacture passable-looking
research. If evidence is thin, the correct outcome is *rejection or
downgrade*, never a lowered bar. Thresholds may be raised via env; the
integrity layer never silently lowers them.

Chain of custody (enforced by app.claims):
    raw observation -> evidence -> claim -> interpretation -> verification
    -> conclusion. A final report may only present CONCLUSIONS that trace
    back through verification to evidence captured from real pages.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class EvidenceThresholds:
    """Configurable minimum-evidence thresholds.

    Rationales (why each number is what it is):

    min_competitor_sample (6):
        Judging a market from 1-2 books is how false positives are born.
        Six distinct competitor ASINs is the smallest sample that reveals
        whether a niche has real incumbents vs. a single anomaly, and lets
        price/rating/review patterns (not vibes) emerge.

    min_review_sample (8):
        A recurring complaint pattern cannot be established from 1-2
        reviews — individual taste dominates small samples. Eight reviews
        across different products is the minimum before "readers want X"
        is treated as a pattern rather than an anecdote.

    min_independent_sources (2):
        A claim resting on ONE page can be a layout quirk, a promoted item,
        or a transient. Two independent sources (different URLs/visits/
        marketplaces) force corroboration before a claim may support a
        final opportunity.

    min_observations (15):
        The session-level floor of raw page contact. Below this the agent
        has mostly reasoned, not researched. Cheap to meet in a real run;
        hard to fake.

    min_verification_coverage (1.0):
        EVERY synthesized opportunity must have been through the
        verification engine (fraction 0.0-1.0 of opportunities requiring a
        verification record). Default 1.0: no opportunity reaches the final
        report unverified.

    min_supported_claim_ratio (0.6):
        At least 60% of a candidate's important claims must be supported by
        evidence ids. A candidate held up mostly by model assumptions is
        exactly the "attractive but unsupported" output this stage exists
        to prevent.

    max_claim_inference_ratio (0.5):
        More than half of a candidate's claims being model inference means
        the "research" is mostly the model talking to itself; such
        candidates are downgraded regardless of how good they sound.
    """

    min_competitor_sample: int = 6
    min_review_sample: int = 8
    min_independent_sources: int = 2
    min_observations: int = 15
    min_verification_coverage: float = 1.0
    min_supported_claim_ratio: float = 0.6
    max_claim_inference_ratio: float = 0.5
    # Minimum distinct competitor ASINs with review-count evidence before a
    # saturation judgement is allowed (prevents "2 books = saturated").
    min_saturation_sample: int = 4
    # Reviews must come from at least this many distinct products before a
    # consumer-need pattern is treated as market-wide.
    min_review_products: int = 3

    @classmethod
    def from_env(cls) -> "EvidenceThresholds":
        return cls(
            min_competitor_sample=_int_env("THRESH_MIN_COMPETITORS", cls.min_competitor_sample),
            min_review_sample=_int_env("THRESH_MIN_REVIEWS", cls.min_review_sample),
            min_independent_sources=_int_env("THRESH_MIN_SOURCES", cls.min_independent_sources),
            min_observations=_int_env("THRESH_MIN_OBSERVATIONS", cls.min_observations),
            min_verification_coverage=float(
                os.environ.get("THRESH_MIN_VERIFICATION_COVERAGE", str(cls.min_verification_coverage))
            ),
            min_supported_claim_ratio=float(
                os.environ.get("THRESH_MIN_SUPPORTED_RATIO", str(cls.min_supported_claim_ratio))
            ),
            max_claim_inference_ratio=float(
                os.environ.get("THRESH_MAX_INFERENCE_RATIO", str(cls.max_claim_inference_ratio))
            ),
            min_saturation_sample=_int_env("THRESH_MIN_SATURATION_SAMPLE", cls.min_saturation_sample),
            min_review_products=_int_env("THRESH_MIN_REVIEW_PRODUCTS", cls.min_review_products),
        )

    def describe(self) -> dict:
        """Owner-facing description including the rationale for each number."""
        return {
            "min_competitor_sample": {
                "value": self.min_competitor_sample,
                "why": "Judging a market from 1-2 books produces false positives; six distinct competitor ASINs is the smallest sample that reveals real incumbents and lets price/rating/review patterns emerge.",
            },
            "min_review_sample": {
                "value": self.min_review_sample,
                "why": "A recurring complaint cannot be established from 1-2 reviews; eight across different products is the minimum before 'readers want X' is a pattern rather than an anecdote.",
            },
            "min_independent_sources": {
                "value": self.min_independent_sources,
                "why": "One page can be a layout quirk or a transient; claims need corroboration from two independent sources before supporting a final opportunity.",
            },
            "min_observations": {
                "value": self.min_observations,
                "why": "Session-level floor of raw page contact — below this the agent has mostly reasoned, not researched.",
            },
            "min_verification_coverage": {
                "value": self.min_verification_coverage,
                "why": "Fraction of opportunities that must have a verification record; 1.0 means no opportunity reaches the final report unverified.",
            },
            "min_supported_claim_ratio": {
                "value": self.min_supported_claim_ratio,
                "why": "Minimum share of a candidate's important claims that must cite evidence ids; prevents candidates held up mostly by model assumptions.",
            },
            "max_claim_inference_ratio": {
                "value": self.max_claim_inference_ratio,
                "why": "If more than half of a candidate's claims are model inference, the research is mostly self-talk; such candidates are downgraded.",
            },
            "min_saturation_sample": {
                "value": self.min_saturation_sample,
                "why": "Minimum distinct competitors with observable data before a saturation judgment is allowed; prevents '2 books = saturated'.",
            },
            "min_review_products": {
                "value": self.min_review_products,
                "why": "Reviews must span this many distinct products before a consumer-need pattern is treated as market-wide.",
            },
        }

    @classmethod
    def describe_defaults(cls) -> dict:
        return cls().describe()


# Singleton for the running process (env read once).
_THRESHOLDS: EvidenceThresholds | None = None


def get_thresholds() -> EvidenceThresholds:
    global _THRESHOLDS
    if _THRESHOLDS is None:
        _THRESHOLDS = EvidenceThresholds.from_env()
    return _THRESHOLDS


def reset_thresholds() -> None:
    """Test hook: force re-read of env on next access."""
    global _THRESHOLDS
    _THRESHOLDS = None
