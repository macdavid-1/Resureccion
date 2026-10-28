"""The 7 research quality gates.

Before a session may deliver a final report, the WHOLE session is audited
against seven deterministic quality gates. Each gate measures real session
data (counts, ratios, distinct sources), records its outcome as a durable
`quality_gate` integrity assessment, and explains WHY the threshold is what
it is. A failed blocking gate marks the final report `provisional` — the
owner sees the gate failures in the report itself, never a silently
under-supported "final" deliverable.

Gates:
    1. observation_gate          raw page contact >= min_observations
    2. evidence_gate             enough evidence from enough distinct sources
    3. candidate_gate            a real candidate pipeline existed and produced
                                 at least one verified candidate
    4. verification_coverage     every opportunity has a verification record
    5. supported_claims_gate     session-wide claim support ratio >= threshold
    6. inference_load_gate       session-wide model-inference ratio <= threshold
    7. opportunity_diversity     opportunities are distinct, not duplicates
    8. opportunity_gap_gate      every opportunity's market gap traces to a
                                 competition/market_gap claim backed by evidence
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.claims import (
    CLAIM_UNSUPPORTED,
    ClaimStore,
    EQ_INFERENCE,
    claim_is_estimate_backed,
    estimate_evidence_ids,
    is_estimate_kind,
)
from app.integrity import KIND_QUALITY_GATE, IntegrityAssessmentStore
from app.research_data import (
    CAND_REJECTED,
    CAND_VERIFIED,
    CandidateStore,
    OpportunityStore,
    VerificationStore,
)
from app.thresholds import EvidenceThresholds


@dataclass
class GateResult:
    name: str
    passed: bool
    blocking: bool          # failing this gate marks the report provisional
    reason: str
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "blocking": self.blocking,
            "reason": self.reason,
            "metrics": self.metrics,
        }


@dataclass
class GateReport:
    results: list[GateResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.results if g.blocking)

    @property
    def failed_blocking(self) -> list[str]:
        return [g.name for g in self.results if g.blocking and not g.passed]

    @property
    def warnings(self) -> list[str]:
        return [g.reason for g in self.results if not g.passed and not g.blocking]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed_blocking": self.failed_blocking,
            "warnings": self.warnings,
            "gates": [g.to_dict() for g in self.results],
        }


class QualityGateRunner:
    """Runs the 7 quality gates over one session's durable data."""

    def __init__(
        self,
        thresholds: EvidenceThresholds,
        claims: ClaimStore,
        candidates: CandidateStore,
        verifications: VerificationStore,
        opportunities: OpportunityStore,
        assessments: IntegrityAssessmentStore,
        *,
        browser_evidence_count: int = 0,
        observation_count: int = 0,
        evidence_stores: tuple[Any, ...] = (),
    ) -> None:
        self.t = thresholds
        self.claims = claims
        self.candidates = candidates
        self.verifications = verifications
        self.opportunities = opportunities
        self.assessments = assessments
        self.browser_evidence_count = browser_evidence_count
        self.observation_count = observation_count
        # Optional (browser_evidence, evidence) stores: when wired, the
        # evidence gate counts only citations that REALLY exist in this
        # session — an invented id must never satisfy it.
        self.evidence_stores = tuple(s for s in evidence_stores if s is not None)
        self._known_ids_cache: set[str] | None | bool = False
        self._estimate_cache: set[str] | bool = False

    def _known_ids(self, session_id: str) -> set[str] | None:
        """Every evidence id that exists in this session, or None when the
        stores are not wired (legacy callers keep the old behavior)."""
        if self._known_ids_cache is not False:
            return self._known_ids_cache  # type: ignore[return-value]
        if not self.evidence_stores:
            self._known_ids_cache = None
            return None
        known: set[str] = set()
        for store_ in self.evidence_stores:
            try:
                known |= {str(e.id) for e in store_.list(session_id, limit=5000)}
            except Exception:
                pass
        self._known_ids_cache = known
        return known

    def _estimate_ids(self, session_id: str) -> set[str]:
        """Evidence ids that are VENDOR ESTIMATES (KDSpy panel figures).

        Empty when the stores are not wired — the gate then behaves exactly
        as before rather than guessing.
        """
        if not self.evidence_stores:
            return set()
        if self._estimate_cache is not False:
            return self._estimate_cache  # type: ignore[return-value]
        rows: list[Any] = []
        for store_ in self.evidence_stores:
            try:
                rows.extend(list(store_.list(session_id, limit=5000)))
            except Exception:
                pass
        ids = estimate_evidence_ids(rows)
        self._estimate_cache = ids
        return ids

    # ------------------------------------------------------------------ run
    def run(self, session_id: str) -> GateReport:
        gates = [
            self._observation_gate(session_id),
            self._evidence_gate(session_id),
            self._candidate_gate(session_id),
            self._verification_coverage_gate(session_id),
            self._supported_claims_gate(session_id),
            self._inference_load_gate(session_id),
            self._opportunity_diversity_gate(session_id),
            self._opportunity_gap_gate(session_id),
        ]
        report = GateReport(results=gates)
        for g in gates:
            self.assessments.record(
                session_id,
                kind=KIND_QUALITY_GATE,
                subject_type="session",
                subject_id=session_id,
                stage=g.name,
                outcome="pass" if g.passed else ("fail" if g.blocking else "warn"),
                reason=g.reason,
                metrics=g.metrics,
            )
        return report

    # --------------------------------------------------------------- gates
    def _observation_gate(self, session_id: str) -> GateResult:
        total = self.observation_count + self.browser_evidence_count
        ok = total >= self.t.min_observations
        return GateResult(
            name="observation_gate",
            passed=ok,
            blocking=True,
            reason=(
                f"{total} raw observations/evidence captures (floor {self.t.min_observations})"
                if ok else
                f"only {total} raw observations/captures vs floor {self.t.min_observations} — "
                "the agent reasoned more than it researched"
            ),
            metrics={
                "observations": self.observation_count,
                "browser_evidence": self.browser_evidence_count,
                "required": self.t.min_observations,
            },
        )

    def _evidence_gate(self, session_id: str) -> GateResult:
        claims = self.claims.list(session_id, limit=2000)
        cited: set[str] = set()
        for c in claims:
            cited.update(c.evidence_ids)
        # An opportunity's own cited evidence backs its conclusions too —
        # the chain-of-custody validator holds it to the same existence
        # rule, so counting it here measures reality, not optimism.
        for o in self.opportunities.list(session_id, limit=100):
            ids = (o.meta or {}).get("evidence_ids") or []
            if isinstance(ids, list):
                cited.update(str(e) for e in ids)
        known = self._known_ids(session_id)
        cited_before = len(cited)
        if known is not None:
            cited &= known
        phantom = cited_before - len(cited) if known is not None else 0
        # Extension-derived ids are vendor ESTIMATES: they may be cited as
        # context, but they never count as proof behind a conclusion.
        estimates = self._estimate_ids(session_id)
        estimate_cited = len(cited & estimates)
        cited -= estimates
        ok = len(cited) >= self.t.min_independent_sources
        est_txt = (
            f" ({estimate_cited} KDSpy estimate(s) cited but not counted as proof)"
            if estimate_cited else ""
        )
        return GateResult(
            name="evidence_gate",
            passed=ok,
            blocking=True,
            reason=(
                f"{len(cited)} distinct evidence records cited by claims/opportunities"
                + (f" ({phantom} invented id(s) ignored)" if phantom else "")
                + est_txt
                if ok else
                f"conclusions cite {len(cited)} real evidence records"
                + (f" ({phantom} invented id(s) ignored)" if phantom else "")
                + est_txt
                + f"; need >= {self.t.min_independent_sources} independent sources "
                "before conclusions are allowed"
            ),
            metrics={
                "cited_evidence": len(cited),
                "estimate_evidence_ignored": estimate_cited,
                "required": self.t.min_independent_sources,
            },
        )

    def _candidate_gate(self, session_id: str) -> GateResult:
        cands = self.candidates.list(session_id, limit=500)
        verified = [c for c in cands if c.status == CAND_VERIFIED]
        rejected = [c for c in cands if c.status == CAND_REJECTED]
        ok = len(cands) >= 3 and len(verified) >= 1
        return GateResult(
            name="candidate_gate",
            passed=ok,
            blocking=True,
            reason=(
                f"{len(cands)} candidates researched, {len(verified)} verified, {len(rejected)} rejected"
                if ok else
                f"candidate pipeline insufficient: {len(cands)} candidates, {len(verified)} verified "
                "(need >= 3 researched and >= 1 verified)"
            ),
            metrics={
                "candidates": len(cands),
                "verified": len(verified),
                "rejected": len(rejected),
            },
        )

    def _verification_coverage_gate(self, session_id: str) -> GateResult:
        opps = self.opportunities.list(session_id, limit=100)
        if not opps:
            return GateResult(
                name="verification_coverage_gate",
                passed=True,
                blocking=True,
                reason="no opportunities synthesized; nothing needs coverage",
                metrics={"opportunities": 0, "covered": 0, "ratio": 1.0},
            )
        covered = 0
        for o in opps:
            vers = self.verifications.for_candidate(session_id, o.candidate_id)
            if any(v.status in ("passed", "failed") for v in vers):
                covered += 1
        ratio = covered / len(opps)
        ok = ratio >= self.t.min_verification_coverage
        return GateResult(
            name="verification_coverage_gate",
            passed=ok,
            blocking=True,
            reason=(
                f"{covered}/{len(opps)} opportunities have a completed verification record"
                if ok else
                f"verification coverage {ratio:.2f} < {self.t.min_verification_coverage:.2f} — "
                "unverified opportunities must not reach the final report"
            ),
            metrics={"opportunities": len(opps), "covered": covered, "ratio": round(ratio, 3)},
        )

    def _supported_claims_gate(self, session_id: str) -> GateResult:
        claims = [c for c in self.claims.list(session_id, limit=2000) if c.kind != "other"]
        if not claims:
            return GateResult(
                name="supported_claims_gate",
                passed=False,
                blocking=True,
                reason="no claims registered — conclusions would rest on nothing",
                metrics={"claims": 0},
            )
        estimates = self._estimate_ids(session_id)
        unsupported = sum(
            1 for c in claims
            if not c.evidence_ids
            or c.status == CLAIM_UNSUPPORTED
            or claim_is_estimate_backed(c, estimates)
        )
        estimate_only = sum(1 for c in claims if claim_is_estimate_backed(c, estimates))
        ratio = 1.0 - (unsupported / len(claims))
        ok = ratio >= self.t.min_supported_claim_ratio
        est_txt = (
            f"; {estimate_only} rest only on KDSpy estimates and count as unsupported"
            if estimate_only else ""
        )
        return GateResult(
            name="supported_claims_gate",
            passed=ok,
            blocking=True,
            reason=(
                f"{len(claims) - unsupported}/{len(claims)} claims are evidence-backed "
                f"(ratio {ratio:.2f})"
                + est_txt
                if ok else
                f"only {ratio:.2f} of claims are evidence-backed (< {self.t.min_supported_claim_ratio:.2f}) — "
                "the session's conclusions rest on model assertion"
                + est_txt
            ),
            metrics={
                "claims": len(claims),
                "unsupported": unsupported,
                "estimate_backed": estimate_only,
                "ratio": round(ratio, 3),
            },
        )

    def _inference_load_gate(self, session_id: str) -> GateResult:
        claims = self.claims.list(session_id, limit=2000)
        if not claims:
            return GateResult(
                name="inference_load_gate",
                passed=False,
                blocking=True,
                reason="no claims registered",
                metrics={"claims": 0},
            )
        inference = sum(1 for c in claims if c.quality == EQ_INFERENCE)
        ratio = inference / len(claims)
        ok = ratio <= self.t.max_claim_inference_ratio
        return GateResult(
            name="inference_load_gate",
            passed=ok,
            blocking=False,  # a warning load, not a veto: supported ratio already blocks
            reason=(
                f"inference share {ratio:.2f} of {len(claims)} claims (max {self.t.max_claim_inference_ratio:.2f})"
                if ok else
                f"{inference}/{len(claims)} claims are pure model inference "
                f"({ratio:.2f} > {self.t.max_claim_inference_ratio:.2f}) — the research is partly self-talk"
            ),
            metrics={"claims": len(claims), "inference": inference, "ratio": round(ratio, 3)},
        )

    def _opportunity_gap_gate(self, session_id: str) -> GateResult:
        """Every opportunity must name a CONCRETE market gap whose existence is
        supported by an evidenced competition/market_gap claim on its
        candidate. An opportunity with a vague or unevidenced gap is a claim,
        not a finding — this is the Opportunity Gap Gate from the spec."""
        opps = self.opportunities.list(session_id, limit=100)
        if not opps:
            return GateResult(
                name="opportunity_gap_gate",
                passed=True,
                blocking=True,
                reason="no opportunities synthesized; nothing needs a gap",
                metrics={"opportunities": 0},
            )
        weak: list[str] = []
        for o in opps:
            meta = o.meta or {}
            gap = str(meta.get("market_gap") or "").strip()
            diff = str(meta.get("differentiation") or "").strip()
            if not gap or gap == "—" or len(gap) < 8:
                weak.append(f"{o.title!r} has no concrete market_gap")
                continue
            if not diff or diff == "—" or len(diff) < 6:
                weak.append(f"{o.title!r} has no concrete differentiation")
                continue
            # The gap must trace to evidence: at least one competition or
            # market_gap claim on the candidate citing real evidence ids.
            claims = self.claims.list(session_id, candidate_id=o.candidate_id, limit=500)
            gap_supported = any(
                c.kind in ("competition", "market_gap") and c.evidence_ids
                for c in claims
            )
            if not gap_supported:
                weak.append(f"{o.title!r}: market gap rests on no evidenced competitor claim")
        ok = not weak
        return GateResult(
            name="opportunity_gap_gate",
            passed=ok,
            blocking=True,
            reason=(
                f"all {len(opps)} opportunities name evidence-backed market gaps"
                if ok else
                "; ".join(weak[:3])
            ),
            metrics={"opportunities": len(opps), "weak_gaps": weak},
        )

    def _opportunity_diversity_gate(self, session_id: str) -> GateResult:
        opps = self.opportunities.list(session_id, limit=100)
        if len(opps) <= 1:
            return GateResult(
                name="opportunity_diversity_gate",
                passed=True,
                blocking=False,
                reason=f"{len(opps)} opportunity/opportunities; diversity check needs 2+",
                metrics={"opportunities": len(opps)},
            )
        dupes: list[tuple[str, str]] = []
        for i in range(len(opps)):
            for j in range(i + 1, len(opps)):
                if _similarity(opps[i].niche, opps[j].niche) >= 0.82:
                    dupes.append((opps[i].niche, opps[j].niche))
        ok = not dupes
        return GateResult(
            name="opportunity_diversity_gate",
            passed=ok,
            blocking=False,
            reason=(
                f"{len(opps)} opportunities are distinct"
                if ok else
                f"near-duplicate opportunities produced: {dupes[:3]}"
            ),
            metrics={"opportunities": len(opps), "duplicates": len(dupes)},
        )


def _similarity(a: str, b: str) -> float:
    aw = set(re.findall(r"[a-z0-9]+", a.lower()))
    bw = set(re.findall(r"[a-z0-9]+", b.lower()))
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / len(aw | bw)
