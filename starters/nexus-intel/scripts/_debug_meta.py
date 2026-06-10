#!/usr/bin/env python3
"""Debug helper — dump Meta Ad Library page text for selector diagnosis."""
import sys, time
from pathlib import Path
from urllib.parse import quote_plus

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright not installed")

URL = (
    "https://www.facebook.com/ads/library/"
    "?active_status=active&ad_type=all&country=US&q=Brian+Cain+Peak+Performance"
)
EXEC = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

with sync_playwright() as pw:
    browser = pw.chromium.launch(
        headless=True,
        executable_path=EXEC if Path(EXEC).exists() else None,
    )
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        ignore_https_errors=True,
        locale="en-US",
    )
    ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    page = ctx.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
    time.sleep(4)

    # Scroll once
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(3)

    html = page.content()
    out = Path("/tmp/meta_debug.html")
    out.write_text(html)
    print(f"Page HTML saved: {out} ({len(html)} bytes)")

    # Try various selectors
    for sel in [
        '[data-testid="ad_archive_preview_card"]',
        "._7jyr",
        '[data-pagelet="AdsLibrarySearchResults"]',
        ".xh8yej3",   # generic ad card class sometimes used
        "._8u8l",
        "._6k-7",
        "div[class*='_9aap']",
    ]:
        els = page.locator(sel).all()
        print(f"  {sel!r}: {len(els)} elements")

    # Print first 3000 chars of body text
    body_text = page.locator("body").inner_text()
    print("\n--- body text (first 3000 chars) ---")
    print(body_text[:3000])
    browser.close()
