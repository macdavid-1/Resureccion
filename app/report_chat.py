"""Conversation with the analyst about a finished report.

After a run has written its report, the owner can ask questions and keep
asking: "who exactly buys that?", "what if I priced at 5.99?", "why did you
reject the second niche?". The answer must come from the SAME record the report
came from — the brief, every phase's analysis, the full evidence (search
results, product pages, prices, BSRs, reviews, publication data), the candidate
ledger with rejection reasons, the verification checks, the model's own notes
and reasoning, the screenshots, the earlier runs in the chain and the delivered
report — never from a summary of it.

Design:
  - `build_chat_dossier` assembles that record whole (nothing truncated).
  - the conversation is carried in full: every earlier question and answer is
    re-sent on every call, so the model keeps the thread without the backend
    having to summarise it.
  - `ChatStore` persists the conversation per report, so it survives restarts
    and reopening the report.
  - the model answers as prose (markdown), grounded: it cites `[ev:ID]`,
    never invents a figure, and says plainly when the research does not answer
    the question.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.timeutil import iso_now

CHAT_SYSTEM = """You are Resurrección's market analyst, and the owner is now talking to you about the report you just wrote. This is a conversation, not another report: answer the question they actually asked, in their language, and keep it short enough to read on a phone — then go as deep as the record allows when they ask for more.

You have the WHOLE research record from that run: the owner's brief, every phase's analysis, every captured page (search results with Amazon's totals, competitor products with price, BSR, rating, review counts and publication data, category charts, customer reviews and their text, the model's own notes and reasoning, the screenshots, the earlier runs in this investigation) and the report as delivered, plus the entire conversation so far. Treat it as your memory — not as a summary, and not as something to describe. The record is the source of every number you use.

HOW TO ANSWER
- Answer directly in the first line. No preamble, no restating the question, no describing the record or this conversation.
- Be specific: name the book, the figure, the phrase, the price, the BSR, the review count — the things the owner can act on. Quote buyer language from the reviews when it makes the point.
- Every load-bearing claim carries its evidence, exactly as in the report: append `[ev:ID]` using ids from the record.
- Use only figures that appear in the record. Never compute new numbers (percentages, differences, averages, rates), never estimate one, never round a measurement into a different quantity. If the record does not hold the number, say so plainly and say what would have to be measured.
- KDSpy values are third-party estimates. Say `estimated` when you use one.
- Never invent competitors, titles, prices, ranks, quotes, dates or search totals.
- If the evidence does not answer the question, say that — and say what the run did find that bears on it. A confident guess is worse than an honest gap.
- Speak as the analyst: "I looked at...", "the record shows...". Never mention phases, tools, prompts, gates, dossiers, JSON, models or how the report was produced.
- Markdown is welcome (short bullets, a small table when comparing numbers), but keep the shape of a chat reply: usually a few sentences or a tight list.
- Follow the thread. If the owner says "that one", "the second option", "make it cheaper", resolve it against the conversation and the report — do not restart from scratch. If they ask for a rewrite of a part of the report, write that part; never silently change the record.

STARTING A MINI SESSION
- Some questions cannot be answered from the finished research: a price or ranking that may have changed, whether a book or category still exists, a phrase nobody searched, a niche this run never touched. For those you can run a MINI SESSION — focused, self-directed work with the live browser, free of the research methodology's phases and contracts (the methodology is a reference you may read, not a cage).
- Propose one when it would change the answer, or when the owner asks you to go and check something. End your reply with exactly one fenced block, after your answer:
```mini-session
{"goal": "<exactly what you will do, specific enough to execute>", "why": "<what this settles for the owner>"}
```
- The owner decides whether it runs. Never claim a mini session's findings before it has run, and never propose one for something the record already answers.
- If they ask you to go find something new: propose the mini session when live measurement is what it needs, and mention the follow-up research run when it needs a whole new investigation.
"""

# The model's proposal for a mini session, as a fenced block at the end of an
# answer. It is parsed out (never shown as text) and offered to the owner as a
# button; nothing starts without the owner accepting it.
_PROPOSAL_RE = re.compile(
    r"```mini-session\s*\n?(\{.*?\})\s*\n?```", re.DOTALL | re.IGNORECASE
)
# Some models omit the fence and emit a bare object with the same key.
_PROPOSAL_BARE_RE = re.compile(
    r'(?m)^\s*\{\s*"goal"\s*:\s*".*?"\s*,\s*"why"\s*:\s*".*?"\s*\}\s*$', re.DOTALL
)


def extract_proposal(text: str) -> tuple[str, dict[str, Any] | None]:
    """Split a mini-session proposal out of an answer.

    Returns (answer_without_block, proposal). A malformed block is left in the
    text: better the owner sees something odd than a silently swallowed
    instruction.
    """
    raw = str(text or "")
    match = _PROPOSAL_RE.search(raw)
    if not match:
        match = _PROPOSAL_BARE_RE.search(raw)
    if not match:
        return raw.strip(), None
    try:
        data = json.loads(match.group(1) if match.re is _PROPOSAL_RE else match.group(0))
    except Exception:
        return raw.strip(), None
    if not isinstance(data, dict):
        return raw.strip(), None
    goal = " ".join(str(data.get("goal") or "").split())
    why = " ".join(str(data.get("why") or "").split())
    if not goal:
        return raw.strip(), None
    cleaned = (raw[: match.start()] + raw[match.end():]).strip()
    proposal = {"goal": goal[:2_000], "why": why[:1_000]}
    kind = str(data.get("kind") or "").strip()
    if kind:
        proposal["kind"] = kind[:60]
    return cleaned, proposal


_EV_RE = re.compile(r"\[ev:([A-Za-z0-9_\-]+)\]")


def link_citations(
    text: str, session_id: str, known: set[str]
) -> tuple[str, list[str], list[str]]:
    """Resolve `[ev:ID]` into a clickable evidence link (as the report does).

    A citation naming a record this session never captured is not a link: it
    is dropped from the text and reported, never left pointing at a 404.
    """
    cited: list[str] = []
    dropped: list[str] = []

    def _sub(m: re.Match[str]) -> str:
        ev_id = m.group(1)
        if ev_id not in known:
            dropped.append(ev_id)
            return ""
        if ev_id not in cited:
            cited.append(ev_id)
        return (
            f'[ev:{ev_id}](/api/sessions/{session_id}/evidence/{ev_id} '
            f'"Evidence {ev_id} — the captured page behind this answer")'
        )

    out = _EV_RE.sub(_sub, text)
    return out, cited, dropped


@dataclass
class ChatMessage:
    id: int
    report_id: str
    session_id: str
    role: str  # "owner" | "analyst"
    text: str
    created_at: str
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "report_id": self.report_id,
            "session_id": self.session_id,
            "role": self.role,
            "text": self.text,
            "created_at": self.created_at,
            "meta": self.meta,
        }


class ChatStore:
    """The owner↔analyst conversation attached to one report.

    Durable and ordered: `id` is autoincrement, so history is exactly the
    order it happened in, across restarts.
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    def list(self, session_id: str, report_id: str) -> list[ChatMessage]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM report_chat_messages WHERE session_id = ? AND report_id = ? ORDER BY id",
                (session_id, report_id),
            ).fetchall()
        return [self._row(r) for r in rows]

    def add(
        self,
        session_id: str,
        report_id: str,
        *,
        role: str,
        text: str,
        meta: dict[str, Any] | None = None,
    ) -> ChatMessage:
        if role not in ("owner", "analyst"):
            raise ValueError(f"invalid chat role {role!r}")
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO report_chat_messages (session_id, report_id, role, text, created_at, meta)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, report_id, role, text, iso_now(), json.dumps(meta or {})),
            )
            row_id = int(cur.lastrowid)
            row = conn.execute(
                "SELECT * FROM report_chat_messages WHERE id = ?", (row_id,)
            ).fetchone()
        return self._row(row)

    @staticmethod
    def _row(row: Any) -> ChatMessage:
        try:
            meta = json.loads(row["meta"] or "{}")
        except Exception:
            meta = {}
        return ChatMessage(
            id=int(row["id"]),
            report_id=str(row["report_id"]),
            session_id=str(row["session_id"]),
            role=str(row["role"]),
            text=str(row["text"]),
            created_at=str(row["created_at"]),
            meta=meta if isinstance(meta, dict) else {},
        )


def chat_dossier(state: Any, session_id: str, report: Any) -> dict[str, Any]:
    """The whole research record for a conversation, from an app-state object.

    Shared by the report chat and by mini sessions, so both always see exactly
    the same thing: every capture, candidate, verification, note, reasoning
    line, screenshot, phase analysis, earlier run in the chain and the report.
    """
    from app.continuation_build import prior_run_records
    from app.methodology import PHASE_ORDER

    verifications = getattr(state, "verifications", None)
    if verifications is None:
        from app.research_data import VerificationStore

        verifications = VerificationStore(state.db)
    prior_runs: list[dict[str, Any]] = []
    try:
        prior_runs = prior_run_records(
            session_id,
            sessions=state.sessions,
            candidates=state.candidates,
            opportunities=state.opportunities,
            verifications=verifications,
            notes=getattr(state, "notes", None),
            browser_evidence=state.browser_evidence,
            reports=state.reports,
            trace=getattr(state, "trace", None),
            thoughts=getattr(state, "thoughts", None),
            owner_messages=getattr(state, "owner_messages", None),
            memory=getattr(state, "research_memory", None),
        )
    except Exception:
        prior_runs = []
    return build_chat_dossier(
        session_id,
        report,
        sessions=state.sessions,
        candidates=state.candidates,
        opportunities=state.opportunities,
        verifications=verifications,
        claims=state.claims,
        browser_evidence=state.browser_evidence,
        observations=state.observations,
        notes=getattr(state, "notes", None),
        thoughts=getattr(state, "thoughts", None),
        trace=getattr(state, "trace", None),
        owner_messages=getattr(state, "owner_messages", None),
        memory=getattr(state, "research_memory", None),
        reports=state.reports,
        checkpoints=getattr(state, "checkpoints", None),
        phase_order=list(PHASE_ORDER),
        prior_runs=prior_runs,
        artifacts=getattr(state, "artifacts", None),
    )


def _phase_outputs(checkpoints: Any, session_id: str, order: list[str]) -> dict[str, Any]:
    """The run's per-phase analyses, as the runner checkpointed them."""
    out: dict[str, Any] = {}
    if checkpoints is None:
        return out
    for phase in order:
        try:
            cp = checkpoints.get_safe(session_id, f"phase_output:{phase}")
        except Exception:
            continue
        payload = (cp.payload or {}) if cp is not None else {}
        if isinstance(payload, dict) and payload:
            out[phase] = payload
    return out


def build_chat_dossier(
    session_id: str,
    report: Any,
    *,
    sessions: Any,
    candidates: Any,
    opportunities: Any,
    verifications: Any,
    claims: Any,
    browser_evidence: Any,
    observations: Any,
    notes: Any,
    thoughts: Any,
    trace: Any,
    owner_messages: Any,
    memory: Any,
    reports: Any,
    checkpoints: Any = None,
    phase_order: list[str] | None = None,
    prior_runs: list[dict[str, Any]] | None = None,
    artifacts: Any = None,
) -> dict[str, Any]:
    """Everything the research knows about this session, whole.

    Deliberately NOT the report writer's `_fit_context`: a conversation
    answer is usually narrow, and silently sampling the record is how an
    analyst ends up answering from a summary of their own work. If a record is
    huge the model reads it; the only bound is the model's context window.
    """
    session = sessions.require(session_id)
    model_json: dict[str, Any] = {}
    try:
        model_json = reports.read_model_json(report) or {}
    except Exception:
        model_json = {}
    body = ""
    try:
        body = str(reports.read_markdown(report) or "")
    except Exception:
        body = ""

    evidence_rows: list[dict[str, Any]] = []
    for ev in browser_evidence.list(session_id, limit=10_000_000):
        evidence_rows.append({
            "id": ev.id,
            "kind": ev.kind,
            "marketplace": ev.marketplace,
            "title": ev.title,
            "url": ev.url,
            "captured_at": ev.captured_at,
            "has_screenshot": bool(ev.screenshot_artifact_id),
            "data": ev.data,
        })

    cand_rows = [
        {
            "candidate_id": c.id,
            "niche": c.niche,
            "status": c.status,
            "marketplace": c.marketplace,
            "score": c.score,
            "rationale": c.rationale,
            "meta": c.meta,
        }
        for c in candidates.list(session_id, limit=10_000_000)
    ]
    ver_rows = [
        {
            "candidate_id": v.candidate_id,
            "verdict": v.verdict,
            "confidence": v.confidence,
            "checks": v.checks,
            "error": v.error,
        }
        for v in verifications.list(session_id, limit=10_000_000)
    ]
    claim_rows = [
        {
            "candidate_id": c.candidate_id,
            "statement": c.statement,
            "evidence_ids": list(c.evidence_ids or []),
            "status": c.status,
        }
        for c in claims.list(session_id, limit=10_000_000)
    ]
    obs_rows = [
        {"source": o.source, "kind": o.kind, "content": o.content, "at": o.created_at}
        for o in observations.list(session_id, limit=10_000_000)
    ]
    note_rows = [
        {"phase": n.phase, "niche": n.niche, "text": n.text}
        for n in notes.list(session_id, limit=10_000_000)
    ]
    reasoning_rows: list[dict[str, Any]] = []
    if thoughts is not None:
        try:
            rows = thoughts.every(session_id) if hasattr(thoughts, "every") else []
            for t in rows:
                detail = str(getattr(t, "detail", "") or "").strip()
                if detail:
                    reasoning_rows.append({
                        "label": str(getattr(t, "label", "") or "thinking"),
                        "at": str(getattr(t, "created_at", "") or ""),
                        "excerpt": detail,
                    })
        except Exception:
            reasoning_rows = []
    action_rows: list[dict[str, Any]] = []
    if trace is not None:
        try:
            for entry in trace.tail(session_id, limit=10_000_000):
                action_rows.append({
                    "at": getattr(entry, "created_at", ""),
                    "kind": getattr(entry, "kind", ""),
                    "text": str(getattr(entry, "text", "") or ""),
                })
        except Exception:
            action_rows = []
    owner_rows: list[dict[str, Any]] = []
    if owner_messages is not None:
        try:
            for m in owner_messages.journal(session_id, limit=10_000_000):
                owner_rows.append({
                    "at": getattr(m, "created_at", ""),
                    "text": str(getattr(m, "text", "") or ""),
                })
        except Exception:
            owner_rows = []
    memory_rows: list[dict[str, Any]] = []
    if memory is not None:
        try:
            for e in memory.list(session_id):
                text = str(getattr(e, "text", "") or "")
                if text.strip():
                    memory_rows.append({
                        "kind": getattr(e, "kind", "") or "memory",
                        "phase": getattr(e, "phase", "") or "",
                        "at": getattr(e, "created_at", "") or "",
                        "text": text,
                    })
        except Exception:
            memory_rows = []
    screenshot_rows: list[dict[str, Any]] = []
    for ev in evidence_rows:
        if not ev.get("has_screenshot"):
            continue
        screenshot_rows.append({
            "evidence_id": ev["id"],
            "kind": ev["kind"],
            "title": ev["title"],
            "marketplace": ev["marketplace"],
        })

    return {
        "session": {
            "id": session.id,
            "name": session.name,
            "mode": session.mode,
            "status": session.status,
            "objective": session.objective,
            "owner_brief": session.prompt,
            "marketplaces": list(session.marketplaces or []),
            "parent_session_id": getattr(session, "parent_session_id", None),
            "created_at": session.created_at,
        },
        "report": {
            "id": report.id,
            "title": report.title,
            "kind": report.kind,
            "created_at": report.created_at,
            "markdown": body,
            "meta": report.meta or {},
        },
        # The structured record the report was built from: opportunities,
        # competitors, consumer intelligence, the candidate ledger, the
        # evidence appendix, finalist dossiers, the adjacent ledger.
        "structured_report": model_json,
        "phases": _phase_outputs(checkpoints, session_id, phase_order or []),
        "candidates": cand_rows,
        "verifications": ver_rows,
        "claims": claim_rows,
        "observations": obs_rows,
        "notes": note_rows,
        "reasoning": reasoning_rows,
        "actions": action_rows,
        "owner_guidance": owner_rows,
        "memory": memory_rows,
        "screenshots": screenshot_rows,
        "evidence": evidence_rows,
        "prior_runs": list(prior_runs or []),
    }


def render_chat_prompt(
    dossier: dict[str, Any],
    history: list[ChatMessage],
    question: str,
) -> str:
    """The full record + the entire conversation + the new question.

    The transcript is passed verbatim: summarising the thread is how a chat
    assistant forgets what "that one" referred to three turns ago.
    """
    parts = [
        (
            "THE RESEARCH RECORD FOR THIS CONVERSATION (complete — the same "
            "record the report was written from, nothing summarised):"
        ),
        json.dumps(dossier, ensure_ascii=False, default=str),
    ]
    if history:
        lines = []
        for m in history:
            who = "OWNER" if m.role == "owner" else "ANALYST"
            lines.append(f"{who}: {m.text}")
        parts.append(
            "\nCONVERSATION SO FAR (oldest first — this thread is the context "
            "the owner is speaking in):\n" + "\n\n".join(lines)
        )
    parts.append(
        "\nOWNER'S QUESTION (answer it now, as the analyst who did this "
        "research; markdown, no preamble):\n" + question.strip()
    )
    return "\n".join(parts)


async def answer(
    *,
    model: Any,
    session_id: str,
    dossier: dict[str, Any],
    history: list[ChatMessage],
    question: str,
    max_tokens: int = 4_000,
) -> str:
    """One analyst turn. Returns the reply text (markdown)."""
    prompt = render_chat_prompt(dossier, history, question)
    reply = await model.call(
        session_id,
        system=CHAT_SYSTEM,
        user=prompt,
        label="Answering the owner's question about the report",
        max_tokens=max_tokens,
        text_mode=True,
    )
    return str(getattr(reply, "raw", "") or "").strip()
