"""BSR for a whole search page, WITHOUT the add-on.

Amazon prints a book's Best Sellers Rank only on its product page. The Scrivy
Book Scout add-on solves that by fetching each result's `/dp/<asin>` in the
browser (same-origin, with the profile's own cookies) and regexing the rank out
of the returned HTML. That fetch is not magic and it is not a Scrivy service:
any page on the marketplace can do it. So the same capability works with the
add-on uninstalled — that is what this module does:

1. collect the result ASINs from the search page,
2. fetch each `/dp/<asin>` from inside that page (`fetch(..., credentials:
   "include")`), a few at a time with human pacing,
3. parse the rank with the same patterns the add-on uses (localized formats
   included), plus the category chain ("#4,231 in Books").

What the add-on adds on top is convenience and its own estimates: instant
badges without the extra requests, and estimated monthly sales/revenue derived
from the rank. Those estimates are NOT reproduced here — the native path
records Amazon's printed ranks only, as ordinary page observations.

Nothing is invented: a failed or robot-blocked fetch is reported as such, never
as a missing rank that reads like "this book has no rank".
"""
from __future__ import annotations

import asyncio
import random
import re
from collections import deque
from typing import Any

# The add-on's own rank patterns (content/amazon.js → BSR_PATTERNS). Kept in
# the same order: the first hit is the book's main rank, later hits are the
# sub-category ranks shown right under it.
BSR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Best Sellers Rank[\s\S]{0,400}?#([\d,.]+)\s+in\s+([^(<\n]{2,100})"),
    re.compile(r"Bestseller-Rang[\s\S]{0,400}?Nr\.\s*([\d.,]+)\s+in\s+([^(<\n]{2,100})"),
    re.compile(
        r"Classement des meilleures ventes[\s\S]{0,400}?([\d.,\u00a0\u202f ]+)\s+en\s+([^(<\n]{2,100})"
    ),
    re.compile(
        r"Clasificaci\u00f3n en los m\u00e1s vendidos[\s\S]{0,400}?n\u00ba\s*([\d.,]+)\s+en\s+([^(<\n]{2,100})"
    ),
    re.compile(
        r"Posizione nella classifica[\s\S]{0,400}?n\.\s*([\d.,]+)\s+in\s+([^(<\n]{2,100})"
    ),
)
_CATEGORY_RANK = re.compile(r"#([\d,.]+)\s+in\s+(?:<a[^>]*>\s*)?([^(<\n]{2,100})")
_PRODUCT_TITLE = re.compile(r'<span id="productTitle"[^>]*>\s*([\s\S]{1,300}?)\s*</span>')
_INDIE = re.compile(r"Independently published", re.IGNORECASE)

# A bot wall must never be mistaken for a page without a rank.
_BLOCK_MARKERS = (
    "api-services-support@amazon.com",
    "Enter the characters you see below",
    "Type the characters you see in this image",
)

MAX_HTML_CHARS = 1_500_000

_ASINS_JS = """
() => Array.from(
  document.querySelectorAll('div[data-component-type="s-search-result"][data-asin]')
).map((el) => el.getAttribute('data-asin')).filter((a) => a && a.length >= 10)
"""

_FETCH_JS = """
async ({ asin }) => {
  try {
    const res = await fetch(`/dp/${encodeURIComponent(asin)}`, {
      credentials: 'include',
      headers: { Accept: 'text/html' },
    });
    const url = res.url || '';
    if (!res.ok) return { status: res.status, url, html: '' };
    const html = await res.text();
    return { status: res.status, url, html: html.slice(0, {limit}) };
  } catch (e) {
    return { status: 0, url: '', html: '', error: String(e && e.message || e) };
  }
}
""".replace("{limit}", str(MAX_HTML_CHARS))


def parse_localized_int(raw: str) -> int | None:
    """`1,234` / `1.234` / `1 234` → 1234 (the add-on's own rule)."""
    digits = re.sub(r"[^\d]", "", str(raw or ""))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())


def parse_product_bsr(html: str) -> dict[str, Any]:
    """One product page's HTML → its printed BSR and category chain.

    Returns `{"rank", "category", "ranks", "title", "indie", "blocked"}`.
    `rank` is `None` when Amazon printed none (or the HTML was a wall, which
    `blocked` says explicitly)."""
    text = html or ""
    blocked = any(marker in text for marker in _BLOCK_MARKERS)
    out: dict[str, Any] = {
        "rank": None,
        "category": "",
        "ranks": [],
        "title": "",
        "indie": bool(_INDIE.search(text)),
        "blocked": blocked,
    }
    if not text or blocked:
        return out
    title = _PRODUCT_TITLE.search(text)
    if title:
        out["title"] = _clean(title.group(1))[:300]
    for pattern in BSR_PATTERNS:
        hit = pattern.search(text)
        if not hit:
            continue
        out["rank"] = parse_localized_int(hit.group(1))
        out["category"] = _clean(hit.group(2))[:160]
        window = text[hit.start() : hit.start() + 3000]
        seen: set[tuple[int, str]] = set()
        for rank_hit in _CATEGORY_RANK.finditer(window):
            rank = parse_localized_int(rank_hit.group(1))
            category = _clean(rank_hit.group(2))[:160]
            if rank is None or not category:
                continue
            key = (rank, category)
            if key in seen:
                continue
            seen.add(key)
            out["ranks"].append({"rank": rank, "category": category})
            if len(out["ranks"]) >= 10:
                break
        break
    return out


async def result_asins(page: Any, *, max_items: int = 16) -> list[str]:
    """The ASINs of the search results on this page, in page order."""
    try:
        raw = await page.evaluate(_ASINS_JS)
    except Exception:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for asin in raw or []:
        value = str(asin or "").strip().upper()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
        if len(out) >= max_items:
            break
    return out


async def scan_page_bsr(
    page: Any,
    *,
    asins: list[str] | None = None,
    max_items: int = 16,
    concurrency: int = 2,
    pacing: tuple[float, float] = (0.6, 1.3),
    retry_blocked: bool = True,
) -> dict[str, Any]:
    """Fetch each result's product page from the search page and read the rank.

    The fetches happen inside the page, so they carry the profile's Amazon
    session — the same request the add-on makes, at the same pace (the add-on
    runs 2 at a time and waits ~0.6–1.3s after each). A wall is retried once
    after a longer pause before it is reported. Returns rows in page order plus
    honest counts.
    """
    targets = [a for a in (asins or []) if a][:max_items] or await result_asins(
        page, max_items=max_items
    )
    sem = asyncio.Semaphore(max(1, concurrency))

    async def fetch(asin: str) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = await page.evaluate(_FETCH_JS, {"asin": asin})
        await asyncio.sleep(random.uniform(*pacing))
        if not isinstance(payload, dict):
            payload = {"status": 0, "html": "", "url": "", "error": "empty response"}
        return payload, parse_product_bsr(str(payload.get("html") or ""))

    async def one(asin: str) -> dict[str, Any]:
        async with sem:
            try:
                payload, parsed = await fetch(asin)
                if (
                    retry_blocked
                    and not parsed["rank"]
                    and parsed["blocked"]
                    and state["retries"] == 0
                    and not any(r.get("rank") for r in done.values())
                ):
                    # The FIRST wall may be a transient rate limit: pause longer
                    # and try that one again. Only once — a second wall means the
                    # page is being throttled, and more requests make it worse.
                    state["retries"] = 1
                    await asyncio.sleep(random.uniform(2.5, 4.0))
                    payload, parsed = await fetch(asin)
            except Exception as exc:  # page went away / navigation race
                return {"asin": asin, "rank": None, "category": "", "ranks": [],
                        "error": f"fetch failed: {exc}"}
            status = int(payload.get("status") or 0)
            html = str(payload.get("html") or "")
            row = {
                "asin": asin,
                "rank": parsed["rank"],
                "category": parsed["category"],
                "ranks": parsed["ranks"],
                "title": parsed["title"],
                "indie": parsed["indie"],
                "url": payload.get("url") or "",
                "status": status,
                "blocked": parsed["blocked"],
            }
            if not html:
                row["error"] = (
                    payload.get("error")
                    or ("robot check" if parsed["blocked"] else f"HTTP {status}")
                )
            elif parsed["rank"] is None:
                row["error"] = "robot check" if parsed["blocked"] else "no rank printed"
            return row

    # A worker pool instead of one big gather, so a wall streak can stop the
    # scan early: sixteen futile requests help nobody, and the row says so.
    queue = deque(targets)
    done: dict[str, dict[str, Any]] = {}
    state = {"wall_streak": 0, "stopped": False, "retries": 0}

    async def worker() -> None:
        while queue and not state["stopped"]:
            asin = queue.popleft()
            row = await one(asin)
            done[asin] = row
            if row.get("blocked"):
                state["wall_streak"] += 1
                if state["wall_streak"] >= 4:
                    state["stopped"] = True
            elif row.get("rank"):
                state["wall_streak"] = 0

    await asyncio.gather(*(worker() for _ in range(min(max(1, concurrency), len(targets)))))
    rows: list[dict[str, Any]] = []
    for asin in targets:
        row = done.get(asin)
        if row is None:
            row = {
                "asin": asin, "rank": None, "category": "", "ranks": [],
                "blocked": False, "skipped": "rate limited — not attempted",
            }
        rows.append(dict(row))
    with_rank = [r for r in rows if r.get("rank")]
    return {
        "source": "product_pages",
        "scanned": sum(1 for r in rows if "skipped" not in r),
        "with_bsr": len(with_rank),
        "failed": sum(1 for r in rows if not r.get("rank")),
        "blocked": sum(1 for r in rows if r.get("blocked")),
        "skipped": sum(1 for r in rows if "skipped" in r),
        "rows": rows,
    }
