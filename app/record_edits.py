"""The model's write access to its own research records.

Research is not write-once. The model is the one reading the pages, and after
more evidence it regularly needs to correct what the ledger says: narrow a
niche, fix the marketplace a result actually came from, re-word a rationale,
re-score or re-status a candidate, drop a line of research it has proven wrong,
correct a wrong result total, or delete a claim it can no longer support.

Before this module the model could only CREATE rows (and let the deterministic
pipeline reject them), so a mistaken record stayed in the ledger and travelled
into the report. Every edit here is:

- **targeted** — by id, or by a niche/title the model already knows;
- **auditable** — the runner journals each edit (and each refusal) to the
  session's event log and trace, exactly like a browser action;
- **honest** — an ambiguous target or a missing row is refused with the
  candidates it could mean, never silently applied to "something like" it.

This is record editing, not browser access: :mod:`app.actions` drives the page
and :mod:`app.model_browser` owns the Playwright verbs.
"""
from __future__ import annotations

from typing import Any

from app.browser_store import BrowserEvidenceStore
from app.claims import ClaimError, ClaimStore
from app.product_rules import screen_niche
from app.research_data import CandidateStore, OpportunityStore

# Actions the model may request to edit its own ledgers. Kept here (not in
# app.methodology) so "what a record edit is" has ONE home; the methodology
# specs import the tuple so every phase can use them.
RECORD_ACTIONS: tuple[str, ...] = (
    "add_candidate",
    "update_candidate",
    "delete_candidate",
    "update_opportunity",
    "delete_opportunity",
    "update_evidence",
    "delete_evidence",
    "update_claim",
    "delete_claim",
)

CANDIDATE_STATUSES_HINT = "discovered, verifying, verified, rejected"


class RecordEditError(Exception):
    """A record edit could not be applied (missing/ambiguous target)."""


def _clip(text: Any, limit: int = 200) -> str:
    clean = " ".join(str(text or "").split())
    return clean[: max(0, limit - 1)] + "…" if len(clean) > limit else clean


class RecordEditor:
    """Applies the model's record edits through the durable stores."""

    def __init__(
        self,
        *,
        candidates: CandidateStore,
        opportunities: OpportunityStore,
        claims: ClaimStore,
        browser_evidence: BrowserEvidenceStore,
    ) -> None:
        self.candidates = candidates
        self.opportunities = opportunities
        self.claims = claims
        self.browser_evidence = browser_evidence

    # ------------------------------------------------------------- dispatch
    def apply(self, session_id: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run one record edit. Raises RecordEditError with a usable reason."""
        handler = getattr(self, f"_do_{action}", None)
        if action not in RECORD_ACTIONS or handler is None:
            raise RecordEditError(f"unknown record edit {action!r}")
        payload = dict(args or {})
        return handler(session_id, payload)

    # ------------------------------------------------------------ candidates
    def _find_candidate(self, session_id: str, args: dict[str, Any]) -> Any:
        cand_id = str(args.get("candidate_id") or args.get("id") or "").strip()
        if cand_id:
            cand = self.candidates.get(session_id, cand_id)
            if cand is None:
                raise RecordEditError(
                    f"candidate {cand_id} is not in this session — read the "
                    "candidate ledger in the prompt and use one of those ids"
                )
            return cand
        needle = " ".join(str(args.get("niche") or "").lower().split())
        if not needle:
            raise RecordEditError(
                "update/delete_candidate needs candidate_id (preferred) or niche"
            )
        rows = self.candidates.list(session_id, limit=500)
        exact = [c for c in rows if " ".join(str(c.niche).lower().split()) == needle]
        matches = exact or [
            c for c in rows if needle in " ".join(str(c.niche).lower().split())
        ]
        if not matches:
            raise RecordEditError(
                f"no candidate matches {args.get('niche')!r}; ledger has: "
                + "; ".join(f"{c.id} {_clip(c.niche, 70)}" for c in rows[:8])
            )
        if len(matches) > 1:
            raise RecordEditError(
                f"{len(matches)} candidates match {args.get('niche')!r} — use "
                "candidate_id: " + "; ".join(f"{c.id} {_clip(c.niche, 70)}" for c in matches[:8])
            )
        return matches[0]

    def _do_add_candidate(self, session_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """A niche the model found while browsing, not in its own plan."""
        niche = " ".join(str(args.get("niche") or "").split())
        if not niche:
            raise RecordEditError("add_candidate needs a niche")
        blocked = screen_niche(niche)
        if blocked:
            # Hard rules outrank the model's own judgement: refused here so the
            # model learns why in the action's error, not an hour later.
            raise RecordEditError(blocked)
        needle = niche.lower()
        for existing in self.candidates.list(session_id, limit=500):
            if " ".join(str(existing.niche).lower().split()) == needle:
                raise RecordEditError(
                    f"candidate {existing.id} already covers {niche!r} — edit it "
                    "with update_candidate instead"
                )
        try:
            score = float(args.get("score") or 0.0)
        except (TypeError, ValueError) as exc:
            raise RecordEditError(f"score must be a number: {args.get('score')!r}") from exc
        meta = dict(args.get("meta") or {}) if isinstance(args.get("meta"), dict) else {}
        meta.setdefault("source", "model_action")
        cand = self.candidates.create(
            session_id,
            niche=niche,
            marketplace=str(args.get("marketplace") or "").strip().lower(),
            rationale=str(args.get("rationale") or "").strip(),
            score=score,
            meta=meta,
        )
        status = str(args.get("status") or "").strip()
        if status and status != "discovered":
            try:
                cand = self.candidates.update_fields(session_id, cand.id, status=status)
            except ValueError as exc:
                raise RecordEditError(str(exc)) from exc
        return {
            "ok": True,
            "target": {"candidate_id": cand.id, "niche": cand.niche},
            "summary": f"added candidate {_clip(cand.niche, 80)!r} ({cand.id})",
        }

    def _do_update_candidate(self, session_id: str, args: dict[str, Any]) -> dict[str, Any]:
        cand = self._find_candidate(session_id, args)
        fields = {
            key: args[key]
            for key in ("niche", "marketplace", "rationale", "score", "status", "meta")
            if key in args
        }
        if not fields:
            raise RecordEditError(
                "update_candidate needs at least one field: niche, marketplace, "
                f"rationale, score, status ({CANDIDATE_STATUSES_HINT}), meta"
            )
        try:
            updated = self.candidates.update_fields(session_id, cand.id, **fields)
        except ValueError as exc:  # invalid status / empty niche
            raise RecordEditError(str(exc)) from exc
        return {
            "ok": True,
            "changed": sorted(fields),
            "target": {"candidate_id": updated.id, "niche": updated.niche},
            "summary": (
                f"updated candidate {_clip(updated.niche, 80)!r}: "
                + ", ".join(f"{k}={_clip(v, 60)}" for k, v in fields.items() if k != "meta")
            ),
        }

    def _do_delete_candidate(self, session_id: str, args: dict[str, Any]) -> dict[str, Any]:
        cand = self._find_candidate(session_id, args)
        removed = self.candidates.delete(session_id, cand.id)
        reason = _clip(args.get("reason") or "", 200)
        return {
            "ok": True,
            "target": {"candidate_id": cand.id, "niche": cand.niche},
            "removed": removed,
            "summary": (
                f"deleted candidate {_clip(cand.niche, 80)!r} (and "
                f"{removed['verifications']} verification(s), "
                f"{removed['opportunities']} opportunit(y/ies))"
                + (f": {reason}" if reason else "")
            ),
        }

    # ---------------------------------------------------------- opportunities
    def _find_opportunity(self, session_id: str, args: dict[str, Any]) -> Any:
        opp_id = str(args.get("opportunity_id") or args.get("id") or "").strip()
        if opp_id:
            opp = self.opportunities.get(session_id, opp_id)
            if opp is None:
                raise RecordEditError(f"opportunity {opp_id} is not in this session")
            return opp
        needle = " ".join(
            str(args.get("title") or args.get("niche") or "").lower().split()
        )
        if not needle:
            raise RecordEditError(
                "update/delete_opportunity needs opportunity_id (preferred) or title"
            )
        rows = self.opportunities.list(session_id, limit=200)
        exact = [
            o for o in rows
            if needle in (
                " ".join(str(o.title).lower().split()),
                " ".join(str(o.niche).lower().split()),
            )
        ]
        matches = exact or [
            o for o in rows
            if needle in " ".join(str(o.title).lower().split())
            or needle in " ".join(str(o.niche).lower().split())
        ]
        if not matches:
            raise RecordEditError(
                f"no opportunity matches {args.get('title') or args.get('niche')!r}"
            )
        if len(matches) > 1:
            raise RecordEditError(
                f"{len(matches)} opportunities match — use opportunity_id: "
                + "; ".join(f"{o.id} {_clip(o.title, 70)}" for o in matches[:8])
            )
        return matches[0]

    def _do_update_opportunity(self, session_id: str, args: dict[str, Any]) -> dict[str, Any]:
        opp = self._find_opportunity(session_id, args)
        fields = {
            key: args[key]
            for key in (
                "title", "niche", "marketplace", "angle", "keywords",
                "projected_demand", "projected_competition", "confidence", "meta",
            )
            if key in args
        }
        if not fields:
            raise RecordEditError(
                "update_opportunity needs at least one field: title, niche, "
                "marketplace, angle, keywords, projected_demand, "
                "projected_competition, confidence, meta"
            )
        try:
            updated = self.opportunities.update_fields(session_id, opp.id, **fields)
        except ValueError as exc:
            raise RecordEditError(str(exc)) from exc
        return {
            "ok": True,
            "changed": sorted(fields),
            "target": {"opportunity_id": updated.id, "title": updated.title},
            "summary": (
                f"updated opportunity {_clip(updated.title, 80)!r}: "
                + ", ".join(k for k in fields if k != "meta")
            ),
        }

    def _do_delete_opportunity(self, session_id: str, args: dict[str, Any]) -> dict[str, Any]:
        opp = self._find_opportunity(session_id, args)
        self.opportunities.delete(session_id, opp.id)
        return {
            "ok": True,
            "target": {"opportunity_id": opp.id, "title": opp.title},
            "summary": f"deleted opportunity {_clip(opp.title, 80)!r}",
        }

    # --------------------------------------------------------------- evidence
    def _do_update_evidence(self, session_id: str, args: dict[str, Any]) -> dict[str, Any]:
        evidence_id = str(args.get("evidence_id") or args.get("id") or "").strip()
        if not evidence_id:
            raise RecordEditError("update_evidence needs evidence_id")
        data_patch = dict(args.get("data") or {}) if isinstance(args.get("data"), dict) else {}
        # Convenience: correcting the competition figure in one key keeps the
        # row internally consistent (result_count IS the total, by contract).
        if "total_results" in args:
            total = args.get("total_results")
            data_patch["total_results"] = int(total) if str(total).strip() else None
            data_patch["result_count"] = data_patch["total_results"]
            data_patch["total_results_source"] = "model-correction"
            data_patch.pop("results_count_note", None)
        if "total_results_are_approximate" in args:
            data_patch["total_results_are_approximate"] = bool(
                args.get("total_results_are_approximate")
            )
        title = args.get("title")
        if not data_patch and title is None:
            raise RecordEditError(
                "update_evidence needs title and/or data (or total_results to "
                "correct the competition figure)"
            )
        row = self.browser_evidence.update(
            session_id, evidence_id, title=title, data=data_patch or None
        )
        if row is None:
            raise RecordEditError(f"evidence {evidence_id} is not in this session")
        return {
            "ok": True,
            "changed": sorted(data_patch) or ["title"],
            "target": {"evidence_id": row.id, "kind": row.kind},
            "summary": f"corrected evidence {row.id} ({row.kind})",
        }

    def _do_delete_evidence(self, session_id: str, args: dict[str, Any]) -> dict[str, Any]:
        evidence_id = str(args.get("evidence_id") or args.get("id") or "").strip()
        if not evidence_id:
            raise RecordEditError("delete_evidence needs evidence_id")
        if not self.browser_evidence.delete(session_id, evidence_id):
            raise RecordEditError(f"evidence {evidence_id} is not in this session")
        return {
            "ok": True,
            "target": {"evidence_id": evidence_id},
            "summary": f"deleted evidence {evidence_id}",
        }

    # ----------------------------------------------------------------- claims
    def _claim(self, session_id: str, args: dict[str, Any]) -> Any:
        claim_id = str(args.get("claim_id") or args.get("id") or "").strip()
        if claim_id:
            claim = self.claims.get(session_id, claim_id)
            if claim is None:
                raise RecordEditError(f"claim {claim_id} is not in this session")
            return claim
        # The model may know a claim by its wording rather than its id: match
        # the statement it wrote, and refuse an ambiguous match with the ids.
        needle = " ".join(str(args.get("statement") or "").lower().split())
        if not needle:
            raise RecordEditError("claim edits need claim_id (or the statement text)")
        rows = self.claims.list(session_id, limit=500)
        matches = [
            c for c in rows
            if needle in " ".join(str(c.statement).lower().split())
        ]
        if not matches:
            raise RecordEditError(f"no claim matches {args.get('statement')!r}")
        if len(matches) > 1:
            raise RecordEditError(
                f"{len(matches)} claims match — use claim_id: "
                + "; ".join(f"{c.id} {_clip(c.statement, 70)}" for c in matches[:8])
            )
        return matches[0]

    def _do_update_claim(self, session_id: str, args: dict[str, Any]) -> dict[str, Any]:
        claim = self._claim(session_id, args)
        fields = {
            key: args[key]
            for key in ("statement", "kind", "interpretation", "evidence_ids", "meta")
            if key in args
        }
        if not fields:
            raise RecordEditError(
                "update_claim needs at least one field: statement, kind, "
                "interpretation, evidence_ids, meta"
            )
        try:
            updated = self.claims.update_fields(session_id, claim.id, **fields)
        except ClaimError as exc:
            raise RecordEditError(str(exc)) from exc
        return {
            "ok": True,
            "changed": sorted(fields),
            "target": {"claim_id": updated.id},
            "summary": f"updated claim {updated.id}: " + ", ".join(sorted(fields)),
        }

    def _do_delete_claim(self, session_id: str, args: dict[str, Any]) -> dict[str, Any]:
        claim = self._claim(session_id, args)
        self.claims.delete(session_id, claim.id)
        return {
            "ok": True,
            "target": {"claim_id": claim.id},
            "summary": f"deleted claim {_clip(claim.statement, 90)!r}",
        }
