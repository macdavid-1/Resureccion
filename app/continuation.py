"""The continuation brief: everything a follow-up run inherits from its parent.

"Start follow-up research" is not a keyword shortcut — the new run continues an
investigation, so it must KNOW that investigation: what it was asked, what it
concluded, which niches it verified, which it rejected and why, what it
measured (competitors, backend keywords, both category maps, consumer signals),
what it thought (the model's own notes and reasoning excerpts), what it DID
(the operational trace: phases, searches, pivots, verifications), what it left
unresolved (limitations, open adjacent niches, errors), and what it already
spent.

This module reads that record from the durable stores and renders it as the
follow-up session's brief. Two properties matter:

- **It is a RECORD, not a summary invented here.** Every line comes from a
  stored row; truncation is explicit ("+N more").
- **It is bounded.** A long run's trace and reasoning are far larger than a
  prompt should be, so each section carries a cap and says so. The full record
  stays where it always was (the parent session's own views).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Section caps. The PROMPT cap is the one that matters: every phase prompt
# carries this text, so it must stay small enough to leave room for the
# deterministic digests and the model's own work.
MAX_PROMPT_CHARS = 9_000
CANDIDATE_LIMIT = 14
OPPORTUNITY_LIMIT = 10
KEYWORD_LIMIT = 14
CATEGORY_LIMIT = 6
COMPETITOR_LIMIT = 8
THOUGHT_LIMIT = 8
THOUGHT_EXCERPT = 420
ACTION_LIMIT = 30
NOTE_LIMIT = 14
OPEN_THREAD_LIMIT = 12
REPORT_SUMMARY_CHARS = 1_600


def _clip(text: Any, limit: int) -> str:
    t = " ".join(str(text or "").split())
    if len(t) <= limit:
        return t
    return t[: max(0, limit - 1)].rstrip() + "…"


@dataclass
class ContinuationBrief:
    """The parent run's full record, structured for the follow-up's prompt."""

    parent_session_id: str
    parent_name: str = ""
    parent_status: str = ""
    parent_mode: str = ""
    parent_objective: str = ""
    parent_prompt: str = ""
    marketplaces: list[str] = field(default_factory=list)
    elapsed_minutes: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    report_title: str = ""
    report_summary: str = ""
    opportunities: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    rejected_candidates: list[dict[str, Any]] = field(default_factory=list)
    keywords: list[dict[str, Any]] = field(default_factory=list)
    categories: list[dict[str, Any]] = field(default_factory=list)
    competitors: list[dict[str, Any]] = field(default_factory=list)
    consumer: list[str] = field(default_factory=list)
    thoughts: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    open_threads: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- rendering
    def render(self, owner_instructions: str = "") -> str:
        """The follow-up session's brief: the record, then the new ask."""
        parts: list[str] = []
        parts.append(
            "CONTINUATION OF AN EARLIER RESEARCH RUN — you are resuming an "
            "investigation, not starting one."
        )
        parts.append(
            f"Previous session: {self.parent_name or self.parent_session_id} "
            f"(id {self.parent_session_id}, status {self.parent_status or 'unknown'}"
            + (f", mode {self.parent_mode}" if self.parent_mode else "")
            + (f", {self.elapsed_minutes} min of research" if self.elapsed_minutes else "")
            + ")."
        )
        if self.parent_objective:
            parts.append(f"Its objective was: {_clip(self.parent_objective, 400)}")
        if self.parent_prompt:
            parts.append(f"Its original brief: {_clip(self.parent_prompt, 1_200)}")
        if self.marketplaces:
            parts.append(f"Marketplaces it worked: {', '.join(self.marketplaces)}.")
        if self.counts:
            counts = ", ".join(f"{k}: {v}" for k, v in sorted(self.counts.items()) if v)
            if counts:
                parts.append(f"What it produced: {counts}.")

        # ---- conclusions ------------------------------------------------
        if self.report_title or self.report_summary:
            parts.append("\nITS REPORT")
            if self.report_title:
                parts.append(f"Title: {self.report_title}")
            if self.report_summary:
                parts.append(_clip(self.report_summary, REPORT_SUMMARY_CHARS))
        if self.opportunities:
            parts.append("\nWHAT IT RECOMMENDED (verified opportunities)")
            for o in self.opportunities[:OPPORTUNITY_LIMIT]:
                line = f"- {o.get('title') or o.get('niche')}"
                if o.get("niche") and o.get("niche") != o.get("title"):
                    line += f" — niche: {o['niche']}"
                if o.get("confidence") is not None:
                    line += f" (confidence {o['confidence']})"
                if o.get("verdict"):
                    line += f"; verification: {o['verdict']}"
                if o.get("angle"):
                    line += f"; angle: {_clip(o['angle'], 160)}"
                parts.append(line)
            if len(self.opportunities) > OPPORTUNITY_LIMIT:
                parts.append(f"- (+{len(self.opportunities) - OPPORTUNITY_LIMIT} more)")

        # ---- what it measured -------------------------------------------
        if self.keywords:
            parts.append(
                "\nBACKEND KEYWORDS IT MEASURED (Amazon result totals at the time — "
                "do not re-measure these unless you have a reason)"
            )
            for k in self.keywords[:KEYWORD_LIMIT]:
                line = f"- {k.get('keyword')}: {k.get('total_results', 'not printed')} results"
                if k.get("competition"):
                    line += f", competition {k['competition']}"
                if k.get("top10_review_median") is not None:
                    line += f", top-10 review median {k['top10_review_median']}"
                if k.get("marketplace"):
                    line += f" ({k['marketplace']})"
                parts.append(line)
            if len(self.keywords) > KEYWORD_LIMIT:
                parts.append(f"- (+{len(self.keywords) - KEYWORD_LIMIT} more)")
        if self.categories:
            parts.append("\nSTORE CATEGORIES IT MAPPED")
            for c in self.categories[:CATEGORY_LIMIT]:
                parts.append(
                    f"- [{c.get('store', 'books')}] {c.get('category')}"
                    + (f" ({c.get('breadcrumb')})" if c.get("breadcrumb") else "")
                )
        if self.competitors:
            parts.append("\nCOMPETITORS IT OBSERVED")
            for c in self.competitors[:COMPETITOR_LIMIT]:
                parts.append(
                    f"- {c.get('title') or c.get('name') or c.get('asin')}"
                    + (f" — {c.get('note')}" if c.get("note") else "")
                )
        if self.consumer:
            parts.append("\nWHAT BUYERS SAID")
            for line in self.consumer[:6]:
                parts.append(f"- {_clip(line, 220)}")

        # ---- what it explored and killed --------------------------------
        if self.candidates:
            parts.append("\nCANDIDATES IT KEPT")
            for c in self.candidates[:CANDIDATE_LIMIT]:
                line = f"- {c.get('niche')} [{c.get('status')}]"
                if c.get("marketplace"):
                    line += f" ({c['marketplace']})"
                if c.get("rationale"):
                    line += f" — {_clip(c['rationale'], 180)}"
                parts.append(line)
        if self.rejected_candidates:
            parts.append(
                "\nCANDIDATES IT REJECTED (do not re-propose these without new evidence)"
            )
            for c in self.rejected_candidates[:CANDIDATE_LIMIT]:
                line = f"- {c.get('niche')}"
                if c.get("why"):
                    line += f" — {_clip(c['why'], 200)}"
                parts.append(line)

        # ---- how it thought and what it did ------------------------------
        if self.thoughts:
            parts.append("\nITS WORKING THOUGHTS (excerpts from its own reasoning)")
            for t in self.thoughts[:THOUGHT_LIMIT]:
                parts.append(f"- [{t.get('label') or 'thinking'}] {_clip(t.get('excerpt'), THOUGHT_EXCERPT)}")
        if self.notes:
            parts.append("\nNOTES IT LEFT FOR ITSELF")
            for n in self.notes[:NOTE_LIMIT]:
                prefix = f"[{n['phase']}] " if n.get("phase") else ""
                parts.append(f"- {prefix}{_clip(n.get('text'), 240)}")
        if self.actions:
            parts.append("\nWHAT IT ACTUALLY DID (operational trace, newest last)")
            for a in self.actions[:ACTION_LIMIT]:
                kind = f"[{a['kind']}] " if a.get("kind") else ""
                parts.append(f"- {kind}{_clip(a.get('text'), 200)}")
            if len(self.actions) >= ACTION_LIMIT:
                parts.append("- (older entries omitted)")

        # ---- loose ends --------------------------------------------------
        if self.limitations:
            parts.append("\nLIMITATIONS IT RECORDED")
            for line in self.limitations[:6]:
                parts.append(f"- {_clip(line, 220)}")
        if self.open_threads:
            parts.append(
                "\nOPEN THREADS IT DID NOT RESEARCH (these are why this run exists)"
            )
            for line in self.open_threads[:OPEN_THREAD_LIMIT]:
                parts.append(f"- {_clip(line, 220)}")

        # ---- the new ask ------------------------------------------------
        if owner_instructions.strip():
            parts.append(
                "\nNEW INSTRUCTIONS FROM THE OWNER FOR THIS RUN (they take "
                "precedence over the notes above where they conflict):\n"
                + owner_instructions.strip()[:3_000]
            )
        else:
            parts.append(
                "\nNO NEW INSTRUCTIONS WERE GIVEN. Continue from the open threads "
                "above: go deeper on the strongest verified opportunities, "
                "resolve the recorded limitations, and research the adjacency "
                "this run left unfunded. Do not repeat measurement that already "
                "exists in this record."
            )
        text = "\n".join(parts)
        if len(text) > MAX_PROMPT_CHARS:
            text = text[: MAX_PROMPT_CHARS - 200].rstrip() + (
                "\n… (brief truncated; the full record is in the previous session)"
            )
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_session_id": self.parent_session_id,
            "parent_name": self.parent_name,
            "parent_status": self.parent_status,
            "parent_mode": self.parent_mode,
            "parent_objective": self.parent_objective,
            "marketplaces": list(self.marketplaces),
            "elapsed_minutes": self.elapsed_minutes,
            "counts": dict(self.counts),
            "report_title": self.report_title,
            "opportunities": self.opportunities,
            "candidates": self.candidates,
            "rejected_candidates": self.rejected_candidates,
            "keywords": self.keywords,
            "categories": self.categories,
            "competitors": self.competitors,
            "consumer": self.consumer,
            "thoughts": self.thoughts,
            "actions": self.actions,
            "notes": self.notes,
            "open_threads": self.open_threads,
            "limitations": self.limitations,
        }
