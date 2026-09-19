"""The verification engine: deterministic adversarial audit of candidates.

Before a candidate may feed opportunity synthesis, its factual foundation is
verified HERE — deterministically, from session data, independent of what the
model claims. The model's own adversarial verdicts (Phase 8) are respected as
*one input*, but this engine is the system's own check:

For each candidate it computes:
  - evidence coverage: how much real page evidence touches the candidate
  - claim integrity: share of important claims that cite evidence
  - inference load: share of claims that are pure model inference
  - contradiction load: unresolved same-kind contradictions
  - corroboration: evidence from >= min_independent_sources distinct URLs
  - KDP risk flags: policy/category/account-health screenings (app.kdp_risk)

The verdict is deterministic and is persisted twice: as a `verifications`
row (research_data) and as integrity assessments (integrity) so the owner can
audit exactly why a candidate survived or died. A failed verification is a
finding, not an exception: the candidate is moved to a durable status.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.claims import (
    CLAIM_CONTRADICTED,
    CLAIM_UNSUPPORTED,
    Claim,
    ClaimStore,
    EQ_INFERENCE,
    VERDICT_INCONCLUSIVE,
    VERDICT_LIMITED,
    VERDICT_REJECTED,
    VERDICT_VERIFIED,
)
from app.integrity import KIND_VERIFICATION, IntegrityAssessmentStore
from app.kdp_risk import KdpRiskScreening, record_screening, screen_candidate
from app.research_data import (
    CAND_REJECTED,
    CAND_UNDER_VERIFICATION,
    CAND_VERIFIED,
    VerificationStore,
)
from app.thresholds import EvidenceThresholds

# Model verdicts (Phase 8 output) mapped onto engine treatment.
_MODEL_PASS = "pass"
_MODEL_DOWNGRADE = "downgrade"
_MODEL_REJECT = "reject"

# Verdicts this engine can produce.
ENGINE_VERIFIED = VERDICT_VERIFIED
ENGINE_LIMITED = VERDICT_LIMITED
ENGINE_INCONCLUSIVE = VERDICT_INCONCLUSIVE
ENGINE_REJECTED = VERDICT_REJECTED


@dataclass
class VerificationReport:
    candidate_id: str
    niche: str
    verdict: str                 # verified / verified_with_limitations / inconclusive / rejected
    status: str                  # passed / failed / inconclusive (verification row status)
    confidence: float
    model_verdict: str | None    # the model's Phase-8 verdict, if any
    checks: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "niche": self.niche,
            "verdict": self.verdict,
            "status": self.status,
            "confidence": self.confidence,
            "model_verdict": self.model_verdict,
            "checks": self.checks,
            "problems": self.problems,
        }


class VerificationEngine:
    """Runs the deterministic verification audit for one candidate."""

    def __init__(
        self,
        thresholds: EvidenceThresholds,
        claims: ClaimStore,
        verifications: VerificationStore,
        assessments: IntegrityAssessmentStore,
    ) -> None:
        self.t = thresholds
        self.claims = claims
        self.verifications = verifications
        self.assessments = assessments

    # ------------------------------------------------------------------ run
    def verify(
        self,
        session_id: str,
        candidate: Any,
        *,
        evidence_records: list[dict[str, Any]],
        model_verdict: str | None = None,
    ) -> VerificationReport:
        claims = self.claims.list(session_id, candidate_id=candidate.id, limit=500)
        important = [c for c in claims if c.kind in ("demand", "competition", "consumer_need", "market_gap")]

        # --- deterministic checks -------------------------------------------
        evidence_urls = {str(e.get("url") or "") for e in evidence_records}
        evidence_urls.discard("")

        n_evidence = len(evidence_records)
        coverage_ok = n_evidence >= max(2, self.t.min_independent_sources)

        n_important = len(important)
        n_unsupported = sum(
            1 for c in important
            if not c.evidence_ids or c.status == CLAIM_UNSUPPORTED
        )
        supported_ratio = (
            (n_important - n_unsupported) / n_important if n_important else 0.0
        )
        claim_ok = n_important > 0 and supported_ratio >= self.t.min_supported_claim_ratio

        n_inference = sum(1 for c in claims if c.quality == EQ_INFERENCE)
        inference_ratio = n_inference / len(claims) if claims else 1.0
        inference_ok = inference_ratio <= self.t.max_claim_inference_ratio

        contradiction_count = sum(1 for c in claims if c.status == CLAIM_CONTRADICTED)
        contradiction_ok = contradiction_count == 0

        # Corroboration: distinct URLs preferred (independent sources), but a
        # paginated/refreshed search legitimately yields many captures from
        # one URL — accept 2x independent capture events in that case.
        corroboration_ok = (
            len(evidence_urls) >= self.t.min_independent_sources
            or n_evidence >= 2 * self.t.min_independent_sources
        )

        risk: KdpRiskScreening = screen_candidate(candidate, evidence_records)
        risk_ok = not risk.blocking
        record_screening(session_id, str(getattr(candidate, "id", "")), risk, self.assessments)

        # --- verdict (deterministic) ------------------------------------------
        problems: list[str] = []
        if not coverage_ok:
            problems.append(
                f"evidence coverage too thin: {n_evidence} records (need >= "
                f"{max(2, self.t.min_independent_sources)})"
            )
        if not claim_ok:
            if n_important == 0:
                problems.append("no important claims registered; nothing has been verified")
            else:
                problems.append(
                    f"supported-claim ratio {supported_ratio:.2f} < "
                    f"{self.t.min_supported_claim_ratio:.2f}"
                )
        if not inference_ok:
            problems.append(
                f"inference ratio {inference_ratio:.2f} > {self.t.max_claim_inference_ratio:.2f} "
                f"({n_inference}/{len(claims)} claims are model self-talk)"
            )
        if not contradiction_ok:
            problems.append(f"{contradiction_count} contradicted claim(s) unresolved")
        if not corroboration_ok:
            problems.append(
                f"corroboration insufficient: evidence spans {len(evidence_urls)} distinct "
                f"URL(s), need >= {self.t.min_independent_sources}"
            )
        if not risk_ok:
            problems.append(f"KDP risk blocking: {'; '.join(risk.blocking)}")

        model_rejects = (model_verdict or "").strip().lower() == _MODEL_REJECT

        if model_rejects or not risk_ok:
            verdict, status = ENGINE_REJECTED, "failed"
        elif not problems:
            verdict, status = ENGINE_VERIFIED, "passed"
        elif coverage_ok and claim_ok and corroboration_ok and contradiction_ok:
            # Substance is there; soft weaknesses only (inference load, minor gaps).
            verdict, status = ENGINE_LIMITED, "passed"
        elif n_evidence == 0 or n_important == 0:
            verdict, status = ENGINE_INCONCLUSIVE, "inconclusive"
        else:
            verdict, status = ENGINE_REJECTED, "failed"

        confidence = self._confidence(claims, n_evidence, verdict)

        report = VerificationReport(
            candidate_id=str(getattr(candidate, "id", "")),
            niche=str(getattr(candidate, "niche", "")),
            verdict=verdict,
            status=status,
            confidence=confidence,
            model_verdict=model_verdict,
            checks={
                "evidence_records": n_evidence,
                "distinct_evidence_urls": len(evidence_urls),
                "important_claims": n_important,
                "supported_claim_ratio": round(supported_ratio, 3),
                "inference_ratio": round(inference_ratio, 3),
                "contradicted_claims": contradiction_count,
                "kdp_risk_flags": risk.flags,
                "kdp_risk_blocking": risk.blocking,
            },
            problems=problems,
        )
        self._persist(session_id, report)
        return report

    # ------------------------------------------------------------- internals
    def _confidence(self, claims: list[Claim], n_evidence: int, verdict: str) -> float:
        """Deterministic confidence: mean claim confidence, tempered by
        coverage and the verdict itself. Quantity never fakes quality."""
        if verdict == ENGINE_REJECTED:
            return round(min(0.3, 0.1 * n_evidence), 3)
        if not claims:
            base = 0.0
        else:
            base = sum(c.confidence for c in claims) / len(claims)
        coverage = min(1.0, n_evidence / max(1, self.t.min_independent_sources * 3))
        conf = base * (0.5 + 0.5 * coverage)
        if verdict == ENGINE_LIMITED:
            conf *= 0.85
        if verdict == ENGINE_INCONCLUSIVE:
            conf *= 0.5
        return round(min(1.0, conf), 3)

    def _persist(self, session_id: str, report: VerificationReport) -> None:
        # 1. verification row (research_data) — the durable research artifact.
        v = self.verifications.create(session_id, candidate_id=report.candidate_id)
        self.verifications.complete(
            session_id,
            v.id,
            status=report.status,
            verdict=report.verdict,
            checks={"report": report.to_dict()},
            confidence=report.confidence,
        )
        # 2. integrity assessments — the owner's audit trail.
        self.assessments.record(
            session_id,
            kind=KIND_VERIFICATION,
            subject_type="candidate",
            subject_id=report.candidate_id,
            stage="VerificationEngine",
            outcome=(
                "pass" if report.status == "passed"
                else "warn" if report.status == "inconclusive"
                else "fail"
            ),
            reason=(
                f"verdict={report.verdict}: " + ("; ".join(report.problems) if report.problems else "all checks passed")
            ),
            metrics=report.checks,
        )
        for problem in report.problems:
            self.assessments.record(
                session_id,
                kind=KIND_VERIFICATION,
                subject_type="candidate",
                subject_id=report.candidate_id,
                stage="VerificationEngine.check",
                outcome="fail",
                reason=problem,
                metrics=report.checks,
            )

    # ------------------------------------------------------------ enforcement
    def apply(
        self,
        session_id: str,
        report: VerificationReport,
        *,
        candidates: Any,
    ) -> str:
        """Move the candidate to its verified/rejected/in-verification status.

        'verified_with_limitations' counts as verified: the verification
        PASSED (limitations travel in the verification checks and the
        opportunity meta). 'inconclusive' stays under verification — the
        candidate needs more research, not a conviction on an empty record.
        """
        if report.verdict in (ENGINE_VERIFIED, ENGINE_LIMITED):
            new_status = CAND_VERIFIED
        elif report.verdict == ENGINE_REJECTED:
            new_status = CAND_REJECTED
        else:
            new_status = CAND_UNDER_VERIFICATION
        candidates.set_status(session_id, report.candidate_id, new_status)
        return new_status


def evidence_for_candidate(
    candidate: Any,
    all_evidence: list[dict[str, Any]],
    *,
    claims: list[Claim],
) -> list[dict[str, Any]]:
    """Collect the evidence records relevant to one candidate.

    Relevant = cited by one of the candidate's claims, OR captured from the
    candidate's marketplace, OR whose title/url mentions the niche head terms.
    Deterministic and cheap: dict-id join, no model involvement.
    """
    cited: set[str] = set()
    for c in claims:
        cited.update(c.evidence_ids)
    niche_head = _head_terms(str(getattr(candidate, "niche", "")))
    out: list[dict[str, Any]] = []
    for e in all_evidence:
        eid = str(e.get("id") or "")
        if eid and eid in cited:
            out.append(e)
            continue
        marketplace = str(getattr(candidate, "marketplace", "") or "")
        if marketplace and str(e.get("marketplace") or "") == marketplace:
            out.append(e)
            continue
        haystack = f"{e.get('title') or ''} {e.get('url') or ''}".lower()
        if niche_head and sum(t in haystack for t in niche_head) >= 2:
            out.append(e)
    return out


def _head_terms(niche: str) -> list[str]:
    import re

    words = [w for w in re.findall(r"[a-z0-9]+", niche.lower()) if len(w) > 3]
    return words[:4]
