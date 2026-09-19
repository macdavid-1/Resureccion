"""Model-generated session names.

A research session's name should tell the owner WHAT was researched at a
glance ("Sudden-loss grief journals — US/UK sweep"), never "Research
Session 14". At session start the runner asks the model for a 3-6 word
title derived from the brief (or the first autonomous discoveries).

Design:
- Best-effort and strictly bounded: a naming failure NEVER affects research;
  the deterministic fallback name stands.
- The result is validated (length, no control characters, not generic) and
  persisted via SessionStore.update(name=...).
- Runs once per session, in the background, after the marketplace plan exists
  so auto-discovery sessions can already name themselves from their seed plan.
"""
from __future__ import annotations

import re
from typing import Any

from app.events import EventLog
from app.model_client import ModelClient
from app.sessions import SessionStore

_BAD_NAMES = re.compile(r"\b(research session|untitled|session \d+|new session)\b", re.IGNORECASE)


def _valid_name(raw: str) -> str | None:
    name = " ".join(str(raw or "").split())
    if not (6 <= len(name) <= 70):
        return None
    if _BAD_NAMES.search(name):
        return None
    if any(ord(c) < 32 for c in name):
        return None
    return name


def fallback_name(mode: str, prompt: str, objective: str) -> str:
    """Deterministic fallback (never generic when there is any signal)."""
    text = " ".join((objective or prompt or "").split())
    if text:
        words = text[:60].rsplit(" ", 1)[0] if len(text) > 60 else text
        return words
    return "Autonomous Market Sweep" if mode == "auto" else "Research Session"


async def generate_session_name(
    session_id: str,
    *,
    sessions: SessionStore,
    model: ModelClient,
    events: EventLog,
) -> str:
    """Ask the model for a meaningful session name; persist and return it.

    Never raises: any problem keeps the existing name.
    """
    session = sessions.get(session_id)
    if session is None:
        return ""
    brief = session.prompt or session.objective or ""
    system = (
        "You name KDP market-research sessions for a professional researcher. "
        "Reply with JSON only: {\"name\": \"...\"}. The name is 3-7 words, "
        "title-style, specific (topic + audience or angle), never generic, "
        "no quotes, no trailing period."
    )
    user = (
        f"Research mode: {session.mode}\n"
        f"Objective: {session.objective or '—'}\n"
        f"Brief: {brief[:600] or '(autonomous discovery — no topic given)'}\n"
        f"Marketplaces: {', '.join(session.marketplaces) or 'auto-selected'}\n"
        "Generate the session name."
    )
    try:
        reply = await model.call(
            session_id, system=system, user=user, max_tokens=64,
            temperature=0.4, max_retries=1,
        )
        name = _valid_name(str((reply.content or {}).get("name") or ""))
    except Exception:
        name = None
    if not name:
        events.append(
            session_id, level="info", actor="system",
            action="session_name_fallback", detail={},
        )
        return session.name
    sessions.update(session_id, name=name)
    events.append(
        session_id, level="info", actor="system",
        action="session_named", detail={"name": name},
    )
    return name
