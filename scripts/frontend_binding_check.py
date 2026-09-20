"""Frontend regression check: the Settings action buttons must be bound.

Guards against the failure mode where HTML ships new buttons but the served
app.js predates their handlers (stale-cache / partial deploy), which presents
to the owner as "buttons do nothing". Verifies:

1. index.html references the handler-bearing app.js (versioned query),
2. the served app.js contains the binding calls and their targets exist,
3. the served HTML contains the buttons those handlers expect,
4. every #id referenced by the interactive-browser JS exists in the HTML.

Run: python scripts/frontend_binding_check.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HTML = ROOT / "static" / "index.html"
JS = ROOT / "static" / "app.js"

FAILURES: list[str] = []


def check(name: str, ok: bool, note: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {note}" if note and not ok else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    html = HTML.read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")

    # 1. Cache-busted asset references. Since app/main.py serves index.html
    # through app.static_cache, versions are content hashes injected at
    # request time — the raw file just needs a placeholder (?v=...) so the
    # rewrite regex can hook it.
    check(
        "app.js is referenced with a version query",
        bool(re.search(r'src="/static/app\.js\?v=[a-zA-Z0-9]+"', html)),
        "index.html must reference /static/app.js with a ?v= placeholder",
    )
    check(
        "styles.css is referenced with a version query",
        bool(re.search(r'href="/static/styles\.css\?v=[a-zA-Z0-9]+"', html)),
    )
    try:
        from app.static_cache import asset_version, render_index

        rendered = render_index(ROOT / "static")
        for asset in ("app.js", "styles.css"):
            expected = f'/static/{asset}?v={asset_version(ROOT / "static", asset)}'
            check(f"rendered index references {asset} by content hash", expected in rendered)
        check(
            "rendered index has no stale numeric versions",
            not re.search(r'\?v=\d+"', rendered),
        )
    except Exception as exc:  # pragma: no cover
        check("static_cache import/render", False, str(exc))

    # 2. Handlers exist in app.js.
    for needle in ("function bindKdspyUpload", "function rbBind", '("#set-amazon-open").addEventListener', '("#set-kdspy-open").addEventListener'):
        check(f"app.js defines {needle!r}", needle in js)

    # 3. Bindings are actually invoked at boot.
    check("rbBind() is called", "rbBind();" in js)
    check("bindKdspyUpload() is called", "bindKdspyUpload();" in js)

    # 4. The HTML contains the controls those handlers bind to.
    for dom_id in (
        "set-amazon-open", "set-kdspy-open", "kdspy-pick-zip", "kdspy-pick-files",
        "kdspy-zip-input", "kdspy-files-input", "kdspy-upload-row",
        "rbrowser", "rb-frame", "rb-back", "rb-send", "rb-text",
    ):
        check(f"index.html contains #{dom_id}", f'id="{dom_id}"' in html)

    # 5. Every #id referenced from JS must exist in the HTML or be created
    # by JS-generated markup (report view etc.).
    html_ids = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', html))
    js_ids = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', js))
    js_refs = set(re.findall(r'\$\("#([a-zA-Z0-9_-]+)"\)', js))
    missing = sorted(js_refs - html_ids - js_ids)
    check("every $('#…') target exists in HTML or JS templates", not missing, f"missing: {missing}")

    print()
    if FAILURES:
        print(f"FRONTEND BINDING CHECK: FAIL ({len(FAILURES)})")
        return 1
    print("FRONTEND BINDING CHECK: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
