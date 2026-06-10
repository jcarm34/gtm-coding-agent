#!/usr/bin/env python3
"""
classify_meta_ads.py — Claude-classify raw Meta ad rows.

Reads unclassified rows from meta_ads, sends them to Claude in batches of 5,
and writes the 18-column taxonomy back to each row.

Usage:
  python3 scripts/classify_meta_ads.py
  python3 scripts/classify_meta_ads.py --advertiser "Brian Cain Peak Performance"
  python3 scripts/classify_meta_ads.py --dry-run
  python3 scripts/classify_meta_ads.py --limit 20
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from _lib import get_db

BATCH_SIZE = 5

SYSTEM_PROMPT = """You are a competitive GTM analyst. You receive raw ad creative text from the Meta Ad Library and classify each ad using the following 18-column taxonomy. Return a JSON array — one object per ad — using exactly the keys shown. Do not add any other text outside the JSON array.

TAXONOMY FIELDS:

1.  primary_hook         — The opening line or visual hook of the ad (1-2 sentences, quoted or paraphrased)
2.  persona_targeted     — ONE of: founder | marketer | sales-leader | revops | developer | coach | athlete | student | parent | executive | other
3.  pain_point           — The core problem or frustration the ad agitates (1 sentence)
4.  promised_outcome     — What the ad says you will get or become (1 sentence)
5.  offer_type           — ONE of: demo | free-trial | report | template | webinar | product-signup | book | course | coaching | event | content | other
6.  funnel_stage         — ONE of: awareness | education | comparison | conversion | retargeting
7.  proof_used           — ONE of: customer-logos | stats | testimonials | case-study | none | multiple
8.  category_narrative   — The larger market story or movement this ad plugs into (1 sentence)
9.  ad_longevity_signal  — Already set; leave as-is (pass through from input)
10. creative_pattern     — ONE of: founder-video | static-graphic | ugc | meme | teardown | carousel | talking-head | testimonial-reel | other
11. messaging_angle      — ONE of: fear | speed | cost-savings | growth | simplicity | status | urgency | category-shift | transformation | social-proof
12. counter_positioning  — For a competing brand: one specific angle to take the opposite side of this ad's message (1-2 sentences)
13. content_opportunity  — One specific LinkedIn post, newsletter section, or short-form video this ad insight should produce (1 sentence)
14. outbound_angle       — One specific cold email or LinkedIn DM hook that a rep could use off the back of this competitive insight (1-2 sentences)

For fields 12-14, write as if YOU are a GTM operator at a competing coaching/performance brand advising your own sales and content team. Be specific and actionable, not generic.

If the creative_text is empty or too short to classify, return null for all taxonomy fields for that ad.
"""


def format_ads(ads: list[dict]) -> str:
    parts = []
    for i, ad in enumerate(ads, 1):
        parts.append(
            f"AD {i}\n"
            f"advertiser: {ad['advertiser_name']}\n"
            f"start_date: {ad.get('ad_start_date') or 'unknown'}\n"
            f"longevity: {ad.get('ad_longevity_signal') or 'unknown'}\n"
            f"landing_page: {ad.get('landing_page_url') or 'unknown'}\n"
            f"creative_text:\n{ad.get('creative_text') or ''}\n"
        )
    return "\n---\n".join(parts)


def classify_batch(ads: list[dict]) -> list[dict] | None:
    prompt = f"{SYSTEM_PROMPT}\n\n---\n\n{format_ads(ads)}\n\nReturn JSON array only."
    result = subprocess.run(
        ["claude", "-p", "--model", "sonnet"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        print(f"  ✗ claude error: {result.stderr[:200]}")
        return None

    raw = result.stdout.strip()
    start = raw.find("[")
    end   = raw.rfind("]") + 1
    if start < 0 or end <= start:
        print(f"  ✗ no JSON array in response:\n{raw[:300]}")
        return None

    try:
        return json.loads(raw[start:end])
    except json.JSONDecodeError as e:
        print(f"  ✗ JSON parse error: {e}")
        return None


TAXONOMY_COLS = [
    "primary_hook", "persona_targeted", "pain_point", "promised_outcome",
    "offer_type", "funnel_stage", "proof_used", "category_narrative",
    "ad_longevity_signal", "creative_pattern", "messaging_angle",
    "counter_positioning", "content_opportunity", "outbound_angle",
]


def write_classifications(conn, ad_id: int, classification: dict) -> None:
    sets = ", ".join(f"{col} = ?" for col in TAXONOMY_COLS) + ", classified_at = datetime('now')"
    vals = [classification.get(col) for col in TAXONOMY_COLS] + [ad_id]
    conn.execute(f"UPDATE meta_ads SET {sets} WHERE id = ?", vals)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--advertiser")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    conn = get_db()

    where = "classified_at IS NULL"
    params: list = []
    if args.advertiser:
        where += " AND advertiser_name = ?"
        params.append(args.advertiser)

    rows = conn.execute(
        f"SELECT id, advertiser_name, creative_text, ad_start_date, landing_page_url, ad_longevity_signal "
        f"FROM meta_ads WHERE {where} ORDER BY id LIMIT ?",
        params + [args.limit],
    ).fetchall()

    if not rows:
        print("No unclassified ads found.")
        return 0

    ads = [dict(r) for r in rows]
    print(f"Classifying {len(ads)} ads in batches of {BATCH_SIZE}...")

    classified = 0
    for i in range(0, len(ads), BATCH_SIZE):
        batch = ads[i : i + BATCH_SIZE]
        ids   = [a["id"] for a in batch]
        print(f"  batch {i // BATCH_SIZE + 1}: ad ids {ids}")

        if args.dry_run:
            print("  [dry-run] skipping claude call")
            continue

        results = classify_batch(batch)
        if results is None:
            print("  ✗ skipping batch due to error")
            continue

        if len(results) != len(batch):
            print(f"  ⚠ expected {len(batch)} results, got {len(results)}")

        for ad, cls in zip(batch, results):
            if cls is None:
                continue
            write_classifications(conn, ad["id"], cls)
            classified += 1

        conn.commit()

    print(f"\nDone. Classified {classified} ads.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
