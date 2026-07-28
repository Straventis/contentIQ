"""
merge_taplio_metrics.py

Pulls every post's real metrics (impressions, likes, comments, shares) from
Taplio's REST API and:
  1. Writes a raw export to src/_data/taplio_analytics_raw.csv (full detail,
     for auditing/debugging -- every post Taplio returned, matched or not).
  2. Merges reactions/comments/shares into src/_data/master.csv.

Built directly against Taplio_API_v1.json (the real OpenAPI spec).

IMPORTANT -- why two endpoints are used:
  GET /v1/posts covers only posts actually composed/scheduled/sent *through
  Taplio's own interface* -- for an account that mostly writes and publishes
  natively on LinkedIn, this can be as small as 1 post, which is what
  happened on the first real run of this script. That's not a bug, it's the
  wrong endpoint for what we want.

  GET /v1/analytics/posts ("Per-post analytics") is the one that syncs the
  full connected LinkedIn history's metrics, regardless of where the post
  was originally written -- this is the actual data source we want, and
  matches what a live pull via Claude's own Taplio connection returned
  earlier (many real posts, not one).

  The catch: /v1/analytics/posts's response has NO LinkedIn url field, only
  `id`, `content`, `created_at`, `impressions`, `likes`, `comments`, `shares`.
  So matching to master.csv (which is keyed by LinkedIn post URL) happens in
  two passes:
    Pass 1 (exact): bridge through /v1/posts's `id` -> `url` mapping, in
      case a given analytics post's `id` also exists as a Post object with
      a URL attached (best case, exact ID match).
    Pass 2 (fallback): for anything Pass 1 doesn't resolve, match by
      comparing master.csv's post_topic against the analytics post's
      content text -- normalized, checked as a substring match. post_topic
      is generally a short opening-line/headline snippet of the full post,
      so this is a reasonably reliable heuristic, just not a guaranteed one.
      Every fallback match is logged so you can eyeball whether it's right.

Auth: Authorization: Bearer <api_key>, per the spec's ApiKey security scheme.

Usage:
  1. Create a .env file in the repo root containing:
       TAPLIO_API_KEY=your-key-here
  2. Make sure .env is in .gitignore.
  3. Run: python3 scripts/merge_taplio_metrics.py
"""

import csv
import datetime
import os
import re
import sys
import time
import unicodedata
import urllib.request
import urllib.error
import urllib.parse
import json
from pathlib import Path

API_BASE = "https://api.taplio.com"
ANALYTICS_POSTS_ENDPOINT = "/v1/analytics/posts"
POSTS_ENDPOINT = "/v1/posts"

MASTER_PATH = Path("src/_data/master.csv")
RAW_EXPORT_PATH = Path("src/_data/taplio_analytics_raw.csv")

RAW_EXPORT_FIELDNAMES = [
    "taplio_id", "li_id", "match_method", "url", "created_at",
    "content_preview", "impressions", "likes", "comments", "shares",
]

BROWSER_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def load_dotenv_manually(path: Path = Path(".env")):
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


def extract_li_id(url: str) -> str | None:
    if not url:
        return None
    m = re.search(r"(?:share|ugcPost)-(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"activity[:-](\d+)", url)
    if m:
        return m.group(1)
    return None


def normalize_text(s: str) -> str:
    """Normalize for matching. Critically: NFKD-normalize first, since
    LinkedIn posts routinely use Unicode "mathematical alphanumeric" bold/
    italic styled letters (e.g. Microsoft written as "𝗠𝗶𝗰𝗿𝗼𝘀𝗼𝗳𝘁") for
    emphasis -- without this step those styled characters get stripped
    entirely as "non-ASCII" instead of converting back to plain letters,
    silently destroying the match."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = s.lower()
    # Apostrophes/smart quotes removed entirely (not turned into a space) so
    # "Didn't" and "Didnt" normalize to the same "didnt" -- master.csv's
    # topics and Taplio's live content don't always agree on whether
    # contractions keep their apostrophe.
    s = re.sub(r"['\u2019\u2018]", "", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def api_request(path: str, api_key: str, params: dict) -> dict:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{API_BASE}{path}?{qs}"
    headers = dict(BROWSER_HEADERS)
    headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)

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
                error_name = parsed.get("error_name", "unknown")
                print(f"ERROR: Cloudflare blocked this request before it reached Taplio (error_name: {error_name}).")
                print("This is a User-Agent / bot-signature block, unrelated to your API key or subscription.")
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
            elif e.code == 403:
                print(f"ERROR: 403 Forbidden ({code}) -- {message}")
            else:
                print(f"ERROR: {e.code} {code} -- {message}")
            sys.exit(1)
        except urllib.error.URLError as e:
            print(f"ERROR: could not reach {API_BASE} -- {e.reason}")
            sys.exit(1)

    print("ERROR: exceeded retry attempts on rate limiting.")
    sys.exit(1)


def paginate(endpoint: str, api_key: str, extra_params: dict) -> list[dict]:
    items = []
    cursor = None
    page = 1
    while True:
        print(f"  Fetching {endpoint} page {page}...")
        params = dict(extra_params)
        params["limit"] = 50
        params["cursor"] = cursor
        data = api_request(endpoint, api_key, params)
        batch = data.get("data", [])
        items.extend(batch)
        print(f"    -> {len(batch)} items (running total: {len(items)})")
        meta = data.get("meta", {})
        if not meta.get("has_more"):
            break
        cursor = meta.get("next_cursor")
        if not cursor:
            break
        page += 1
    return items


def fetch_url_bridge(api_key: str) -> dict:
    date_from, date_to = "2015-01-01", datetime.date.today().isoformat()
    print("Fetching /v1/posts (for the id -> url bridge)...")
    posts = paginate(POSTS_ENDPOINT, api_key, {"from": date_from, "to": date_to})
    return {p["id"]: p.get("url", "") for p in posts if p.get("url")}


def fetch_analytics_posts(api_key: str) -> list[dict]:
    date_from, date_to = "2015-01-01", datetime.date.today().isoformat()
    print("Fetching /v1/analytics/posts (the real data source)...")
    return paginate(ANALYTICS_POSTS_ENDPOINT, api_key, {"from": date_from, "to": date_to})


def write_raw_export(enriched: list[dict]):
    RAW_EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RAW_EXPORT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RAW_EXPORT_FIELDNAMES)
        writer.writeheader()
        for item in enriched:
            writer.writerow(item)
    print(f"\nRaw export written: {RAW_EXPORT_PATH.resolve()} ({len(enriched)} posts)")


def merge_into_master(enriched: list[dict]):
    if not MASTER_PATH.exists():
        print(f"ERROR: {MASTER_PATH} not found. Run this from the repo root.")
        sys.exit(1)

    by_li_id = {e["li_id"]: e for e in enriched if e["li_id"]}
    by_content = [(normalize_text(e["content_preview"]), e) for e in enriched if e["content_preview"]]
    claimed_taplio_ids = set()

    # Minimum normalized-topic length before attempting a content match.
    # Short/generic topics like "Open to Work" (no suffix) are substrings
    # of many longer, unrelated posts -- matching on those produces false
    # positives. Longer topics are specific enough to be a reliable signal.
    MIN_FUZZY_MATCH_LENGTH = 20

    with open(MASTER_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    matched_exact, matched_fuzzy, unmatched, skipped_too_short = 0, 0, [], []

    for row in rows:
        li_id = extract_li_id(row.get("post_url", ""))
        entry = by_li_id.get(li_id) if li_id else None
        method = "exact_id" if entry else None

        if not entry:
            topic_norm = normalize_text(row.get("post_topic", ""))
            if len(topic_norm) < MIN_FUZZY_MATCH_LENGTH:
                if topic_norm:
                    skipped_too_short.append(row.get("post_topic", "")[:50])
            else:
                for content_norm, candidate in by_content:
                    if candidate["taplio_id"] in claimed_taplio_ids:
                        continue  # this Taplio post already matched a different row
                    if topic_norm in content_norm[:len(topic_norm) + 100]:
                        entry = candidate
                        method = "fuzzy_content"
                        claimed_taplio_ids.add(candidate["taplio_id"])
                        break

        if entry:
            row["reactions"] = entry["likes"]
            row["comments"] = entry["comments"]
            row["shares"] = entry["shares"]
            if method == "exact_id":
                matched_exact += 1
            else:
                matched_fuzzy += 1
                print(f"  [fuzzy match] \"{row.get('post_topic','')[:50]}\" <- \"{entry['content_preview'][:50]}\"")
        else:
            unmatched.append(row.get("post_topic", "")[:50])

    with open(MASTER_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    total_matched = matched_exact + matched_fuzzy
    print(f"\nMerged into {MASTER_PATH.resolve()}:")
    print(f"  {total_matched} of {len(rows)} rows matched ({matched_exact} exact by ID, {matched_fuzzy} by content match)")
    if skipped_too_short:
        print(f"  {len(skipped_too_short)} rows skipped for fuzzy matching (topic too short/generic to match safely):")
        for topic in skipped_too_short[:5]:
            print(f"    - {topic}")
        if len(skipped_too_short) > 5:
            print(f"    ... and {len(skipped_too_short) - 5} more")
    if unmatched:
        print(f"  {len(unmatched)} rows had no match at all:")
        for topic in unmatched[:10]:
            print(f"    - {topic}")
        if len(unmatched) > 10:
            print(f"    ... and {len(unmatched) - 10} more")


def main():
    api_key = os.environ.get("TAPLIO_API_KEY")
    if not api_key:
        print("ERROR: TAPLIO_API_KEY not found.")
        print("Create a .env file in the repo root with: TAPLIO_API_KEY=your-key-here")
        sys.exit(1)

    url_bridge = fetch_url_bridge(api_key)
    analytics_posts = fetch_analytics_posts(api_key)
    print(f"\nTotal analytics posts fetched: {len(analytics_posts)}")

    enriched = []
    for p in analytics_posts:
        url = url_bridge.get(p["id"], "")
        li_id = extract_li_id(url)
        enriched.append({
            "taplio_id": p.get("id", ""),
            "li_id": li_id or "",
            "match_method": "id_bridge" if li_id else "",
            "url": url,
            "created_at": p.get("created_at", ""),
            "content_preview": (p.get("content") or "")[:200].replace("\n", " "),
            "impressions": p.get("impressions", 0),
            "likes": p.get("likes", 0),
            "comments": p.get("comments", 0),
            "shares": p.get("shares", 0),
        })

    write_raw_export(enriched)
    merge_into_master(enriched)


if __name__ == "__main__":
    main()
