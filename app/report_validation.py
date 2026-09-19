"""Adversarial report validation — the last line of defense.

The final report is the deliverable. Before it may be delivered as "final",
it is adversarially validated HERE, deterministically, against the session's
durable state:

  - every presented opportunity survives the structural opportunity contract,
  - every opportunity's candidate has a verification record with a passing
    verdict (verified or verified_with_limitations),
  - every opportunity traces to evidence: its candidate's claims cite real
    evidence ids (the chain-of-custody rule from app.claims),
  - every opportunity's marketplace attribution matches the candidate's
    marketplace (competitor/marketplace information must belong to the right
    market),
  - KDP risk: any candidate with blocking risk flags must NOT have produced
    an opportunity (the verification engine should have rejected it),
  - the session passes its blocking quality gates (app.quality_gates),
  - no credential-shaped material survives in the report body (redaction
    scan — the report must never leak secrets).

If any check fails, the report is delivered as `provisional` with the
failures listed IN the report itself — the owner sees exactly why the
session's output is not fully verified. Nothing is hidden.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.claims import ClaimStore
from app.integrity import (
    KIND_KDP_RISK,
    KIND_REPORT_VALIDATION,
    IntegrityAssessmentStore,
)
from app.methodology import validate_opportunity
from app.quality_gates import GateReport, QualityGateRunner
from app.research_data import CandidateStore, OpportunityStore, VerificationStore
from app.redact import redact

# Verification verdicts that allow a "final" (not provisional) report.
_FINAL_VERDICTS = {"verified", "verified_with_limitations"}


@dataclass
class ReportValidation:
    passed: bool
    mode: str                     # "final" | "provisional"
    failures: list[str] = field(default_factory=list)
    gate_report: dict[str, Any] = field(default_factory=dict)
    per_opportunity: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "mode": self.mode,
            "failures": self.failures,
            "gate_report": self.gate_report,
            "per_opportunity": self.per_opportunity,
        }


class ReportValidator:
    """Validates a session's final deliverable against its durable state."""

    def __init__(
        self,
        claims: ClaimStore,
        opportunities: OpportunityStore,
        verifications: VerificationStore,
        gate_runner: QualityGateRunner,
        assessments: IntegrityAssessmentStore,
        *,
        candidates: CandidateStore,
        browser_evidence: Any | None = None,
        evidence: Any | None = None,
    ) -> None:
        self.claims = claims
        self.opportunities = opportunities
        self.verifications = verifications
        self.gate_runner = gate_runner
        self.assessments = assessments
        self.candidates = candidates
        # Evidence stores for existence checks (browser + research evidence).
        self._evidence_stores = (browser_evidence, evidence)

    # ------------------------------------------------------------------ run
    def validate(self, session_id: str, *, report_body: str = "") -> ReportValidation:
        failures: list[str] = []
        per_opportunity: list[dict[str, Any]] = []

        # 1. Quality gates (blocking gates must pass).
        gate_report: GateReport = self.gate_runner.run(session_id)
        if not gate_report.passed:
            failures.extend(
                f"quality gate failed: {name}" for name in gate_report.failed_blocking
            )

        # 2. Per-opportunity adversarial checks.
        opps = self.opportunities.list(session_id, limit=100)
        for o in opps:
            # Attach the candidate's marketplace for the attribution check.
            cand = self.candidates.get(session_id, o.candidate_id)
            o._candidate_marketplace = cand.marketplace if cand else ""
            problems = self._check_opportunity(session_id, o)
            per_opportunity.append({
                "opportunity_id": o.id,
                "title": o.title,
                "niche": o.niche,
                "problems": problems,
                "ok": not problems,
            })
            failures.extend(f"opportunity {o.title!r}: {p}" for p in problems)

        # 2b. Portfolio-level checks: duplication + marketplace registry.
        failures.extend(self._check_portfolio(opps))

        # 3. Redaction scan of the rendered body: no credential-shaped keys.
        if report_body:
            leaks = self._redaction_scan(report_body)
            if leaks:
                failures.append(f"report body failed redaction scan: {leaks}")

        passed = not failures
        validation = ReportValidation(
            passed=passed,
            mode="final" if passed else "provisional",
            failures=failures,
            gate_report=gate_report.to_dict(),
            per_opportunity=per_opportunity,
        )
        self._persist(session_id, validation)
        return validation

    # ------------------------------------------------------ per-opportunity
    def _check_opportunity(self, session_id: str, o: Any) -> list[str]:
        problems: list[str] = []

        # Structural contract.
        ok, struct = validate_opportunity({
            "niche": o.niche,
            "parent_market": (o.meta or {}).get("parent_market"),
            "target_reader": (o.meta or {}).get("target_reader"),
            "reader_problem": (o.meta or {}).get("reader_problem"),
            "marketplaces": [m for m in (o.marketplace or "").split(",") if m],
            "evidence_ids": (o.meta or {}).get("evidence_ids"),
            "competitive_landscape": (o.meta or {}).get("competitive_landscape"),
            "consumer_needs": (o.meta or {}).get("consumer_needs"),
            "market_gap": (o.meta or {}).get("market_gap"),
            "differentiation": (o.meta or {}).get("differentiation"),
            "risks": (o.meta or {}).get("risks"),
            "keywords": o.keywords,
            "title_concepts": (o.meta or {}).get("title_concepts"),
            "positioning": o.angle,
            "confidence": o.confidence,
            "verification_status": (o.meta or {}).get("verification_status"),
        })
        if not ok:
            problems.extend(f"structural: {s}" for s in struct)

        # Verification record with a passing verdict.
        vers = self.verifications.for_candidate(session_id, o.candidate_id)
        passing = [v for v in vers if v.verdict in _FINAL_VERDICTS and v.status == "passed"]
        if not vers:
            problems.append("no verification record — opportunity is unverified")
        elif not passing:
            problems.append(
                "verification exists but no passing verdict "
                f"(verdicts: {[v.verdict for v in vers]})"
            )

        # Chain of custody: the candidate's claims must cite real evidence.
        claims = self.claims.list(session_id, candidate_id=o.candidate_id, limit=500)
        cited: set[str] = set()
        for c in claims:
            cited.update(c.evidence_ids)
        meta_ids = (o.meta or {}).get("evidence_ids") or []
        if isinstance(meta_ids, list):
            cited.update(str(e) for e in meta_ids)
        if not cited:
            problems.append(
                "chain-of-custody: neither the opportunity nor its candidate's "
                "claims cite any evidence ids"
            )
        # Evidence references must EXIST in this session: a rendered report
        # must never point at fabricated or foreign ids. (Skipped only when
        # evidence stores are not wired into the validator.)
        known = self._known_evidence_ids(session_id)
        if known is not None:
            phantom = sorted(e for e in cited if e not in known)
            if phantom:
                problems.append(
                    f"evidence references do not exist in session: {phantom[:5]}"
                )

        # Marketplace attribution: the opportunity's claimed marketplaces must
        # include the candidate's own marketplace — competitor information
        # attributed to the wrong market poisons cross-market conclusions.
        cand_marketplace = str(getattr(o, "_candidate_marketplace", "") or "")
        opp_marketplaces = [m.strip().lower() for m in (o.marketplace or "").split(",") if m.strip()]
        if opp_marketplaces and not cand_marketplace:
            problems.append(
                "marketplace attribution: candidate has no recorded marketplace "
                f"but opportunity claims {opp_marketplaces}"
            )
        elif opp_marketplaces and cand_marketplace and cand_marketplace.lower() not in opp_marketplaces:
            problems.append(
                f"marketplace attribution: candidate marketplace {cand_marketplace!r} "
                f"missing from opportunity marketplaces {opp_marketplaces}"
            )

        # KDP risk: a blocking screening must have killed this candidate
        # upstream. If an opportunity exists anyway, the pipeline was bypassed.
        risk = self.assessments.list(
            session_id, kind=KIND_KDP_RISK, subject_id=o.candidate_id, limit=50,
        )
        for r in risk:
            blocking = (r.metrics or {}).get("blocking") or []
            if r.outcome == "fail" and blocking:
                problems.append(
                    f"KDP risk: candidate carries blocking flags {blocking} but "
                    "produced an opportunity"
                )
                break

        return problems

    # -------------------------------------------------------------- helpers
    def _known_evidence_ids(self, session_id: str) -> set[str] | None:
        """Every evidence id that genuinely exists in this session.

        Returns None when the evidence stores are not wired (legacy callers) —
        the existence check then cannot run and must not false-positive.
        """
        browser_evidence, evidence = self._evidence_stores
        if browser_evidence is None or evidence is None:
            return None
        return {e.id for e in browser_evidence.list(session_id, limit=5000)} | {
            e.id for e in evidence.list(session_id, limit=5000)
        }

    def _check_portfolio(self, opps: list[Any]) -> list[str]:
        """Portfolio-level adversarial checks on the final opportunity set."""
        problems: list[str] = []
        # Duplicated opportunities: the same niche (or the same candidate)
        # presented twice dilutes the portfolio and double-counts demand.
        by_niche: dict[str, str] = {}
        by_candidate: dict[str, str] = {}
        for o in opps:
            key = " ".join(str(o.niche or "").lower().split())
            if key and key in by_niche:
                problems.append(
                    f"duplicated opportunities: {o.title!r} and {by_niche[key]!r} "
                    "share the same niche"
                )
            else:
                by_niche[key] = o.title
            if o.candidate_id and o.candidate_id in by_candidate:
                problems.append(
                    f"duplicated opportunities: {o.title!r} and {by_candidate[o.candidate_id]!r} "
                    "come from the same candidate"
                )
            else:
                by_candidate[o.candidate_id] = o.title
        # Marketplace names must exist in the supported registry — a wrong
        # code silently corrupts every marketplace comparison downstream.
        from app.marketplace import AMAZON_MARKETPLACES

        for o in opps:
            for m in (o.marketplace or "").split(","):
                code = m.strip().lower()
                if code and code not in AMAZON_MARKETPLACES:
                    problems.append(
                        f"unknown marketplace code {code!r} on opportunity {o.title!r}"
                    )
        # Numeric sanity: confidence must be a real bounded value.
        for o in opps:
            try:
                conf = float(o.confidence)
            except (TypeError, ValueError):
                problems.append(f"opportunity {o.title!r}: confidence is not numeric")
                continue
            if not 0.0 <= conf <= 1.0:
                problems.append(f"opportunity {o.title!r}: confidence {conf} out of range")
        return problems

    @staticmethod
    def _redaction_scan(body: str) -> list[str]:
        """Scan rendered report text for credential-shaped assignments that
        redact() would have removed. We render the body through redact-parsed
        JSON to detect key-shaped leaks deterministically."""
        import json

        # Simulate: if the body contained `key: value` pairs with forbidden
        # key names, they would appear as plain text. Detect the key names.
        forbidden = ("authorization", "cookie", "set-cookie", "session-token",
                     "x-amz-security-token", "password", "api_key", "bearer")
        low = body.lower()
        leaks = []
        for f in forbidden:
            if f + ":" in low or f + "=" in low or f + '":' in low:
                # Confirm it is a real assignment, not prose mentioning the word.
                import re

                if re.search(rf"{re.escape(f)}\s*[:=]\s*\S", low):
                    leaks.append(f)
        return leaks

    def _persist(self, session_id: str, validation: ReportValidation) -> None:
        self.assessments.record(
            session_id,
            kind=KIND_REPORT_VALIDATION,
            subject_type="report",
            subject_id=session_id,
            stage="ReportValidator",
            outcome="pass" if validation.passed else "fail",
            reason=(
                "final report fully validated"
                if validation.passed
                else "; ".join(validation.failures[:8])
            ),
            metrics={
                "mode": validation.mode,
                "failure_count": len(validation.failures),
                "gate_passed": validation.gate_report.get("passed"),
            },
            meta=redact({"failures": validation.failures[:20]}),
        )
