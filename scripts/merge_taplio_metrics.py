"""
merge_taplio_metrics.py

Pulls every published post's real metrics (impressions, likes, comments,
shares) from Taplio's REST API and:
  1. Writes a raw export to src/_data/taplio_analytics_raw.csv (full detail,
     for auditing/debugging -- every post Taplio returned, matched or not).
  2. Merges reactions/comments/shares into src/_data/master.csv, matched to
     the existing rows by the numeric LinkedIn post ID embedded in the URL.

Built directly against Taplio_API_v1.json (the real OpenAPI spec), not
guessed:
  GET /v1/posts  -- paginated, each item already has both `url` (the real
  LinkedIn URL) and `metrics: {likes, comments, shares, impressions}`
  embedded, so this is the only endpoint needed. No separate join required.

Auth: Authorization: Bearer <api_key>, per the spec's ApiKey security scheme.

Usage:
  1. Create a file named .env in the repo root (same folder as this
     scripts/ directory sits inside) containing one line:
       TAPLIO_API_KEY=your-key-here
  2. Make sure .env is in your .gitignore (see setup note below) so it
     never gets committed.
  3. Run: python3 scripts/merge_taplio_metrics.py

The key is loaded from that local .env file (or, if you'd rather not use
one, a TAPLIO_API_KEY environment variable set in your shell works too).
It is never hardcoded here or committed to the repo -- same pattern as
everywhere else in this project.
"""

import csv
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import json
from pathlib import Path


def load_dotenv_manually(path: Path = Path(".env")):
    """Minimal .env loader -- no external dependency required. Reads
    KEY=value lines, skips blanks and #comments, only sets variables that
    aren't already set in the real environment (so an explicit `export`
    always wins over the .env file)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv_manually()

API_BASE = "https://api.taplio.com"
POSTS_ENDPOINT = "/v1/posts"

MASTER_PATH = Path("src/_data/master.csv")
RAW_EXPORT_PATH = Path("src/_data/taplio_analytics_raw.csv")

RAW_EXPORT_FIELDNAMES = [
    "taplio_id", "li_id", "url", "status", "created_at",
    "content_preview", "impressions", "likes", "comments", "shares",
]


def extract_li_id(url: str) -> str | None:
    """Pull the numeric LinkedIn post ID out of any of the URL shapes we've
    seen -- .../share-<digits>-..., .../ugcPost-<digits>-..., or
    urn:li:activity:<digits>. All three encode the same underlying ID."""
    if not url:
        return None
    m = re.search(r"(?:share|ugcPost)-(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"activity[:-](\d+)", url)
    if m:
        return m.group(1)
    return None


def api_request(path: str, api_key: str, params: dict) -> dict:
    """One GET request against the Taplio API, with basic 429 backoff and
    the error shapes documented in the spec (401/403/404/409/422/429/500/503
    all return the same ErrorEnvelope shape: {error: {code, message}, meta})."""
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{API_BASE}{path}?{qs}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        # Cloudflare (which sits in front of api.taplio.com) blocks Python's
        # default "Python-urllib/3.x" User-Agent as a known bot signature --
        # this is Cloudflare Error 1010 / browser_signature_banned, nothing
        # to do with your API key or subscription tier. A normal-looking
        # User-Agent avoids it.
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })

    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {}

            if parsed.get("cloudflare_error"):
                # Cloudflare's own block page, not a response from Taplio's
                # actual API -- the ErrorEnvelope schema never has this field.
                error_name = parsed.get("error_name", "unknown")
                print(f"ERROR: Cloudflare blocked this request before it reached Taplio (error_name: {error_name}).")
                print("This is almost always a User-Agent / bot-signature block, unrelated to your API key or subscription.")
                print("If this recurs, try rotating the User-Agent string in api_request(), or contact Taplio support to allowlist API client requests.")
                sys.exit(1)

            message = parsed.get("error", {}).get("message", body)
            code = parsed.get("error", {}).get("code", str(e.code))

            if e.code == 429 and attempt < 3:
                wait = 2 ** (attempt + 1)
                print(f"  Rate limited (429), waiting {wait}s before retry...")
                time.sleep(wait)
                continue
            if e.code == 401:
                print("ERROR: 401 Unauthorized -- your TAPLIO_API_KEY is missing or invalid.")
                print("Get a fresh one from Taplio: Settings > your profile > Integration.")
            elif e.code == 403:
                print(f"ERROR: 403 Forbidden ({code}) -- {message}")
                print("Your Taplio subscription tier may not include API analytics access.")
            else:
                print(f"ERROR: {e.code} {code} -- {message}")
            sys.exit(1)
        except urllib.error.URLError as e:
            print(f"ERROR: could not reach {API_BASE} -- {e.reason}")
            sys.exit(1)

    print("ERROR: exceeded retry attempts on rate limiting.")
    sys.exit(1)


def fetch_all_posts(api_key: str) -> list[dict]:
    """Paginate through every sent post via cursor-based pagination, per
    ListMeta's has_more/next_cursor shape."""
    posts = []
    cursor = None
    page = 1

    while True:
        print(f"Fetching page {page}...")
        data = api_request(POSTS_ENDPOINT, api_key, {
            "status": "sent",
            "limit": 50,
            "cursor": cursor,
        })
        batch = data.get("data", [])
        posts.extend(batch)
        print(f"  -> {len(batch)} posts (running total: {len(posts)})")

        meta = data.get("meta", {})
        if not meta.get("has_more"):
            break
        cursor = meta.get("next_cursor")
        if not cursor:
            break
        page += 1

    return posts


def write_raw_export(posts: list[dict]):
    RAW_EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RAW_EXPORT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RAW_EXPORT_FIELDNAMES)
        writer.writeheader()
        for p in posts:
            metrics = p.get("metrics") or {}
            writer.writerow({
                "taplio_id": p.get("id", ""),
                "li_id": extract_li_id(p.get("url", "")) or "",
                "url": p.get("url", ""),
                "status": p.get("status", ""),
                "created_at": p.get("created_at", ""),
                "content_preview": (p.get("content") or "")[:80].replace("\n", " "),
                "impressions": metrics.get("impressions", ""),
                "likes": metrics.get("likes", ""),
                "comments": metrics.get("comments", ""),
                "shares": metrics.get("shares", ""),
            })
    print(f"\nRaw export written: {RAW_EXPORT_PATH.resolve()} ({len(posts)} posts)")


def merge_into_master(posts: list[dict]):
    if not MASTER_PATH.exists():
        print(f"ERROR: {MASTER_PATH} not found. Run this from the repo root.")
        sys.exit(1)

    # Build a lookup: LinkedIn numeric ID -> metrics, skipping any Taplio
    # post with no resolvable ID or no metrics at all.
    by_li_id = {}
    for p in posts:
        li_id = extract_li_id(p.get("url", ""))
        metrics = p.get("metrics")
        if li_id and metrics:
            by_li_id[li_id] = metrics

    with open(MASTER_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    matched, unmatched_master_rows = 0, []
    for row in rows:
        li_id = extract_li_id(row.get("post_url", ""))
        metrics = by_li_id.get(li_id) if li_id else None
        if metrics:
            row["reactions"] = metrics.get("likes", row.get("reactions", ""))
            row["comments"] = metrics.get("comments", row.get("comments", ""))
            row["shares"] = metrics.get("shares", row.get("shares", ""))
            matched += 1
        else:
            unmatched_master_rows.append(row.get("post_topic", "")[:50])

    with open(MASTER_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nMerged into {MASTER_PATH.resolve()}:")
    print(f"  {matched} of {len(rows)} master.csv rows matched and updated")
    if unmatched_master_rows:
        print(f"  {len(unmatched_master_rows)} master.csv rows had no Taplio match (likely predate your Taplio usage):")
        for topic in unmatched_master_rows[:10]:
            print(f"    - {topic}")
        if len(unmatched_master_rows) > 10:
            print(f"    ... and {len(unmatched_master_rows) - 10} more")

    taplio_unmatched = len(posts) - matched
    if taplio_unmatched > 0:
        print(f"  Note: {len(posts)} total Taplio posts fetched, {matched} matched into master.csv -- "
              f"the rest are Taplio posts not currently tracked in master.csv.")


def main():
    api_key = os.environ.get("TAPLIO_API_KEY")
    if not api_key:
        print("ERROR: TAPLIO_API_KEY not found.")
        print("Create a .env file in the repo root with: TAPLIO_API_KEY=your-key-here")
        print("Get your key from Taplio: Settings > your profile > Integration.")
        sys.exit(1)

    print("Fetching all posts from Taplio...")
    posts = fetch_all_posts(api_key)
    print(f"\nTotal posts fetched: {len(posts)}")

    write_raw_export(posts)
    merge_into_master(posts)


if __name__ == "__main__":
    main()
