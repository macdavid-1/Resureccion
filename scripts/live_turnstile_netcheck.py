"""Network-instrumented check of the KDSpy login page through the proxy.

Answers: does challenges.cloudflare.com load? does the Turnstile widget
render or auto-solve (managed challenge fills the token invisibly on
trusted IPs)? Any console errors?
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")


async def main() -> int:
    from playwright.async_api import async_playwright

    from app import stealth
    from app.config import get_config
    from app.privacy import relay_launch_args
    from app.proxy_spec import playwright_proxy_kwarg

    config = get_config()
    proxy_kw = playwright_proxy_kwarg(config.browser_proxy)
    async with async_playwright() as pw:
        args = list(relay_launch_args([]))
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(config.browser_profiles_dir / "live-proxy-check"),
            channel=config.browser_channel,
            headless=config.browser_headless,
            args=args,
            proxy=proxy_kw,
        )
        page = await ctx.new_page()
        stealth.apply(page)

        cf_events: list[str] = []
        console: list[str] = []

        def on_response(resp):
            if "cloudflare" in resp.url:
                cf_events.append(f"RESP {resp.status} {resp.url[:100]}")

        def on_reqfail(req):
            if "cloudflare" in req.url:
                cf_events.append(f"FAIL {req.failure} {req.url[:100]}")

        def on_console(msg):
            if msg.type in ("error", "warning"):
                console.append(f"{msg.type}: {msg.text[:120]}")

        page.on("response", on_response)
        page.on("requestfailed", on_reqfail)
        page.on("console", on_console)

        await page.goto("https://publishingaltitude.com/login", timeout=45000)
        # Managed challenges can take a while to auto-solve.
        for i in range(6):
            await page.wait_for_timeout(5000)
            state = await page.evaluate(
                """() => {
                    const token = document.querySelector(
                        'input[name="cf-turnstile-response"], [name^="cf-turnstile"]'
                    );
                    const widget = document.querySelector('.cf-turnstile, [class*="turnstile" i]');
                    const iframes = Array.from(document.querySelectorAll('iframe')).map(
                        (f) => (f.src || '').slice(0, 60)
                    );
                    return {
                        tokenLen: token ? String(token.value || '').length : -1,
                        widgetHtml: widget ? widget.outerHTML.slice(0, 150) : null,
                        iframes,
                    };
                }"""
            )
            if state["tokenLen"] and state["tokenLen"] > 0:
                break

        print(f"cloudflare network events ({len(cf_events)}):")
        for e in cf_events[:10]:
            print("  ", e)
        print(f"console errors/warnings ({len(console)}):")
        for c in console[:5]:
            print("  ", c)
        print(f"token length after wait: {state['tokenLen']}")
        print(f"widget element: {state['widgetHtml']}")
        print(f"iframes: {state['iframes']}")

        # Definitive signal: submit-readiness of the login form.
        user_visible = await page.evaluate(
            """() => {
                const u = document.querySelector('#user_login');
                const p = document.querySelector('#user_pass');
                const b = document.querySelector('#wp-submit, input[type=submit]');
                return {user: !!u, pass: !!p, button: !!b};
            }"""
        )
        print(f"login form present: {user_visible}")
        await page.screenshot(path="verify_tmp/proxy_login2.png")
        await ctx.close()
        return 0 if (state["tokenLen"] or 0) > 0 else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
