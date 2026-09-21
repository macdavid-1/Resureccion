"""Live check: the real research browser egresses through the owner's
configured Webshare proxy (Playwright native auth path), and a Cloudflare
Turnstile widget responds on the KDSpy login page.

Mirrors the app's launch path (channel, headless, privacy args, native
proxy auth). Reads BROWSER_PROXY from the environment (never prints it).
Run: .venv/bin/python scripts/live_proxy_browser_check.py
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")


async def main() -> int:
    from app import stealth
    from app.config import get_config
    from app.privacy import relay_launch_args
    from app.proxy_spec import playwright_proxy_kwarg

    config = get_config()
    if not config.browser_proxy:
        print("BROWSER_PROXY not set — nothing to verify")
        return 1
    proxy_kw = playwright_proxy_kwarg(config.browser_proxy)
    print(f"proxy server (no creds): {proxy_kw['server']}")

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        args: list[str] = []
        args.extend(relay_launch_args(args))
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(config.browser_profiles_dir / "live-proxy-check"),
            channel=config.browser_channel,
            headless=config.browser_headless,
            args=args,
            proxy=proxy_kw,
        )
        page = await ctx.new_page()
        stealth.apply(page)

        # 1. Egress IP as the marketplace would see it.
        await page.goto("https://api.ipify.org/?format=json", timeout=30000)
        egress = await page.inner_text("body")
        print(f"browser egress IP: {egress}")

        # 2. Turnstile response on the real KDSpy login page.
        await page.goto("https://publishingaltitude.com/login", timeout=45000)
        await page.wait_for_timeout(8000)
        boxes = await page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll('iframe').forEach((f) => {
                    const r = f.getBoundingClientRect();
                    const src = f.src || '';
                    if (/turnstile|challenges\\.cloudflare/.test(src)) {
                        out.push({x: r.x, y: r.y, w: r.width, h: r.height, src: src.slice(0, 80)});
                    }
                });
                // Token appears in a hidden input once the challenge passes.
                const token = document.querySelector(
                    'input[name="cf-turnstile-response"], [name^="cf-turnstile"]'
                );
                return {boxes: out, token: token ? String(token.value || '').length : -1};
            }"""
        )
        print(f"turnstile iframes: {boxes['boxes']}")
        print(f"token length (-1 = no widget input on page): {boxes['token']}")

        if not boxes["boxes"]:
            print("no Turnstile iframe found — page layout may have changed")
            await ctx.close()
            return 1

        b = boxes["boxes"][0]
        cx, cy = b["x"] + 25, b["y"] + b["h"] / 2
        print(f"tapping widget checkbox at ({cx:.0f}, {cy:.0f})")
        await page.mouse.click(cx, cy)
        await page.wait_for_timeout(6000)

        after = await page.evaluate(
            """() => {
                const token = document.querySelector(
                    'input[name="cf-turnstile-response"], [name^="cf-turnstile"]'
                );
                return {token: token ? String(token.value || '').length : -1};
            }"""
        )
        print(f"post-tap token length: {after['token']}")
        if after["token"] > 0:
            print("TURNSTILE PASSED — widget issued a token through the proxy")
            ok = True
        else:
            print("widget did not issue a token yet (may need a retry or the challenge is silent)")
            ok = False

        await ctx.close()
        return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
