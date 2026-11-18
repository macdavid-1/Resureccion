"""The continuation record: everything a follow-up run inherits from its parent.

"Start follow-up research" is not a keyword shortcut — the new run continues an
investigation, so it must KNOW that investigation the way its own model knew it
at the end of the previous run: the exact brief the owner sent, every message
the owner typed while it ran, the full report it wrote, every measured keyword,
category, competitor and buyer quote, every candidate it kept and every one it
killed and why, every note it left itself, every word of reasoning it produced,
the operational trace, what it left unresolved, and the memory it wrote.

Two properties matter:

- **It is a RECORD, not a summary invented here.** Every line comes from a
  stored row (``app.continuation_build`` does the reading); nothing is
  re-worded, condensed or inferred. The report, the owner's own words, the
  notes and the reasoning are reproduced verbatim.
- **It is unbounded.** Nothing here caps how much a follow-up may receive: no
  character budget, no "top N", no excerpts, no truncation. A six-hour parent
  run hands its successor everything it wrote. (The model this app runs has a
  1M-token window; the previous 9k-then-16k "brief" was not a safety limit, it
  was the reason a continuation felt like a fresh run.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContinuationBrief:
    """The parent run's full record, structured for the follow-up's prompt."""

    parent_session_id: str
    parent_name: str = ""
    parent_status: str = ""
    parent_mode: str = ""
    parent_objective: str = ""
    # The brief the owner sent when the parent run STARTED — verbatim.
    parent_prompt: str = ""
    marketplaces: list[str] = field(default_factory=list)
    elapsed_minutes: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    # Everything the owner typed while the parent ran, oldest first.
    owner_messages: list[dict[str, Any]] = field(default_factory=list)
    report_title: str = ""
    # The newest report exactly as the parent wrote it.
    report_markdown: str = ""
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
    # The parent's own long-running memory (app.research_memory), verbatim.
    # It is NOT rendered here: `seed_memory` copies it into the follow-up's own
    # memory journal, which app.research_memory renders IN FULL on every step.
    # Rendering it twice would duplicate a potentially huge block in every
    # prompt of the new run.
    memory: list[dict[str, Any]] = field(default_factory=list)
    open_threads: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- rendering
    def _blocks(self) -> list[tuple[str, str]]:
        """Every section of the record, in reading order: (key, text)."""
        blocks: list[tuple[str, str]] = []

        head = [
            (
                "CONTINUATION OF AN EARLIER RESEARCH RUN — you are resuming an "
                "investigation that is already under way. Everything below is "
                "your OWN prior work, not a description of someone else's run."
            ),
            f"Previous session: {self.parent_name or self.parent_session_id} "
            f"(id {self.parent_session_id}, status {self.parent_status or 'unknown'}"
            + (f", mode {self.parent_mode}" if self.parent_mode else "")
            + (f", {self.elapsed_minutes} min of research" if self.elapsed_minutes else "")
            + ").",
        ]
        if self.parent_objective:
            head.append(f"Its objective was: {self.parent_objective}")
        if self.marketplaces:
            head.append(f"Marketplaces it worked: {', '.join(self.marketplaces)}.")
        if self.counts:
            counts = ", ".join(f"{k}: {v}" for k, v in sorted(self.counts.items()) if v)
            if counts:
                head.append(f"What it produced: {counts}.")
        head.append(
            "Continue that investigation. Do NOT restart it: the measurements, "
            "verdicts, dead ends and instructions below stand until new evidence "
            "on a page changes them."
        )
        blocks.append(("head", "\n".join(head)))

        if self.parent_prompt.strip():
            blocks.append((
                "parent_brief",
                "\nITS ORIGINAL BRIEF (verbatim — the exact text the owner sent "
                "when this investigation started; still in force):\n"
                "----- BEGIN ORIGINAL BRIEF -----\n"
                + self.parent_prompt.strip()
                + "\n----- END ORIGINAL BRIEF -----",
            ))

        if self.owner_messages:
            lines = [
                (
                    "\nEVERY INSTRUCTION THE OWNER GAVE DURING IT (verbatim, "
                    "oldest first — these outrank anything else in this record "
                    "where they conflict, and they are still in force):"
                )
            ]
            for m in self.owner_messages:
                stamp = f"({m['created_at']}) " if m.get("created_at") else ""
                lines.append(f"- {stamp}{m.get('text', '')}")
            blocks.append(("owner_messages", "\n".join(lines)))

        report = self.report_markdown.strip()
        if report or self.report_title:
            body = [
                (
                    "\nITS REPORT (the report it wrote at the end of that run — "
                    "verbatim, the whole thing)"
                )
            ]
            if self.report_title:
                body.append(f"Title: {self.report_title}")
            if report:
                body.append("----- BEGIN REPORT -----\n" + report + "\n----- END REPORT -----")
            elif self.report_summary:
                body.append(self.report_summary)
            blocks.append(("report", "\n".join(body)))

        if self.opportunities:
            lines = ["\nWHAT IT RECOMMENDED (its verified opportunities)"]
            for o in self.opportunities:
                line = f"- {o.get('title') or o.get('niche')}"
                if o.get("niche") and o.get("niche") != o.get("title"):
                    line += f" — niche: {o['niche']}"
                if o.get("confidence") is not None:
                    line += f" (confidence {o['confidence']})"
                if o.get("verdict"):
                    line += f"; verification: {o['verdict']}"
                if o.get("angle"):
                    line += f"; angle: {o['angle']}"
                lines.append(line)
            blocks.append(("opportunities", "\n".join(lines)))

        if self.keywords:
            lines = [
                (
                    "\nBACKEND KEYWORDS IT MEASURED (Amazon's own OVERALL "
                    "totals at the time — not the first page's row count; do "
                    "not re-measure these unless a candidate needs a NEW "
                    "phrase measured)"
                )
            ]
            for k in self.keywords:
                total = k.get("total_results")
                label = f"{total:,}" if isinstance(total, int) else "not printed"
                line = f"- {k.get('keyword')}: {label} total results"
                if k.get("competition"):
                    line += f", competition {k['competition']}"
                if k.get("top10_review_median") is not None:
                    line += f", top-10 review median {k['top10_review_median']}"
                if k.get("marketplace"):
                    line += f" ({k['marketplace']})"
                lines.append(line)
            blocks.append(("keywords", "\n".join(lines)))

        if self.categories:
            lines = ["\nSTORE CATEGORIES IT MAPPED"]
            for c in self.categories:
                lines.append(
                    f"- [{c.get('store', 'books')}] {c.get('category')}"
                    + (f" ({c.get('breadcrumb')})" if c.get("breadcrumb") else "")
                )
            blocks.append(("categories", "\n".join(lines)))

        if self.competitors:
            lines = ["\nCOMPETITORS IT OBSERVED"]
            for c in self.competitors:
                lines.append(
                    f"- {c.get('title') or c.get('name') or c.get('asin')}"
                    + (f" — {c.get('note')}" if c.get("note") else "")
                )
            blocks.append(("competitors", "\n".join(lines)))

        if self.consumer:
            lines = ["\nWHAT BUYERS SAID (captured review material)"]
            for line in self.consumer:
                lines.append(f"- {line}")
            blocks.append(("consumer", "\n".join(lines)))

        if self.candidates:
            lines = ["\nCANDIDATES IT KEPT"]
            for c in self.candidates:
                line = f"- {c.get('niche')} [{c.get('status')}]"
                if c.get("marketplace"):
                    line += f" ({c['marketplace']})"
                if c.get("rationale"):
                    line += f" — {c['rationale']}"
                lines.append(line)
            blocks.append(("candidates", "\n".join(lines)))

        if self.rejected_candidates:
            lines = [
                (
                    "\nCANDIDATES IT REJECTED (final unless a page shows new "
                    "evidence — do not re-propose them from memory)"
                )
            ]
            for c in self.rejected_candidates:
                lines.append(
                    f"- {c.get('niche')}"
                    + (f" — {c['why']}" if c.get("why") else "")
                )
            blocks.append(("rejected", "\n".join(lines)))

        if self.thoughts:
            lines = [
                (
                    "\nITS WORKING THOUGHTS (its own reasoning, in order — what "
                    "it concluded and why)"
                )
            ]
            for t in self.thoughts:
                label = t.get("label") or "thinking"
                stamp = f" ({t['created_at']})" if t.get("created_at") else ""
                lines.append(f"- [{label}{stamp}]\n  {t.get('excerpt', '')}")
            blocks.append(("thoughts", "\n".join(lines)))

        if self.notes:
            lines = ["\nNOTES IT LEFT FOR ITSELF (all of them)"]
            for n in self.notes:
                prefix = f"[{n['phase']}] " if n.get("phase") else ""
                lines.append(f"- {prefix}{n.get('text', '')}")
            blocks.append(("notes", "\n".join(lines)))

        if self.actions:
            lines = ["\nWHAT IT ACTUALLY DID (its operational trace)"]
            for a in self.actions:
                kind = f"[{a['kind']}] " if a.get("kind") else ""
                lines.append(f"- {kind}{a.get('text', '')}")
            blocks.append(("actions", "\n".join(lines)))

        if self.limitations:
            lines = ["\nLIMITATIONS IT RECORDED"]
            for line in self.limitations:
                lines.append(f"- {line}")
            blocks.append(("limitations", "\n".join(lines)))

        if self.open_threads:
            lines = [
                (
                    "\nOPEN THREADS IT DID NOT FINISH (candidates for this run, "
                    "not orders)"
                )
            ]
            for line in self.open_threads:
                lines.append(f"- {line}")
            blocks.append(("open_threads", "\n".join(lines)))

        return blocks

    def record_text(self) -> str:
        """Just the record, with no closing ask.

        Used when an earlier run's record is supplied as CONTEXT rather than as
        a follow-up brief — the report writer for a follow-up run holds the
        whole chain, and appending "you decide what this run does first" to a
        report dossier would be nonsense.
        """
        return "\n".join(body for _key, body in self._blocks())

    def render(self, owner_instructions: str = "") -> str:
        """The follow-up session's prompt: the whole record, then the new ask."""
        text = self.record_text()

        # ---- the new ask -------------------------------------------------
        if owner_instructions.strip():
            text += (
                "\n\nNEW INSTRUCTIONS FROM THE OWNER FOR THIS RUN (they take "
                "precedence over the record above where they conflict):\n"
                + owner_instructions.strip()
            )
        else:
            text += (
                "\n\nNO NEW INSTRUCTIONS WERE GIVEN. You decide what this run "
                "does first, from the record above: continue the strongest "
                "verified work, resolve the limitations and open threads, and "
                "extend the investigation where it actually stopped. Do not "
                "rebuild a keyword pool or re-run measurements that already "
                "exist in this record."
            )
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_session_id": self.parent_session_id,
            "parent_name": self.parent_name,
            "parent_status": self.parent_status,
            "parent_mode": self.parent_mode,
            "parent_objective": self.parent_objective,
            "parent_prompt": self.parent_prompt,
            "marketplaces": list(self.marketplaces),
            "elapsed_minutes": self.elapsed_minutes,
            "counts": dict(self.counts),
            "owner_messages": self.owner_messages,
            "report_title": self.report_title,
            "report_markdown": self.report_markdown,
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
            "memory": self.memory,
            "open_threads": self.open_threads,
            "limitations": self.limitations,
        }
