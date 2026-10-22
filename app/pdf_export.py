"""PDF export: the final report as a premium 6×9in PDF via PDFKit.

The structured report model (app.report_model) is transformed into
designed blocks; the Node sidecar (scripts/render_pdf.js, PDFKit) renders
them into a 6×9 inch PDF. The TEXTUAL report content is preserved word for
word — the exporter only re-shapes the existing model into blocks; it never
summarizes, rewrites or drops report text.

Durable design:
- Generation writes to a temp file in the session's artifact directory and
  is atomically moved into place only on success — an interrupted export
  never leaves a broken artifact registered.
- The rendered report HTML (the designed web document) is persisted as an
  artifact too, so the report can be shared/opened independently.
- Node availability is probed once per process; if the sidecar is missing
  the export raises PdfExportError with a clear reason (never a silent fail).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from app.artifacts import ArtifactStore
from app.config import Config
from app.db import Database, atomic_write_text
from app.timeutil import iso_now

_SIDECAR = Path(__file__).resolve().parent.parent / "scripts" / "render_pdf.js"
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
LOGO_FILE = _STATIC_DIR / "logo.png"
_node_ok: bool | None = None  # probe once per process


def logo_file() -> Path | None:
    """The installed app logo (static/logo.png), or None when it is absent.

    The logo is a supplied asset, never generated here — both report covers
    (the PDF sidecar and the standalone report HTML) render exactly these
    bytes, and skip the mark cleanly when the file is not deployed.
    """
    return LOGO_FILE if LOGO_FILE.is_file() else None


class PdfExportError(Exception):
    pass


# Research screenshots embedded in the PDF. Capped deliberately: each is a
# full-width image on a 6x9 page, so an uncapped appendix would turn a
# readable report into a screenshot dump.
MAX_REPORT_FIGURES = 12

# PDFKit can only decode these; anything else makes the renderer fall back
# to a placeholder line.
_FIGURE_SUFFIXES = {".png", ".jpg", ".jpeg"}


def node_available() -> bool:
    global _node_ok
    if _node_ok is None:
        _node_ok = shutil.which("node") is not None and _SIDECAR.is_file()
    return _node_ok


def _resolve_figures(
    session_id: str,
    model: dict[str, Any],
    artifacts: ArtifactStore,
    *,
    limit: int = MAX_REPORT_FIGURES,
) -> list[dict[str, str]]:
    """Resolve the report's evidence screenshots to embeddable file paths.

    Walks the report's own evidence appendix (not every row in the database)
    so a screenshot only appears when the evidence it documents actually
    reached the report. Each path is checked to exist, to be a decodable
    image, and to live inside the artifact root — the renderer reads whatever
    path it is handed, so containment is verified here rather than trusted.

    A missing or pruned artifact is skipped: losing a figure must never abort
    an export.
    """
    appendix = model.get("evidence_appendix") or []
    if not isinstance(appendix, list) or not appendix:
        return []
    try:
        root = artifacts.config.artifacts_dir.resolve()
    except Exception:
        return []

    figures: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in appendix:
        if len(figures) >= limit:
            break
        artifact_id = row.get("screenshot_artifact_id") if isinstance(row, dict) else None
        if not artifact_id or artifact_id in seen:
            continue
        seen.add(artifact_id)
        try:
            art = artifacts.get(session_id, artifact_id)
        except Exception:
            continue
        if art is None or not art.path:
            continue
        path = Path(art.path)
        if path.suffix.lower() not in _FIGURE_SUFFIXES:
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(root)  # raises when the path escapes the root
        except Exception:
            continue
        if not resolved.is_file():
            continue
        label = str(row.get("title") or row.get("url") or "").strip()
        kind = str(row.get("kind") or "capture").replace("_", " ")
        market = str(row.get("marketplace") or "")
        caption = " · ".join(
            x for x in (market.upper() if market else "", kind, label[:90]) if x
        )
        figures.append({
            "artifact_id": artifact_id,
            "path": str(resolved),
            "caption": caption or "Research capture",
        })
    return figures


# --------------------------------------------------------------------- blocks
def model_to_blocks(model: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    """Transform the structured report model into designed PDF blocks.

    Returns (title, subtitle, blocks). Content is transferred verbatim from
    the model into blocks — string values are never altered.
    """
    ex = model.get("executive_summary", {}) or {}
    s = model.get("session", {}) or {}
    name = str(s.get("name") or "Market Intelligence Report")
    title = "Market Intelligence Report"
    subtitle = name
    mkt = ", ".join(str(m) for m in (s.get("marketplaces_requested") or [])) or "auto-selected"
    footer = f"Resurrección  ·  {name}  ·  {mkt}"

    blocks: list[dict[str, Any]] = []

    def h1(x: str) -> None:
        if x:
            blocks.append({"t": "h1", "x": x})

    def h2(x: str) -> None:
        if x:
            blocks.append({"t": "h2", "x": x})

    def h3(x: str) -> None:
        if x:
            blocks.append({"t": "h3", "x": x})

    def p(x: Any) -> None:
        if x is not None and str(x).strip():
            blocks.append({"t": "p", "x": str(x)})

    def li(x: Any) -> None:
        if x is not None and str(x).strip():
            blocks.append({"t": "li", "x": str(x)})

    def quote(x: Any) -> None:
        if x is not None and str(x).strip():
            blocks.append({"t": "quote", "x": str(x)})

    def table(head: list[str], rows: list[list[Any]]) -> None:
        if rows:
            blocks.append({"t": "table", "head": head, "rows": rows})

    # ---------------------------------------------------- executive summary
    h2("Executive Summary")
    p(f"What was researched: {ex.get('what_was_researched', '—')}")
    p(f"Why: {ex.get('why', '—')}")
    p(
        f"Scale: {ex.get('candidates_considered', 0)} candidates considered · "
        f"{ex.get('candidates_rejected', 0)} rejected · "
        f"{ex.get('survived_verification', 0)} survived verification."
    )
    p(f"Methodology: {ex.get('methodology', '—')}")
    p(f"Marketplaces investigated: {', '.join(str(m) for m in ex.get('marketplaces_investigated', [])) or '—'}")
    if ex.get("major_findings"):
        for f in ex["major_findings"]:
            li(f)
    v = model.get("validation", {}) or {}
    mode = str(v.get("mode", "final")).upper()
    blocks.insert(0, {"t": "chip", "x": f"Report status: {mode}"})
    if not v.get("passed", True) and v.get("failures"):
        quote("Adversarial validation findings: " + "; ".join(str(f) for f in v["failures"][:8]))

    # ------------------------------------------------------ opportunities
    opps = model.get("opportunities", []) or []
    h2("Opportunity Portfolio")
    if not opps:
        p("No opportunities passed adversarial verification in this session. "
          "See the candidate ledger and quality statement.")
    for i, o in enumerate(opps, 1):
        h3(f"{i}. {o.get('name', 'Opportunity')}")
        vr = o.get("verification", {}) or {}
        conf = int(round(float(o.get("confidence", 0)) * 100))
        rows = [
            ["Precise niche", o.get("niche", "—")],
            ["Parent market", o.get("parent_market", "—")],
            ["Target reader", o.get("target_reader", "—")],
            ["Reader problem / desire", o.get("reader_problem", "—")],
            ["Marketplaces", ", ".join(o.get("marketplaces") or []) or "—"],
            ["Market gap", o.get("market_gap", "—")],
            ["Differentiation", o.get("differentiation", "—")],
            ["Positioning", o.get("positioning") or "—"],
            ["Risks", "; ".join(str(r) for r in (o.get("risks") or [])) or "—"],
            ["Verification status", vr.get("status", "—")],
            ["Confidence", f"{conf}%"],
            ["Recommended next step", o.get("next_step", "—")],
        ]
        table(["Field", "Value"], rows)

        chain = o.get("niching_chain") or []
        if chain:
            p("Niching hierarchy: " + " → ".join(str(c) for c in chain))
        d = o.get("demand") or {}
        p(f"Demand: {d.get('verdict', '—')}"
          + (f" — {'; '.join(str(x) for x in d.get('signals', []))}" if d.get("signals") else ""))
        m = o.get("marketplace_analysis") or {}
        if m:
            p(f"Marketplace analysis: observed on {', '.join(m.get('observed', [])) or '—'}; "
              f"strongest: {', '.join(m.get('strongest', [])) or '—'}; {m.get('scope', '')}")
            for diff in (m.get("differences") or [])[:4]:
                li(diff)
        if o.get("competitive_landscape"):
            p(f"Competitive landscape: {o['competitive_landscape']}")
        if o.get("consumer_needs"):
            p(f"Consumer needs: {o['consumer_needs']}")
        for ang in (o.get("angles") or [])[:5]:
            li(f"Angle — problem: {ang.get('reader_problem', '—')} | "
               f"angle: {ang.get('angle', '—')} | gap: {ang.get('addresses_gap', '—')}")
        for t in (o.get("titles") or [])[:6]:
            full = t.get("title", "")
            if t.get("subtitle"):
                full += f": {t['subtitle']}"
            li(f"Title concept: {full} — {t.get('rationale', '')}")
        k = o.get("keywords") or {}
        for label, key in (
            ("Core", "core"), ("Supporting", "supporting"), ("Long-tail", "long_tail"),
            ("Problem-based", "problem_based"), ("Audience-specific", "audience_specific"),
            ("Intent phrases", "intent_phrases"),
        ):
            items = k.get(key) or []
            if items:
                li(f"{label} keywords: {', '.join(str(x) for x in items)}")
        if k.get("relevance_rationale"):
            p(f"Why these keywords: {k['relevance_rationale']}")
        risk = o.get("kdp_risk") or {}
        if risk.get("warnings"):
            p(f"KDP risk ({risk.get('level', '—')}): "
              + "; ".join(str(w) for w in risk["warnings"]))
        else:
            p(f"KDP risk ({risk.get('level', 'safe')}): no policy flags raised by deterministic screening")
        if vr.get("limitations"):
            p(f"Verification limitations: {'; '.join(vr['limitations'])}")
        ev = o.get("evidence_ids") or []
        if ev:
            p(f"Evidence trail: {', '.join(str(e) for e in ev[:12])}")

    # ------------------------------------------------- consumer intelligence
    ci = model.get("consumer_intelligence", {}) or {}
    themes = ci.get("themes", {}) or {}
    if ci.get("by_niche"):
        h2("Consumer Intelligence — What Readers Actually Say")
        for label, key in (
            ("Recurring praise", "praised"),
            ("Recurring complaints", "complaints"),
            ("Unmet needs", "unmet_needs"),
            ("Desired features", "desired_features"),
            ("Missing information", "missing_information"),
            ("Frustrations", "frustrations"),
        ):
            items = themes.get(key, [])
            if items:
                h3(label)
                for it in items[:10]:
                    li(it)
        diff = themes.get("what_the_new_book_should_do_differently", [])
        if diff:
            h3("What the new book should do differently")
            for it in diff:
                li(it)
        table(
            ["Niche", "Praise", "Complaints", "Unmet needs"],
            [
                [
                    m.get("niche", ""),
                    "; ".join(m.get("praises", [])[:3]) or "—",
                    "; ".join(m.get("complaints", [])[:3]) or "—",
                    "; ".join(m.get("unmet", [])[:3]) or "—",
                ]
                for m in ci["by_niche"][:20]
            ],
        )

    # --------------------------------------------------------- competitors
    # Screenshots travel WITH the claim they document rather than being dumped
    # in a gallery at the end: each competitor's capture sits directly under its
    # own metrics, so "0 reviews · BSR 6,709" is readable next to the image that
    # proves it. One shared budget spans the whole report, so a session with a
    # hundred captures still ships a readable report, not a screenshot album.
    figures: list[dict[str, Any]] = list(model.get("figures", []) or [])
    fig_by_id = {f.get("artifact_id"): f for f in figures if f.get("artifact_id")}
    placed: set[str] = set()
    _budget = {"left": MAX_REPORT_FIGURES}

    def place(artifact_id: Any, caption: str) -> None:
        """Emit one figure inline if the budget allows. Idempotent per artifact."""
        if not artifact_id or _budget["left"] <= 0 or artifact_id in placed:
            return
        fig = fig_by_id.get(artifact_id)
        if not fig:
            return
        placed.add(artifact_id)
        _budget["left"] -= 1
        blocks.append({"t": "img", "path": fig["path"], "caption": caption})

    def competitor_caption(c: dict[str, Any]) -> str:
        """Caption states the claim the screenshot is evidence for."""
        bits = []
        if c.get("marketplace"):
            bits.append(str(c["marketplace"]).upper())
        if c.get("review_count") is not None:
            bits.append(f"{c['review_count']} reviews")
        if c.get("rank") is not None:
            bits.append(f"BSR {c['rank']}")
        if c.get("asin"):
            bits.append(str(c["asin"]))
        return " · ".join(bits) or str(c.get("title") or "Research capture")

    comps = model.get("competitors", []) or []
    if comps:
        h2("Competitor Analysis")
        p("Screenshots extract positioning lessons — never to copy. Each entry "
          "notes where the proposed book can be materially stronger.")
        for c in comps[:30]:
            h3(str(c.get("title", "Competitor")))
            bits = []
            if c.get("marketplace"):
                bits.append(f"Marketplace: {c['marketplace']}")
            if c.get("asin"):
                bits.append(f"ASIN: {c['asin']}")
            if c.get("price") is not None:
                bits.append(f"Price: {c['price']}")
            if c.get("rank") is not None:
                bits.append(f"Rank: {c['rank']}")
            if c.get("review_count") is not None:
                bits.append(f"Reviews: {c['review_count']}")
            if c.get("saturation"):
                bits.append(f"Saturation: {c['saturation']}")
            if bits:
                p(" · ".join(str(b) for b in bits))
            # The capture lands right under the numbers it substantiates.
            place(c.get("screenshot_artifact_id"), competitor_caption(c))
            if c.get("url"):
                p(str(c["url"]))
            if c.get("positioning"):
                p(f"Positioning / patterns: {c['positioning']}")
            p(f"How to outperform it legitimately: {c.get('lesson', '—')}")

    # -------------------------------------------------------------- ledger
    ledger = model.get("candidate_ledger", []) or []
    if ledger:
        h2("Candidate Ledger")
        table(
            ["Niche", "Status", "Marketplace", "Rationale"],
            [
                [c.get("niche", ""), c.get("status", ""),
                 c.get("marketplace") or "—", (c.get("rationale") or "")[:200]]
                for c in ledger
            ],
        )

    # --------------------------------------------------------- methodology
    trace = model.get("methodology_trace", []) or []
    if trace:
        h2("Methodology Trace")
        for t in trace:
            extra = f" ({', '.join(t['outputs'])})" if t.get("outputs") else ""
            li(f"{t.get('title', t.get('phase', ''))} — completed{extra}")

    # ------------------------------------------------------------- quality
    q = model.get("quality_statement", {}) or {}
    h2("Research Quality Statement")
    p(f"What was investigated: {q.get('investigated', '—')}")
    p(f"What was verified: {q.get('verified', 0)} opportunities carry passing adversarial verification verdicts.")
    p(f"What was rejected: {q.get('rejected', 0)} candidates were rejected by deterministic filters, "
      "the verification engine, or KDP risk screening.")
    for r in (q.get("rejection_reasons") or [])[:8]:
        li(f"{r.get('niche', '')}: {r.get('rationale', '')}")
    if q.get("remains_uncertain"):
        h3("What remains uncertain")
        for u in q["remains_uncertain"]:
            li(u)
    if q.get("verification_limitations"):
        h3("Recorded verification limitations")
        for l in q["verification_limitations"]:
            li(l)
    p(f"Certainty policy: {q.get('certainty_policy', '—')}")

    # ------------------------------------------------------------ appendix
    appendix = model.get("evidence_appendix", []) or []
    if appendix:
        h2("Evidence Appendix")
        p("Traceability for key conclusions: source, marketplace, timestamp, "
          "evidence type, observation and screenshot reference.")
        table(
            ["Evidence id", "Type", "Marketplace", "Source / product", "Captured"],
            [
                [str(e.get("id", ""))[:16], e.get("kind", ""),
                 e.get("marketplace") or "—",
                 (e.get("title") or e.get("url") or "—")[:80],
                 str(e.get("captured_at", ""))[:19]]
                for e in appendix
            ],
        )

    # ------------------------------------------- remaining research captures
    # Only evidence that did NOT already appear next to a competitor, and only
    # within whatever budget the inline placement left. A report whose captures
    # all found an inline home gets no trailing section at all.
    leftovers = [
        f for f in figures
        if isinstance(f, dict) and f.get("path")
        and f.get("artifact_id") not in placed and _budget["left"] > 0
    ][: _budget["left"]]
    if leftovers:
        h2("Research Screenshots")
        p("Captured live by the research browser during this session — the "
          "visual ground truth behind the evidence table above.")
        for fig in leftovers:
            placed.add(fig.get("artifact_id"))
            _budget["left"] -= 1
            blocks.append({
                "t": "img",
                "path": str(fig["path"]),
                "caption": str(fig.get("caption") or "Research capture"),
            })

    blocks.append({"t": "hr"})
    p("Generated by Resurrección from durable, evidence-linked research state. "
      "Every opportunity above passed the deterministic methodology, the "
      "adversarial verification engine, and the research quality gates.")
    return title, subtitle, blocks


# --------------------------------------------------------------------- html
_CSS = """
@page { margin: 0; }
* { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { margin: 0; background: #e8e9ee; font-family: Georgia, 'Times New Roman', serif; color: #111318; }
.sheet { width: 6in; min-height: 9in; margin: 0 auto; background: #fff; padding: 0.72in 0.72in 0.9in;
         box-shadow: 0 2px 14px rgba(0,0,0,.18); }
.cover { background: #000; color: #f2f4fa; margin: 0; padding: 2.6in 0.72in 0.72in; }
.cover .cover-logo { display: block; width: 0.62in; height: 0.62in; border-radius: 0.13in;
                     border: 1px solid #1c2340; margin: 0 0 0.2in; }
.cover .brand { font-family: Helvetica, Arial, sans-serif; font-size: 7.5pt; letter-spacing: .35em; color: #5d6890; }
.cover h1 { font-family: Helvetica, Arial, sans-serif; font-size: 19pt; margin: 0.3in 0 0; font-weight: 700; }
.cover .sub { font-style: italic; color: #aab3d0; font-size: 10pt; margin-top: 10px; }
.cover .bar { height: 3px; background: #1207DA; width: 100%; margin: 0.28in 0; }
.cover .meta { font-family: Helvetica, Arial, sans-serif; font-size: 7pt; color: #5d6890; }
h2 { font-family: Helvetica, Arial, sans-serif; font-size: 9.5pt; letter-spacing: .08em; text-transform: uppercase;
     color: #1207DA; border-bottom: 1px solid #1207DA; padding-bottom: 4px; margin: 22px 0 10px; }
h3 { font-family: Helvetica, Arial, sans-serif; font-size: 10.5pt; margin: 16px 0 6px; }
p { font-size: 9.8pt; line-height: 1.55; margin: 7px 0; text-align: justify; }
ul { margin: 6px 0 10px 18px; padding: 0; }
li { font-size: 9.6pt; line-height: 1.5; margin: 3.5px 0; }
blockquote { margin: 10px 0; padding: 6px 12px; border-left: 3px solid #1207DA; color: #4b5162; font-style: italic; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 7.8pt; }
th { font-family: Helvetica, Arial, sans-serif; font-size: 7.2pt; text-transform: uppercase; letter-spacing: .05em;
     text-align: left; background: #f0f1fa; color: #4b5162; padding: 5px 6px; border-bottom: 1.5px solid #1207DA; }
td { padding: 5px 6px; border-bottom: .5px solid #d8dce8; vertical-align: top; }
.chip { display: inline-block; font-family: Helvetica, Arial, sans-serif; font-size: 7.5pt; font-weight: 700;
        color: #1207DA; border: 1px solid #1207DA; border-radius: 8px; padding: 2px 10px; margin: 10px 0; }
hr { border: none; border-top: .6px solid #d8dce8; margin: 16px 0; }
img { max-width: 100%; }
.fig { margin: 12px 0; }
.fig img { display: block; width: 100%; border: .5px solid #d8dce8; }
.fig figcaption { font-family: Helvetica, Arial, sans-serif; font-size: 7.2pt; color: #4b5162;
                  letter-spacing: .03em; margin-top: 4px; }
.small { font-size: 7.5pt; color: #4b5162; }
"""


def render_report_html(model: dict[str, Any], blocks: list[dict[str, Any]]) -> str:
    """Render the same blocks into the designed standalone report HTML.

    Print-ready (6×9in sheet) and screen-friendly; same content, no markdown
    left in the output.
    """
    import re

    def esc(x: Any) -> str:
        s = str(x if x is not None else "")
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        return s

    title, subtitle, _ = model_to_blocks(model)
    s = model.get("session", {}) or {}
    cover_logo = ""
    if logo_file() is not None:
        # Served from the same origin as this artifact, content-hash versioned
        # exactly like the app shell so the cover never shows a stale mark.
        from app.static_cache import asset_version

        cover_logo = (
            '<img class="cover-logo" src="/static/logo.png?v='
            f'{asset_version(_STATIC_DIR, "logo.png")}" alt="Resurrección">'
        )
    body: list[str] = [
        '<section class="cover">',
        cover_logo,
        '<div class="brand">R E S U R R E C C I Ó N</div>',
        '<div class="bar"></div>',
        f"<h1>{esc(title)}</h1>",
        f'<div class="sub">{esc(subtitle)}</div>',
        f'<div class="meta" style="margin-top:0.35in">{esc(s.get("name", ""))} · '
        f"{esc(', '.join(str(m) for m in (s.get('marketplaces_requested') or [])) or 'auto-selected')}</div>",
        "</section>",
        '<section class="sheet">',
    ]
    for b in blocks:
        t = b.get("t")
        if t == "h1":
            body.append(f"<h1>{esc(b.get('x', ''))}</h1>")
        elif t == "h2":
            body.append(f"<h2>{esc(b.get('x', ''))}</h2>")
        elif t == "h3":
            body.append(f"<h3>{esc(b.get('x', ''))}</h3>")
        elif t == "p":
            body.append(f"<p>{esc(b.get('x', ''))}</p>")
        elif t in ("li", "oli"):
            body.append(f"<li>{esc(b.get('x', ''))}</li>")
        elif t == "quote":
            body.append(f"<blockquote>{esc(b.get('x', ''))}</blockquote>")
        elif t == "hr":
            body.append("<hr>")
        elif t == "chip":
            body.append(f'<span class="chip">{esc(b.get("x", ""))}</span>')
        elif t == "img":
            # Same figure the PDF shows — without this the HTML artifact
            # would silently drop every screenshot from the report.
            src = str(b.get("path") or "")
            if src:
                body.append(
                    f'<figure class="fig"><img src="{esc(src)}" alt="{esc(b.get("caption", ""))}">'
                    f'<figcaption>{esc(b.get("caption", ""))}</figcaption></figure>'
                )
        elif t == "table":
            rows = b.get("rows", [])
            head = b.get("head", [])
            th = "".join(f"<th>{esc(h)}</th>" for h in head)
            trs = "".join(
                "<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in rows
            )
            body.append(f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>")
    body.append("</section>")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)} — Resurrección</title>"
        f"<style>{_CSS}</style></head><body>{''.join(body)}</body></html>"
    )


# -------------------------------------------------------------------- export
def _render_to_tmp(job: dict[str, Any]) -> Path:
    """Run the Node sidecar on a render job; returns the validated temp PDF.

    On any failure the temp file is removed and PdfExportError is raised —
    a broken render never leaves a stray file behind.
    """
    if not node_available():
        raise PdfExportError(
            "PDF export unavailable: node or scripts/render_pdf.js is missing"
        )
    pdf_tmp = Path(job["out"])
    try:
        proc = subprocess.run(
            ["node", str(_SIDECAR)],
            input=json.dumps(job, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            # Combined multi-report PDFs are larger than single ones.
            timeout=300,
        )
        if proc.returncode != 0:
            raise PdfExportError(
                f"PDF renderer failed (exit {proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'replace')[:300]}"
            )
        if not pdf_tmp.is_file() or pdf_tmp.stat().st_size < 500:
            raise PdfExportError("PDF renderer produced no usable output")
    except PdfExportError:
        pdf_tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        pdf_tmp.unlink(missing_ok=True)
        raise PdfExportError(f"PDF export failed: {exc}") from exc
    return pdf_tmp


def export_pdf(
    db: Database,
    config: Config,
    session_id: str,
    *,
    model: dict[str, Any],
    artifacts: ArtifactStore,
) -> dict[str, Any]:
    """Generate the 6×9in PDF + report HTML artifacts for a session's report.

    Returns {"pdf": Artifact, "html": Artifact, "bytes": int, "pages_hint": bool}.
    Raises PdfExportError with a clear reason on any failure; on failure no
    artifact is registered (temp files are cleaned up).
    """
    if not node_available():
        raise PdfExportError(
            "PDF export unavailable: node or scripts/render_pdf.js is missing"
        )
    # Resolve the report's evidence screenshots to real files BEFORE shaping
    # blocks, so the sidecar receives plain paths and never touches the DB.
    model = {**model, "figures": _resolve_figures(session_id, model, artifacts)}
    title, subtitle, blocks = model_to_blocks(model)
    out_dir = artifacts.session_dir(session_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = iso_now().replace(":", "").replace("-", "")[:15]
    pdf_tmp = out_dir / f".pdf-{uuid.uuid4().hex[:8]}.tmp"
    job = {
        "out": str(pdf_tmp),
        "title": title,
        "subtitle": subtitle,
        "footer": f"Resurrección  ·  {subtitle}  ·  {stamp}",
        "blocks": blocks,
        "logo": str(logo) if (logo := logo_file()) else None,
    }
    pdf_tmp = _render_to_tmp(job)
    try:
        # The short uuid keeps same-second exports from overwriting each
        # other (separate mode renders several PDFs back to back).
        final_pdf = out_dir / f"report-{stamp}-{uuid.uuid4().hex[:6]}.pdf"
        os.replace(pdf_tmp, final_pdf)  # atomic: interrupted export leaves no artifact
        pdf_art = artifacts.register_external_file(
            session_id, kind="pdf", path=final_pdf,
            meta={"generator": "pdfkit", "page_size": "6x9in",
                  "report_session": session_id, "blocks": len(blocks)},
        )
    except Exception as exc:
        pdf_tmp.unlink(missing_ok=True)
        raise PdfExportError(f"PDF export failed: {exc}") from exc

    # Persist the designed report HTML as its own durable artifact.
    html_id = None
    try:
        html = render_report_html(model, blocks)
        html_art = artifacts.save_text(
            session_id, kind="page_snapshot",
            filename=f"report-{stamp}.html", text=html,
            meta={"generator": "pdf_export", "role": "report_html"},
        )
        html_id = html_art.id
    except Exception:
        pass  # PDF is the primary deliverable; HTML is additive

    return {
        "pdf": pdf_art.to_dict(),
        "html_artifact_id": html_id,
        "bytes": final_pdf.stat().st_size,
    }


def export_reports(
    db: Database,
    config: Config,
    session_id: str,
    *,
    models: list[dict[str, Any]],
    artifacts: ArtifactStore,
    separate: bool = False,
) -> dict[str, Any]:
    """Export one or more of a session's reports as designed 6×9in PDFs.

    Default: every selected report lands in ONE combined PDF, each report
    introduced by its own section page (page break + divider heading). The
    owner opts into separate=True for one PDF artifact per report.

    Returns {"separate": bool, "pdfs": [per-report export result], "bytes": int}.
    """
    if not models:
        raise PdfExportError("no reports to export")
    if separate:
        pdfs = [
            export_pdf(db, config, session_id, model=m, artifacts=artifacts)
            for m in models
        ]
        return {
            "separate": True,
            "pdfs": pdfs,
            "bytes": sum(int(p.get("bytes") or 0) for p in pdfs),
        }

    # --- combined: one PDF, one section divider per report ------------------
    stamp = iso_now().replace(":", "").replace("-", "")[:15]
    blocks: list[dict[str, Any]] = []
    labels: list[str] = []
    first_resolved: dict[str, Any] = {}
    for i, m in enumerate(models):
        resolved = {**m, "figures": _resolve_figures(session_id, m, artifacts)}
        _, session_name, r_blocks = model_to_blocks(resolved)
        label = session_name or f"Report {i + 1}"
        labels.append(label)
        if not first_resolved:
            first_resolved = resolved
        if i:
            blocks.append({"t": "pagebreak"})
        blocks.append({"t": "section", "x": label})
        blocks.extend(r_blocks)
    title = "Market Intelligence Reports" if len(models) > 1 else "Market Intelligence Report"
    subtitle = labels[0] if len(labels) == 1 else f"{len(models)} reports combined"
    out_dir = artifacts.session_dir(session_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "out": str(out_dir / f".pdf-{uuid.uuid4().hex[:8]}.tmp"),
        "title": title,
        "subtitle": subtitle,
        "footer": f"Resurrección  ·  {subtitle}  ·  {stamp}",
        "blocks": blocks,
        "logo": str(logo) if (logo := logo_file()) else None,
    }
    pdf_tmp = _render_to_tmp(job)
    try:
        final_pdf = out_dir / f"report-combined-{stamp}-{uuid.uuid4().hex[:6]}.pdf"
        os.replace(pdf_tmp, final_pdf)  # atomic: interrupted export leaves no artifact
        pdf_art = artifacts.register_external_file(
            session_id, kind="pdf", path=final_pdf,
            meta={"generator": "pdfkit", "page_size": "6x9in",
                  "report_session": session_id, "blocks": len(blocks),
                  "combined_reports": len(models), "report_labels": labels[:10]},
        )
    except Exception as exc:
        pdf_tmp.unlink(missing_ok=True)
        raise PdfExportError(f"PDF export failed: {exc}") from exc
    # The designed HTML mirror of the combined document (additive).
    html_id = None
    try:
        html = render_report_html(first_resolved, blocks)
        html_art = artifacts.save_text(
            session_id, kind="page_snapshot",
            filename=f"report-combined-{stamp}.html", text=html,
            meta={"generator": "pdf_export", "role": "report_html",
                  "combined_reports": len(models)},
        )
        html_id = html_art.id
    except Exception:
        pass  # PDF is the primary deliverable; HTML is additive
    return {
        "separate": False,
        "pdfs": [{
            "pdf": pdf_art.to_dict(),
            "html_artifact_id": html_id,
            "bytes": final_pdf.stat().st_size,
        }],
        "bytes": final_pdf.stat().st_size,
    }
