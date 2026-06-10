#!/usr/bin/env python3
"""
scrape_meta_ads.py — Scrape Meta Ad Library for target advertisers.

Uses Playwright to load facebook.com/ads/library, scroll for all active ads,
and insert raw creative text + metadata into the meta_ads table.

Usage:
  python3 scripts/scrape_meta_ads.py --company "Brian Cain Peak Performance"
  python3 scripts/scrape_meta_ads.py --all
  python3 scripts/scrape_meta_ads.py --company "Brian Cain Peak Performance" --dry-run
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from _lib import finish_scrape_run, get_db, start_scrape_run

# ── Target companies ────────────────────────────────────────────────────────
TARGET_COMPANIES: dict[str, dict] = {
    "Brian Cain Peak Performance": {
        "search": "Brian Cain Peak Performance",
        "source_match": "briancain",
    },
}

AD_LIBRARY_BASE = "https://www.facebook.com/ads/library/"
SCROLL_PAUSE = 2.5   # seconds between scrolls
MAX_SCROLLS  = 20    # cap to avoid infinite loops


def build_url(search_term: str) -> str:
    params = {
        "active_status": "active",
        "ad_type": "all",
        "country": "US",
        "q": search_term,
    }
    query = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())
    return f"{AD_LIBRARY_BASE}?{query}"


def human_pause(lo: float = 1.0, hi: float = 2.5) -> None:
    import random
    time.sleep(random.uniform(lo, hi))


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS refresh_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            approved_at TEXT
        );
        INSERT OR IGNORE INTO refresh_batches (id, name, approved_at)
            VALUES (0, 'default', datetime('now'));

        CREATE TABLE IF NOT EXISTS meta_ads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            advertiser_name TEXT NOT NULL,
            source_id INTEGER REFERENCES sources(id),
            ad_platform TEXT DEFAULT 'meta',
            ad_url TEXT,
            advertiser_page_id TEXT,
            landing_page_url TEXT,
            ad_start_date TEXT,
            creative_text TEXT,
            -- taxonomy (Claude-classified)
            primary_hook TEXT,
            persona_targeted TEXT,
            pain_point TEXT,
            promised_outcome TEXT,
            offer_type TEXT,
            funnel_stage TEXT,
            proof_used TEXT,
            category_narrative TEXT,
            ad_longevity_signal TEXT,
            creative_pattern TEXT,
            messaging_angle TEXT,
            counter_positioning TEXT,
            content_opportunity TEXT,
            outbound_angle TEXT,
            -- housekeeping
            scraped_at TEXT DEFAULT (datetime('now')),
            classified_at TEXT,
            batch_id INTEGER REFERENCES refresh_batches(id) DEFAULT 0,
            UNIQUE(advertiser_name, ad_url)
        );
    """)
    conn.commit()


def compute_longevity(start_date_str: str | None) -> str:
    if not start_date_str:
        return "unknown"
    try:
        start = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - start).days
        if days < 7:
            return "new-test"
        if days < 30:
            return "scaling"
        return "long-running"
    except Exception:
        return "unknown"


def scrape_company(page, company: str, cfg: dict, dry_run: bool) -> list[dict]:
    url = build_url(cfg["search"])
    print(f"  › {url}")
    if dry_run:
        print("  [dry-run] skipping browser load")
        return []

    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    human_pause(2, 4)

    # Dismiss any cookie / login popups
    for selector in ['[aria-label="Close"]', '[data-testid="cookie-policy-dialog-accept-button"]']:
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=2000):
                btn.click()
                human_pause(0.5, 1.0)
        except Exception:
            pass

    # Scroll to load lazy cards
    prev_height = 0
    for _ in range(MAX_SCROLLS):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(SCROLL_PAUSE)
        cur_height = page.evaluate("document.body.scrollHeight")
        if cur_height == prev_height:
            break
        prev_height = cur_height

    # Extract ad cards
    cards = page.locator('[data-testid="ad_archive_preview_card"]').all()
    if not cards:
        # fallback: any element containing recognisable ad-card structure
        cards = page.locator("._7jyr").all()

    ads: list[dict] = []
    for card in cards:
        try:
            text = card.inner_text()
        except Exception:
            continue

        # Extract start date from text like "Started running on May 1, 2025"
        start_date = None
        m = re.search(r"Started running on (.+?)(?:\n|$)", text)
        if m:
            try:
                start_date = datetime.strptime(m.group(1).strip(), "%B %d, %Y").date().isoformat()
            except ValueError:
                pass

        # Grab "Learn More" / destination link if present
        try:
            link_el = card.locator("a[href]").first
            landing = link_el.get_attribute("href") or ""
            if "facebook.com" in landing or "instagram.com" in landing:
                landing = ""
        except Exception:
            landing = ""

        # Try to get ad library permalink from card link
        try:
            ad_link_el = card.locator('a[href*="ads/library"]').first
            ad_url = ad_link_el.get_attribute("href") or ""
        except Exception:
            ad_url = ""

        ads.append({
            "advertiser_name": company,
            "creative_text": text.strip(),
            "ad_start_date": start_date,
            "landing_page_url": landing,
            "ad_url": ad_url,
            "ad_longevity_signal": compute_longevity(start_date),
        })

    return ads


def insert_ads(conn: sqlite3.Connection, ads: list[dict]) -> int:
    inserted = 0
    for ad in ads:
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO meta_ads
                    (advertiser_name, creative_text, ad_start_date,
                     landing_page_url, ad_url, ad_longevity_signal, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    ad["advertiser_name"],
                    ad["creative_text"],
                    ad.get("ad_start_date"),
                    ad.get("landing_page_url"),
                    ad.get("ad_url"),
                    ad.get("ad_longevity_signal", "unknown"),
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        except sqlite3.Error as e:
            print(f"  ✗ insert error: {e}")
    conn.commit()
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", help="Exact company key from TARGET_COMPANIES")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.company and not args.all:
        parser.error("Pass --company <name> or --all")

    targets: dict[str, dict] = {}
    if args.all:
        targets = TARGET_COMPANIES
    else:
        if args.company not in TARGET_COMPANIES:
            # Try case-insensitive match
            lc = args.company.lower()
            for k in TARGET_COMPANIES:
                if k.lower() == lc:
                    targets[k] = TARGET_COMPANIES[k]
                    break
            if not targets:
                sys.exit(f"Unknown company: {args.company!r}. Add it to TARGET_COMPANIES.")
        else:
            targets[args.company] = TARGET_COMPANIES[args.company]

    conn = get_db()
    ensure_schema(conn)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright not installed. Run: pip install playwright && playwright install chromium")

    total_inserted = 0

    with sync_playwright() as pw:
        # Use pre-installed Chromium if the default path doesn't exist
        import os as _os
        _exec = _os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or (
            "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
            if Path("/opt/pw-browsers/chromium-1194/chrome-linux/chrome").exists()
            else None
        )
        launch_kwargs = {"headless": True}
        if _exec:
            launch_kwargs["executable_path"] = _exec
        browser = pw.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
            locale="en-US",
            ignore_https_errors=True,
        )
        # Stealth: hide webdriver flag
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = ctx.new_page()

        for company, cfg in targets.items():
            run_id = start_scrape_run(conn, "meta-ads", company, "meta")
            print(f"\n[{company}]")
            ads = scrape_company(page, company, cfg, args.dry_run)
            print(f"  found {len(ads)} ad cards")
            if not args.dry_run:
                n = insert_ads(conn, ads)
                total_inserted += n
                print(f"  inserted {n} new rows")
            finish_scrape_run(conn, run_id, len(ads))

        browser.close()

    print(f"\nDone. Total new ads inserted: {total_inserted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
