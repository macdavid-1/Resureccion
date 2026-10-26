"""Frontend regression check: the Settings action buttons must be bound.

Guards against the failure mode where HTML ships new buttons but the served
app.js predates their handlers (stale-cache / partial deploy), which presents
to the owner as "buttons do nothing". Verifies:

1. index.html references the handler-bearing app.js (versioned query),
2. the served app.js contains the binding calls and their targets exist,
3. the served HTML contains the buttons those handlers expect,
4. every #id referenced by the interactive-browser JS exists in the HTML,
5. app.js actually BOOTS: it is executed in strict mode against a stub DOM,
   so an undeclared assignment (ReferenceError under \"use strict\") fails the
   check instead of silently killing rbBind() — which is what made every tap
   on the sign-in browser do nothing while the overlay still rendered.

Run: python scripts/frontend_binding_check.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HTML = ROOT / "static" / "index.html"
JS = ROOT / "static" / "app.js"

FAILURES: list[str] = []

# Executes app.js the way the browser does — strict mode, from top to boot —
# against a stub DOM. Any ReferenceError (an undeclared assignment, a use
# before declaration) rejects the boot promise and fails the check. Handlers
# bound BEFORE the error still attach, so this is the only way to catch a
# partial bind that leaves the page looking healthy while taps do nothing.
_BOOT_JS = r'''
const vm = require("vm");
const fs = require("fs");
const src = fs.readFileSync("static/app.js", "utf8");
let failure = null;
process.on("unhandledRejection", (e) => { failure = e; });

function stub(name) {
  return new Proxy(function () {}, {
    get(t, p) {
      if (p === "then") return undefined;            // never a thenable
      if (p === Symbol.toPrimitive) return () => "";
      if (p === Symbol.toStringTag) return "Stub";
      if (p === "length") return 0;
      return stub(name + "." + String(p));
    },
    set() { return true; },
    apply() { return stub(name + "()"); },
    construct() { return stub("new " + name); },
    has() { return true; },
  });
}
const globals = [
  "document", "localStorage", "sessionStorage", "fetch", "setInterval",
  "clearInterval", "setTimeout", "clearTimeout", "prompt", "alert",
  "confirm", "navigator", "location", "history", "FormData", "Image",
  "performance", "requestAnimationFrame", "matchMedia", "EventSource",
  "WebSocket", "ResizeObserver", "IntersectionObserver", "AbortController",
  "Headers", "Request", "Response", "atob", "btoa", "crypto", "console",
  "CustomEvent", "Event", "getComputedStyle", "URLSearchParams", "screen",
];
const sandbox = { console };
for (const g of globals) sandbox[g] = stub(g);
sandbox.URL = URL;
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

try {
  vm.runInContext(src, sandbox, { filename: "app.js" });
} catch (e) {
  failure = e;                                    // synchronous boot error
}

setTimeout(() => {                                // let async boot settle
  if (failure) {
    process.stderr.write("BOOT_FAIL: " + (failure && failure.message) + "\n"
      + ((failure && failure.stack) || "") + "\n");
    process.exit(1);
  }
  process.stdout.write("BOOT_OK\n");
}, 50);
'''


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
    for needle in (
        "function bindKdspyUpload",
        "function rbBind",
        '(".signin-mkt")',  # seven per-marketplace Amazon sign-in buttons
        '("#rb-zoom-in").addEventListener',
        '("#rb-page-up").addEventListener',
        '("#rb-page-down").addEventListener',
        '("#set-kdspy-open").addEventListener',
    ):
        check(f"app.js defines {needle!r}", needle in js)

    # 3. Bindings are actually invoked at boot.
    check("rbBind() is called", "rbBind();" in js)
    check("bindKdspyUpload() is called", "bindKdspyUpload();" in js)

    # 4. The HTML contains the controls those handlers bind to.
    for dom_id in (
        "set-amazon-signins", "set-kdspy-open", "kdspy-pick-zip", "kdspy-pick-files",
        "kdspy-zip-input", "kdspy-files-input", "kdspy-upload-row",
        "rbrowser", "rb-frame", "rb-back", "rb-send", "rb-text",
        "rb-zoom-in", "rb-zoom-out", "rb-zoom-pct",
        "rb-page-up", "rb-page-down",
    ):
        check(f"index.html contains #{dom_id}", f'id="{dom_id}"' in html)

    # 5. Every #id referenced from JS must exist in the HTML or be created
    # by JS-generated markup (report view etc.).
    html_ids = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', html))
    js_ids = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', js))
    js_refs = set(re.findall(r'\$\("#([a-zA-Z0-9_-]+)"\)', js))
    missing = sorted(js_refs - html_ids - js_ids)
    check("every $('#…') target exists in HTML or JS templates", not missing, f"missing: {missing}")

    # 5. The script boots. app.js is one strict-mode IIFE executed at load;
    #    a ReferenceError inside it aborts boot for real while the page still
    #    looks fine, so the only reliable check is to RUN it.
    if shutil.which("node"):
        try:
            proc = subprocess.run(
                ["node", "-e", _BOOT_JS], cwd=str(ROOT),
                capture_output=True, text=True, timeout=60, check=False,
            )
            out = (proc.stdout + proc.stderr).strip()
            check(
                "app.js boots clean under strict mode",
                proc.returncode == 0 and "BOOT_OK" in proc.stdout,
                out[-500:],
            )
        except Exception as exc:  # pragma: no cover - node misbehaving
            check("app.js boots clean under strict mode", False, str(exc))
    else:
        print("SKIP: node not available — boot check not run")

    print()
    if FAILURES:
        print(f"FRONTEND BINDING CHECK: FAIL ({len(FAILURES)})")
        return 1
    print("FRONTEND BINDING CHECK: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
