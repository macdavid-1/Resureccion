"""The category census — discovery as a measured walk, not a brainstorm.

Phase 1's model can only reason about markets it has actually SEEN. Left to
its imagination it drifts toward categories it already knows, which is
exactly the ceiling on "markets I didn't know existed". This module removes
that ceiling deterministically: BEFORE the model opines, the backend walks
Amazon's own structure — the bestseller chart, the new-release chart and the
browse-node category tree hanging off them — and harvests rank, price, rating
and review-count from every visited node.

The census is ordinary evidence (stable ids, `redact()`, `browser_evidence`)
so Phase 1 receives it as measured input and reasons over it instead of
guessing where to look. You cannot discover a category you never visited:
this makes the system visit them.

Budget: node count, items per node, marketplace count and a wall clock are
all config-fixed. Every step is failure-isolated — a page that will not
render is recorded as a miss and the walk continues.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from app.marketplace import Marketplace
from app.redact import redact, scrub_url

# Extract category links from the bestseller/new-release page (Amazon's own
# navigation order preserved). Multi-selector for layout drift.
_CATEGORY_LINKS_JS = """() => {
  const out = [];
  const seen = new Set();
  const push = (text, href) => {
    if (!text || !href) return;
    const t = text.trim().replace(/\\s+/g, ' ');
    if (!t || t.length > 60 || seen.has(t.toLowerCase())) return;
    if (!(href.includes('/gp/bestsellers/') || href.includes('/gp/new-releases/')
          || href.includes('/b/') || href.includes('zg'))) return;
    seen.add(t.toLowerCase());
    out.push({text: t, href: href});
  };
  document.querySelectorAll('a[href]').forEach((a) => {
    push(a.textContent || a.getAttribute('aria-label') || '', a.getAttribute('href') || '');
  });
  return out.slice(0, 80);
}"""

# Extract chart/category items: rank, title, price, rating, reviews, asin.
# Several selector families because Amazon rotates bestseller layouts.
_ITEMS_JS = """() => {
  const num = (s) => {
    if (!s) return null;
    const m = String(s).replace(/[^0-9.]/g, '');
    if (!m) return null;
    const v = Number(m);
    return Number.isFinite(v) ? v : null;
  };
  const text = (root, sels) => {
    for (const sel of sels) {
      const el = root.querySelector(sel);
      if (el) {
        const t = (el.textContent || '').trim();
        if (t) return t;
      }
    }
    return '';
  };
  const out = [];
  const roots = Array.from(
    document.querySelectorAll('#gridItemRoot, .zg-grid-general-faceout, [id^="p13n-asin-index"], li[data-asin]')
  );
  roots.forEach((root, i) => {
    const asin = (root.getAttribute('data-asin') || '').trim()
      || (String(root.id || '').match(/([A-Z0-9]{10})/) || [])[1] || '';
    const title = text(root, [
      'img[alt]', '.p13n-sc-truncate', '.p13n-sc-truncated', 'a.a-link-normal span',
      '.a-size-medium', '.a-size-base',
    ]);
    const price = text(root, ['.p13n-sc-price', 'span.a-price > span.a-offscreen', '.a-price .a-offscreen']);
    const rating = text(root, ['i.a-icon-alt', 'span.a-icon-alt']);
    const reviews = text(root, ['span.a-size-small', 'a[href*="product-reviews"] span', '.a-size-small']);
    const link = (root.querySelector('a[href*="/dp/"]') || {}).href || '';
    if (!title && !asin) return;
    out.push({
      position: i + 1,
      asin: asin,
      title: title.slice(0, 200),
      price: price || null,
      rating: /([0-9](?:\\.[0-9])?)/.test(rating || '') ? Number(RegExp.$1) : null,
      review_count: num(reviews),
      url: link.split('?')[0] || '',
    });
  });
  return out.slice(0, 40);
}"""


@dataclass
class CensusResult:
    categories: list[dict[str, Any]] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    visited_nodes: int = 0
    charts_captured: int = 0
    elapsed_seconds: float = 0.0
    stopped_reason: str = "completed"

    @property
    def category_names(self) -> list[str]:
        return [str(c.get("category") or "") for c in self.categories if c.get("category")]

    def to_dict(self) -> dict[str, Any]:
        return {
            "categories": self.categories[:40],
            "category_names": self.category_names[:40],
            "evidence_ids": self.evidence_ids,
            "visited_nodes": self.visited_nodes,
            "charts_captured": self.charts_captured,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "stopped_reason": self.stopped_reason,
        }


def _review_median(items: list[dict[str, Any]]) -> int | None:
    counts = sorted(
        int(r["review_count"]) for r in items[:10]
        if isinstance(r.get("review_count"), (int, float)) and int(r["review_count"]) >= 0
    )
    if not counts:
        return None
    mid = len(counts) // 2
    if len(counts) % 2:
        return counts[mid]
    return int(round((counts[mid - 1] + counts[mid]) / 2))


class CategoryCensus:
    def __init__(
        self,
        browser: Any,
        evidence_store: Any,
        events: Any = None,
        *,
        max_categories: int = 12,
        items_per_category: int = 20,
        max_marketplaces: int = 1,
        time_budget_seconds: float = 600.0,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.browser = browser
        self.evidence_store = evidence_store
        self.events = events
        self.max_categories = max(0, int(max_categories))
        self.items_per_category = max(0, int(items_per_category))
        self.max_marketplaces = max(1, int(max_marketplaces))
        self.time_budget = max(1.0, float(time_budget_seconds))
        self.should_stop = should_stop or (lambda: False)

    # ---------------------------------------------------------------- run
    async def run(
        self, *, session_id: str, marketplaces: list[Any]
    ) -> CensusResult:
        """Walk charts + category tree. Never raises."""
        result = CensusResult()
        start = time.monotonic()
        deadline = start + self.time_budget
        mkts = [m for m in (marketplaces or []) if isinstance(m, Marketplace)]
        mkts = mkts[: self.max_marketplaces]
        if not mkts or self.max_categories == 0:
            result.stopped_reason = "disabled"
            result.elapsed_seconds = time.monotonic() - start
            return result

        for mkt in mkts:
            if result.stopped_reason != "completed":
                break
            if self.should_stop():
                result.stopped_reason = "stopped"
                break
            if time.monotonic() > deadline:
                result.stopped_reason = "time_budget"
                break
            try:
                await self._census_marketplace(
                    session_id, mkt, result, deadline
                )
            except Exception:
                # A marketplace that blows up mid-walk contributes nothing
                # but also stops nothing.
                continue

        if (
            result.stopped_reason == "completed"
            and time.monotonic() > deadline
        ):
            result.stopped_reason = "time_budget"
        if self.events is not None:
            try:
                self.events.append(
                    session_id, level="info", actor="system",
                    action="category_census_completed",
                    detail=redact(result.to_dict()),
                )
            except Exception:
                pass
        result.elapsed_seconds = time.monotonic() - start
        return result

    # -------------------------------------------------- one marketplace
    async def _census_marketplace(
        self,
        session_id: str,
        mkt: Marketplace,
        result: CensusResult,
        deadline: float,
    ) -> None:
        budget_left = self.max_categories

        # 1. The bestseller chart itself (top of the market, measured).
        chart_specs = (
            ("bestsellers", "/gp/bestsellers/books"),
            ("new_releases", "/gp/new-releases/books"),
        )
        category_links: list[dict[str, str]] = []
        for label, path in chart_specs:
            if self.should_stop() or time.monotonic() > deadline:
                result.stopped_reason = "time_budget"
                return
            page = await self.browser.open_marketplace(mkt, path)
            try:
                items = await self._extract(page, _ITEMS_JS)
                if items:
                    await self._record(
                        session_id, mkt, result,
                        category=label,
                        path=path,
                        node_id="",
                        items=items,
                        chart=label,
                    )
                    result.charts_captured += 1
                if not category_links:
                    links = await self._extract(page, _CATEGORY_LINKS_JS)
                    category_links = [
                        {"text": str(l.get("text") or ""), "href": str(l.get("href") or "")}
                        for l in links
                        if isinstance(l, dict)
                    ]
            finally:
                try:
                    await self.browser.close_page(page)
                except Exception:
                    pass

        # 2. Walk the browse-node tree the charts expose.
        walked = 0
        for link in category_links:
            if walked >= budget_left:
                break
            if self.should_stop() or time.monotonic() > deadline:
                result.stopped_reason = "time_budget"
                return
            path = _link_path(link.get("href") or "")
            if not path:
                continue
            try:
                page = await self.browser.open_marketplace(mkt, path)
            except Exception:
                continue  # a dead node is a miss, not a stop
            try:
                items = await self._extract(page, _ITEMS_JS)
            finally:
                try:
                    await self.browser.close_page(page)
                except Exception:
                    pass
            if not items:
                continue  # nothing measured: do not record an empty row
            walked += 1
            result.visited_nodes += 1
            await self._record(
                session_id, mkt, result,
                category=link.get("text") or path,
                path=path,
                node_id=_node_id(path),
                items=items,
                chart="category",
            )

    # ------------------------------------------------------------ helpers
    async def _extract(self, page: Any, js: str) -> list[dict[str, Any]]:
        try:
            out = await page.evaluate(js)
        except Exception:
            return []
        if not isinstance(out, list):
            return []
        return [x for x in out if isinstance(x, dict)]

    async def _record(
        self,
        session_id: str,
        mkt: Marketplace,
        result: CensusResult,
        *,
        category: str,
        path: str,
        node_id: str,
        items: list[dict[str, Any]],
        chart: str,
    ) -> None:
        trimmed = items[: max(1, self.items_per_category)]
        prices = [
            r for r in trimmed
            if r.get("price") not in (None, "", 0)
        ]
        data = {
            "category": category,
            "chart": chart,
            "path": path,
            "node_id": node_id,
            "marketplace": mkt.code,
            "items": trimmed,
            "item_count": len(trimmed),
            "top10_review_median": _review_median(trimmed),
            "priced_items": len(prices),
        }
        try:
            rec = self.evidence_store.create(
                session_id=session_id,
                marketplace=mkt.code,
                kind="census",
                url=scrub_url(f"{mkt.base_url}{path}"),
                title=f"census: {category}"[:300],
                data=redact(data),
            )
            result.evidence_ids.append(rec.id)
            result.categories.append(
                {
                    "category": category,
                    "chart": chart,
                    "marketplace": mkt.code,
                    "item_count": len(trimmed),
                    "top10_review_median": data["top10_review_median"],
                    "evidence_id": rec.id,
                }
            )
        except Exception:
            pass  # persistence pressure must never abort the walk


def _link_path(href: str) -> str:
    """Reduce an absolute/relative Amazon link to a safe '/path?query'."""
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("http"):
        parsed = urlparse(href)
        path, query = parsed.path, parsed.query
    else:
        if "?" in href:
            path, query = href.split("?", 1)
        else:
            path, query = href, ""
    if not path.startswith("/"):
        return ""
    # Only the chart/category families we walked are ever re-opened: a stray
    # navigation target from the page cannot redirect the census elsewhere.
    if not (
        path.startswith("/gp/bestsellers/")
        or path.startswith("/gp/new-releases/")
        or path.startswith("/b/")
    ):
        return ""
    return f"{path}?{query}" if query else path


def _node_id(path: str) -> str:
    tail = path.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    return tail if tail.isdigit() else ""
