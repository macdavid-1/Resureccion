"""Mini sessions: focused, self-directed work started from a report conversation.

A chat answer can only be as good as the record behind it. When a question needs
something the finished research never measured — a price that moved, whether a
book still exists, a phrase nobody searched, a niche the run never touched — the
analyst can propose a MINI SESSION: a small, focused piece of live work the owner
approves with one tap.

What makes it "mini" is that it is NOT the nine-phase methodology:

  - there are no phases, no order, no output contracts and no exit gates;
  - the methodology is supplied as REFERENCE the model may read and then ignore;
  - the model decides what to do, in what order, and when the job is done, using
    the same browser actions the research agent uses;
  - it carries the SAME complete record a chat answer carries (every capture,
    candidate, verification, note, reasoning line, screenshot, the report, the
    whole conversation, and — for a follow-up run — every earlier run in the
    chain), because a mini session about an old finding is only useful if it
    knows the old finding;
  - the owner can type into the conversation while it runs and the model
    receives those messages on its next step;
  - its answer is posted back into the conversation as the analyst's message,
    with citations resolved to real evidence.

There is no limit on how many mini sessions a conversation may run. Each one is
durable (row + step log), so the owner can see exactly what it did afterwards.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.methodology import PHASE_ORDER, PHASE_TITLES, SPECS
from app.timeutil import iso_now

MINI_SYSTEM = """You are Resurrección's market analyst, running a MINI SESSION: one focused, self-directed piece of work that the owner approved from your conversation about a finished report. You drive a real browser against Amazon (and the allow-listed external sources) and then report back into the conversation.

YOU ARE NOT INSIDE THE RESEARCH METHODOLOGY. Its nine phases, exit gates, output contracts and ordering are REFERENCE ONLY — read them if they help you decide what matters, and ignore anything that does not fit the task. You choose what to do, in what order, how deep, and when the job is done. A mini session can be for anything the owner asked: check a price, test a phrase, count reviews, look at a category, verify an incumbent still exists, size a niche nobody measured.

THE RECORD
You receive the whole research record the report was written from — every captured page, the candidate ledger with rejection reasons, the verification checks, your own notes and reasoning, the screenshots, the report, the whole conversation so far (including, for a follow-up, the earlier runs in this investigation). Treat it as your memory: do not re-measure what it already holds, build on it, and use its ids when you cite something.

HOW TO WORK
- Use the browser instead of guessing. One step at a time: read the result of what you just did before deciding the next move.
- Prefer the direct measurement: a search for the phrase, a product page for price/BSR/reviews, `read_reviews` for buyer language, a category list for where a book can rank.
- When the owner types to you mid-session, take it as guidance for the rest of the task. If it changes the goal, follow the new goal.
- Stop as soon as the question is settled. A mini session is not a research run: a handful of precise measurements beats twenty exploratory pages. If something cannot be measured, say so instead of widening forever.

REPLY FORMAT — exactly one JSON object, nothing else:
{"note": "<one line: what you are doing and why>", "actions": [{"action": "<name>", "args": {...}}], "done": false, "answer": ""}
- `actions` runs then you are called again with their results. Omit or empty it when you have nothing left to run.
- Finish with {"note": "...", "actions": [], "done": true, "answer": "<your answer to the owner>"}.
- `answer` is markdown written to the owner in the chat: what you found, the numbers verbatim, and `[ev:ID]` citations for the ids you captured (ids from the record are fine too). No preamble, no methodology narration.

GROUNDING (the same law as the report)
- Use only figures you measured in this mini session or that appear in the record. Never compute new numbers (percentages, differences, averages, rates), never estimate one.
- Never invent competitors, titles, prices, ranks, quotes, dates or search totals. KDSpy values are third-party estimates: say `estimated`.
- If the measurement did not settle a point, say so plainly — an honest gap is a useful answer.

ACTIONS YOU CAN RUN (args are plain JSON; marketplace defaults to the run's plan)
- search_marketplace {query, marketplace?} — Books-department search; returns the printed total and results.
- autocomplete {query, marketplace?} — Amazon's search suggestions for a targeted phrase (a generic seed is refused).
- open_product {asin, marketplace?} — one book's page: price, BSR, rating, review count, publication data.
- read_reviews {asin, max_reviews?} — real customer reviews, with a screenshot of the review section.
- related_products {asin, marketplace?} / bestsellers {path?} / new_releases {path?} / open_category {path}
- kdspy_data {asin?} — KDSpy's own keyword panel for the current page, when the add-on is loaded.
- google_suggest {query} / forum_search {query} / goodreads_check {query} — external demand signals (mid-research only by methodology; free here).
- capture_evidence {note} — record what is on screen as evidence (a screenshot plus a note).
- record_observation {content} — note something about the market that has no field of its own.
- navigate {url} / click {selector?|text?} / type {selector, text} / press {key} / scroll {direction?, amount?} / hover {selector?|text?} / select_option {selector, value} / evaluate {script} / wait_for {selector?, seconds?} / go_back / go_forward / reload / screenshot — direct browser control when the structured actions do not fit.
- browser {action: open|close|status} — the browser session itself.
"""


# --------------------------------------------------------------- data records
@dataclass
class MiniSession:
    id: str
    session_id: str
    report_id: str
    goal: str
    title: str
    status: str
    created_at: str
    updated_at: str
    finished_at: str | None = None
    result: str = ""
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "report_id": self.report_id,
            "goal": self.goal,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
            "meta": self.meta,
        }


@dataclass
class MiniSessionStep:
    id: int
    mini_id: str
    created_at: str
    kind: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mini_id": self.mini_id,
            "created_at": self.created_at,
            "kind": self.kind,
            "text": self.text,
            "meta": self.meta,
        }


class MiniSessionStore:
    """Durable mini-session rows and their step logs."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def create(
        self,
        *,
        session_id: str,
        report_id: str,
        goal: str,
        title: str = "",
        meta: dict[str, Any] | None = None,
    ) -> MiniSession:
        now = iso_now()
        mini_id = uuid.uuid4().hex
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO mini_sessions (id, session_id, report_id, goal, title,"
                " status, created_at, updated_at, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (mini_id, session_id, report_id, goal, title, "queued", now, now,
                 json.dumps(meta or {})),
            )
        return self.get(mini_id)  # type: ignore[return-value]

    def get(self, mini_id: str) -> MiniSession | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM mini_sessions WHERE id = ?", (mini_id,)
            ).fetchone()
        return self._row(row) if row else None

    def list(self, session_id: str, report_id: str | None = None) -> list[MiniSession]:
        q = "SELECT * FROM mini_sessions WHERE session_id = ?"
        params: list[Any] = [session_id]
        if report_id:
            q += " AND report_id = ?"
            params.append(report_id)
        q += " ORDER BY created_at DESC, rowid DESC"
        with self.db.read() as conn:
            rows = conn.execute(q, tuple(params)).fetchall()
        return [self._row(r) for r in rows]

    def active(self, session_id: str, report_id: str | None = None) -> list[MiniSession]:
        return [
            m for m in self.list(session_id, report_id)
            if m.status in ("queued", "running")
        ]

    def active_for_report(self, report_id: str) -> MiniSession | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM mini_sessions WHERE report_id = ?"
                " AND status IN ('queued', 'running') ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (report_id,),
            ).fetchone()
        return self._row(row) if row else None

    def update(self, mini_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = iso_now()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE mini_sessions SET {assignments} WHERE id = ?",
                (*fields.values(), mini_id),
            )

    def add_step(
        self,
        mini_id: str,
        *,
        kind: str,
        text: str,
        meta: dict[str, Any] | None = None,
    ) -> MiniSessionStep:
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO mini_session_steps (mini_id, created_at, kind, text, meta)"
                " VALUES (?, ?, ?, ?, ?)",
                (mini_id, iso_now(), kind, text, json.dumps(meta or {})),
            )
            row = conn.execute(
                "SELECT * FROM mini_session_steps WHERE id = ?", (int(cur.lastrowid),)
            ).fetchone()
        return self._step(row)

    def steps(self, mini_id: str, *, after_id: int = 0) -> list[MiniSessionStep]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM mini_session_steps WHERE mini_id = ? AND id > ? ORDER BY id",
                (mini_id, after_id),
            ).fetchall()
        return [self._step(r) for r in rows]

    def owner_steps(self, mini_id: str, *, after_id: int = 0) -> list[MiniSessionStep]:
        return [s for s in self.steps(mini_id, after_id=after_id) if s.kind == "owner"]

    def reconcile_on_boot(self) -> int:
        """A process that died mid-session must not claim it is still running."""
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT id FROM mini_sessions WHERE status IN ('queued', 'running')"
            ).fetchall()
        for row in rows:
            self.update(
                str(row["id"]), status="interrupted",
                error="the app restarted while this mini session was running",
            )
        return len(rows)

    @staticmethod
    def _row(row: Any) -> MiniSession:
        try:
            meta = json.loads(row["meta"] or "{}")
        except Exception:
            meta = {}
        return MiniSession(
            id=str(row["id"]),
            session_id=str(row["session_id"]),
            report_id=str(row["report_id"]),
            goal=str(row["goal"]),
            title=str(row["title"] or ""),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            finished_at=row["finished_at"],
            result=str(row["result"] or ""),
            error=row["error"],
            meta=meta if isinstance(meta, dict) else {},
        )

    @staticmethod
    def _step(row: Any) -> MiniSessionStep:
        try:
            meta = json.loads(row["meta"] or "{}")
        except Exception:
            meta = {}
        return MiniSessionStep(
            id=int(row["id"]),
            mini_id=str(row["mini_id"]),
            created_at=str(row["created_at"]),
            kind=str(row["kind"]),
            text=str(row["text"]),
            meta=meta if isinstance(meta, dict) else {},
        )


# ------------------------------------------------------------------- prompts
def methodology_reference() -> str:
    """The methodology as REFERENCE the mini session may read and ignore."""
    lines = [
        (
            "THE RESEARCH METHODOLOGY (reference only — you are NOT bound by "
            "it; use it when it helps, ignore it when it does not):"
        ),
    ]
    for i, key in enumerate(PHASE_ORDER, 1):
        spec = SPECS[key]
        lines.append(
            f"{i}. {PHASE_TITLES.get(key, key)} — contract: "
            f"{', '.join(spec.output_contract) or 'none'}; "
            f"guidance: {spec.guidance.strip()[:600]}"
        )
    return "\n".join(lines)


def render_mini_prompt(
    *,
    dossier: dict[str, Any],
    thread: list[dict[str, str]],
    goal: str,
    steps: list[MiniSessionStep],
    owner_notes: list[str],
) -> str:
    """The whole record + thread + goal + what has been done so far."""
    parts = [
        (
            "THE RESEARCH RECORD (complete — the same record the report and "
            "the conversation were built from; nothing summarised):"
        ),
        json.dumps(dossier, ensure_ascii=False, default=str),
        methodology_reference(),
        "\nTHE CONVERSATION THIS MINI SESSION WAS APPROVED FROM (oldest first):",
        "\n\n".join(
            f"{'OWNER' if m.get('role') == 'owner' else 'ANALYST'}: {m.get('text')}"
            for m in thread
        ) or "(the conversation is empty)",
        f"\nMINI SESSION GOAL (from the conversation): {goal}",
    ]
    if steps:
        done = []
        for s in steps:
            line = f"[{s.kind}] {s.text}"
            if s.kind == "action":
                # The model needs the OUTCOME, not just what it asked for.
                summary = str(s.meta.get("summary") or "")
                error = str(s.meta.get("error") or "")
                if summary:
                    line += f"\n  -> {summary}"
                if error:
                    line += f"\n  -> FAILED: {error}"
            done.append(line)
        parts.append(
            "\nWHAT THIS MINI SESSION HAS DONE SO FAR (oldest first — the "
            "results above are real):\n" + "\n".join(done)
        )
    if owner_notes:
        parts.append(
            "\nNEW MESSAGES FROM THE OWNER DURING THIS MINI SESSION (they take "
            "precedence over the original goal):\n" + "\n".join(f"- {t}" for t in owner_notes)
        )
    parts.append(
        "\nNEXT STEP: reply with exactly one JSON object "
        '{"note", "actions", "done", "answer"}. '
        "Set done=true with the answer when the goal is settled."
    )
    return "\n".join(parts)


def parse_reply(raw: Any) -> tuple[str, list[dict[str, Any]], bool, str]:
    """(note, actions, done, answer) from a model reply, forgivingly."""
    data = raw if isinstance(raw, dict) else {}
    if not data:
        try:
            data = json.loads(str(raw or "{}"))
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    note = " ".join(str(data.get("note") or "").split())
    actions: list[dict[str, Any]] = []
    for item in data.get("actions") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("action") or "").strip()
        if not name:
            continue
        args = item.get("args")
        actions.append({"action": name, "args": args if isinstance(args, dict) else {}})
    answer = str(data.get("answer") or "").strip()
    done = bool(data.get("done")) or (not actions and bool(answer))
    return note[:2_000], actions, done, answer


# ------------------------------------------------------------------ the work
class MiniSessionService:
    """Starts, runs and finishes mini sessions.

    Holds the app's stores (a `state` object) plus lazy model/executor
    factories, so one service serves every report conversation. Tests inject
    `state.mini_model` / `state.mini_executor` to drive the loop deterministically.
    """

    def __init__(self, state: Any) -> None:
        self.st = state
        self.store = MiniSessionStore(state.db)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel: dict[str, bool] = {}

    # ------------------------------------------------------------- lifecycle
    def start(
        self,
        *,
        session_id: str,
        report_id: str,
        goal: str,
        title: str = "",
        proposal_message_id: int | None = None,
    ) -> MiniSession:
        goal = " ".join(str(goal or "").split())
        if not goal:
            raise ValueError("a mini session needs a goal")
        meta: dict[str, Any] = {}
        if proposal_message_id is not None:
            meta["proposal_message_id"] = int(proposal_message_id)
        mini = self.store.create(
            session_id=session_id,
            report_id=report_id,
            goal=goal,
            title=(title or goal)[:160],
            meta=meta,
        )
        self._cancel[mini.id] = False
        task = asyncio.create_task(self._run(mini.id))
        self._tasks[mini.id] = task

        def _done(_t: asyncio.Task[None]) -> None:
            self._tasks.pop(mini.id, None)
            self._cancel.pop(mini.id, None)

        task.add_done_callback(_done)
        self.st.events.append(
            session_id, level="info", actor="agent", action="mini_session_started",
            detail={"mini_id": mini.id, "report_id": report_id, "goal": goal[:300]},
        )
        return mini

    def active_for_report(self, report_id: str) -> MiniSession | None:
        return self.store.active_for_report(report_id)

    def deliver_owner_message(self, report_id: str, text: str) -> str | None:
        """Hand a message typed into the conversation to the running session.

        Returns the mini-session id it was delivered to, or None when nothing
        is running (the message is then answered by the analyst instead).
        """
        mini = self.active_for_report(report_id)
        if mini is None:
            return None
        self.store.add_step(mini.id, kind="owner", text=text)
        self.st.events.append(
            mini.session_id, level="info", actor="owner",
            action="mini_session_owner_message",
            detail={"mini_id": mini.id, "chars": len(text)},
        )
        return mini.id

    def cancel(self, mini_id: str) -> bool:
        mini = self.store.get(mini_id)
        if mini is None or mini.status not in ("queued", "running"):
            return False
        self._cancel[mini_id] = True
        return True

    def is_running(self, mini_id: str) -> bool:
        return mini_id in self._tasks

    # -------------------------------------------------------------- the loop
    async def _run(self, mini_id: str) -> None:
        store = self.store
        mini = store.get(mini_id)
        if mini is None:
            return
        session_id = mini.session_id
        store.update(mini_id, status="running")
        model = None
        owned_client = None
        try:
            from app.report_chat import ChatStore, link_citations

            chat = ChatStore(self.st.db)
            thread = [
                {"role": "owner" if m.role == "owner" else "analyst", "text": m.text}
                for m in chat.list(session_id, mini.report_id)
            ]
            report = self.st.reports.get(session_id, mini.report_id)
            if report is None:
                raise RuntimeError("the report this mini session belongs to is gone")
            dossier = self._dossier(session_id, report)
            executor = self._executor(session_id)
            model = getattr(self.st, "mini_model", None)
            if model is None:
                from app.model_client import ModelClient

                owned_client = ModelClient(
                    self.st.config, self.st.events,
                    thoughts=getattr(self.st, "thoughts", None),
                )
                model = owned_client

            timeout = float(
                getattr(self.st.config, "mini_session_timeout_seconds", 3_600) or 3_600
            )
            deadline = time.monotonic() + timeout
            seen_owner_id = 0
            while True:
                if self._cancel.get(mini_id):
                    store.update(
                        mini_id, status="cancelled", error="cancelled by the owner",
                        finished_at=iso_now(),
                    )
                    self._event(session_id, "mini_session_cancelled", mini_id)
                    return
                if time.monotonic() > deadline:
                    store.update(
                        mini_id, status="failed", finished_at=iso_now(),
                        error=f"mini session exceeded its {int(timeout)}s budget",
                    )
                    self._event(session_id, "mini_session_timed_out", mini_id)
                    return

                steps = store.steps(mini_id)
                new_owner = [
                    s.text for s in store.owner_steps(mini_id, after_id=seen_owner_id)
                ]
                if new_owner:
                    seen_owner_id = steps[-1].id if steps else seen_owner_id
                prompt = render_mini_prompt(
                    dossier=dossier, thread=thread, goal=mini.goal,
                    steps=steps, owner_notes=new_owner,
                )
                reply = await model.call(
                    session_id,
                    system=MINI_SYSTEM,
                    user=prompt,
                    label=f"Mini session: {mini.title[:90]}",
                    max_tokens=int(
                        getattr(self.st.config, "mini_session_max_tokens", 4_000) or 4_000
                    ),
                )
                note, actions, done, answer = parse_reply(
                    getattr(reply, "content", None) or getattr(reply, "raw", None)
                )
                if note:
                    store.add_step(mini_id, kind="note", text=note)
                if done:
                    result_text = answer or note or "The mini session finished without an answer."
                    known = {
                        row.get("id") for row in (dossier.get("evidence") or [])
                        if isinstance(row, dict)
                    }
                    linked, cited, dropped = link_citations(result_text, session_id, known)
                    store.add_step(
                        mini_id, kind="answer", text=linked,
                        meta={"citations": cited, "dropped_citations": dropped},
                    )
                    finish = store.get(mini_id)
                    store.update(
                        mini_id,
                        status="completed",
                        result=linked,
                        finished_at=iso_now(),
                        error=None,
                        meta=json.dumps({
                            **(finish.meta if finish else {}),
                            "citations": cited,
                            "dropped_citations": dropped,
                        }),
                    )
                    chat.add(
                        session_id, mini.report_id, role="analyst", text=linked,
                        meta={
                            "mini_session_id": mini_id,
                            "mini_goal": mini.goal,
                            "citations": cited,
                            "dropped_citations": dropped,
                        },
                    )
                    self._event(
                        session_id, "mini_session_finished", mini_id,
                        {"chars": len(linked), "citations": len(cited)},
                    )
                    return
                if not actions:
                    # Nothing run and nothing answered: ask once more with the
                    # record of what it said, so a lost reply is not a lost job.
                    store.add_step(
                        mini_id, kind="result",
                        text="no actions were requested and no answer was given",
                    )
                    continue
                for item in actions:
                    outcome = await executor.execute(
                        session_id=session_id,
                        action=str(item["action"]),
                        args=dict(item.get("args") or {}),
                    )
                    summary = str(getattr(outcome, "summary", "") or "")
                    error = str(getattr(outcome, "error", "") or "")
                    store.add_step(
                        mini_id,
                        kind="action",
                        text=f"{item['action']} {json.dumps(item['args'], ensure_ascii=False, default=str)[:300]}",
                        meta={
                            "action": item["action"],
                            "ok": bool(getattr(outcome, "ok", False)),
                            "summary": summary[:2_000],
                            "error": error[:2_000],
                            "evidence_ids": list(getattr(outcome, "evidence_ids", []) or []),
                        },
                    )
            # The loop returns on every terminal path; this line is only
            # reached if a future edit removes one, and then the run fails
            # loudly instead of silently hanging.
            raise RuntimeError("the mini-session loop exited without a verdict")
        except asyncio.CancelledError:
            store.update(
                mini_id, status="interrupted", finished_at=iso_now(),
                error="the app stopped while this mini session was running",
            )
            raise
        except Exception as exc:
            store.update(
                mini_id, status="failed", finished_at=iso_now(),
                error=f"{type(exc).__name__}: {exc}"[:1_000],
            )
            self._event(
                session_id, "mini_session_failed", mini_id,
                {"error": f"{type(exc).__name__}: {exc}"[:300]},
            )
        finally:
            if owned_client is not None:
                try:
                    await owned_client.aclose()
                except Exception:
                    pass

    # --------------------------------------------------------------- helpers
    def _event(self, session_id: str, action: str, mini_id: str, detail: dict | None = None) -> None:
        try:
            self.st.events.append(
                session_id, level="info", actor="agent", action=action,
                detail={"mini_id": mini_id, **(detail or {})},
            )
        except Exception:
            pass

    def _dossier(self, session_id: str, report: Any) -> dict[str, Any]:
        """The exact dossier a chat answer gets — nothing summarised."""
        from app.report_chat import chat_dossier

        return chat_dossier(self.st, session_id, report)

    def _executor(self, session_id: str) -> Any:
        injected = getattr(self.st, "mini_executor", None)
        if injected is not None:
            return injected
        # One executor per mini session: a shared one would share its working
        # page, and two mini sessions must never navigate each other's browser.
        from app.actions import ActionExecutor
        from app.evidence_capture import EvidenceCollector
        from app.marketplace import resolve_plan, select_auto
        from app.research_runner import _RunnerAuth

        executor = ActionExecutor(
            self.st.config,
            self.st.browser_manager,
            EvidenceCollector(
                self.st.browser_manager,
                self.st.browser_evidence,
                self.st.artifacts,
                live_view=getattr(self.st, "live_view", None),
                live_view_session_provider=(
                    (lambda: session_id) if getattr(self.st, "live_view", None) else None
                ),
            ),
            _RunnerAuth(
                self.st.config, self.st.browser_manager,
                self.st.browser_auth, self.st.login_windows,
            ),
            self.st.events,
        )
        session = self.st.sessions.require(session_id)
        if session.marketplaces:
            _mode, marketplaces = resolve_plan(session.marketplaces)
        else:
            marketplaces = select_auto(session.prompt or "", session.objective or "")
        executor.set_marketplaces(marketplaces)
        return executor
