"""
merge_buffer_metrics.py

Pulls real post metrics (reactions, comments, shares, impressions) from
Buffer's GraphQL API and merges them into src/_data/master.csv. Built
directly against Buffer's real developer docs (developers.buffer.com),
not guessed.

IMPORTANT -- rate limits, corrected from what was assumed going in:
  Buffer's actual documented Free-plan limits are 100 requests per 15
  minutes, 100 per 24 hours, 3,000 per 30 days -- not 150/day. This script
  budgets against the real 100/24hr number, configurable via
  BUFFER_DAILY_CALL_BUDGET if your plan differs.

IMPORTANT -- these endpoints are marked "Preview"/"Experimental" in
  Buffer's own docs. Field shapes may change without backwards
  compatibility. Worth re-running connect_status() periodically to catch
  drift rather than assuming this stays correct forever.

IMPORTANT -- no confirmed LinkedIn URL field on Post in Buffer's public
  examples (id, text, dueAt, channelId, metrics were all that's shown).
  Matching to master.csv is therefore content-based only (same proven
  normalize/match logic as the Taplio script), not URL/URN-based. Run
  connect_status() first -- its schema dump will show definitively whether
  a url-shaped field actually exists that the docs examples just didn't
  surface, in which case the merge logic below should be upgraded to use
  it.

Usage:
  1. Create a .env file in the repo root containing:
       BUFFER_API_KEY=your-key-here
     Get it from: https://publish.buffer.com/settings/api
  2. Make sure .env is in .gitignore.
  3. One-time setup / troubleshooting only:
       python3 scripts/merge_buffer_metrics.py --connect-status
     Validates the key, dumps the full GraphQL schema, your organizations,
     channels, and a small sample of real posts with every discoverable
     field to buffer_schema_dump.json. Not part of the regular merge run --
     keep this around for troubleshooting, or delete it once things are
     confirmed working.
  4. Normal run:
       python3 scripts/merge_buffer_metrics.py
"""

import csv
import datetime
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
import urllib.error
from pathlib import Path

API_ENDPOINT = "https://api.buffer.com"
MASTER_PATH = Path("src/_data/master.csv")
RAW_EXPORT_PATH = Path("src/_data/buffer_analytics_raw.csv")
SCHEMA_DUMP_PATH = Path("buffer_schema_dump.json")

# Buffer's real documented Free-plan limit is 100/24hr, not the 150 that
# was assumed going in -- budgeting against the confirmed number. Override
# with BUFFER_DAILY_CALL_BUDGET in .env if your plan is Essentials/Team.
DEFAULT_DAILY_CALL_BUDGET = 100

RAW_EXPORT_FIELDNAMES = [
    "buffer_post_id", "channel_id", "created_at", "content_preview",
    "impressions", "reactions", "comments", "shares", "external_link", "share_mode",
]

BROWSER_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
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


def normalize_text(s: str) -> str:
    """Same proven normalizer from the Taplio script: NFKD first so
    LinkedIn's Unicode "mathematical bold" styled text (e.g. "𝗠𝗶𝗰𝗿𝗼𝘀𝗼𝗳𝘁")
    converts back to plain letters instead of being stripped as
    non-ASCII, apostrophes removed entirely (not turned into a space) so
    "Didn't" and "Didnt" normalize the same way."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = s.lower()
    s = re.sub(r"['\u2019\u2018]", "", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def extract_li_id(url: str):
    """Pull the numeric LinkedIn activity/share ID out of a post URL, so
    two different URL formats pointing at the same post (e.g. Buffer's
    externalLink vs. what's already in master.csv's post_url) can be
    matched exactly instead of by fuzzy text. Handles the two common
    LinkedIn URL shapes:
      - .../feed/update/urn:li:activity:1234567890123456789
      - .../posts/username_slug-activity-1234567890123456789-abcd
    Falls back to None if neither pattern is found, so exact matching
    degrades gracefully to the fuzzy content match rather than erroring.
    """
    if not url:
        return None
    m = re.search(r"urn:li:(?:activity|share):(\d{10,20})", url)
    if m:
        return m.group(1)
    m = re.search(r"activity-(\d{10,20})", url)
    if m:
        return m.group(1)
    return None


class CallBudget:
    """Tracks GraphQL requests against the daily limit so a run stops
    cleanly instead of hitting a real 429 mid-pagination."""

    def __init__(self, budget: int):
        self.budget = budget
        self.used = 0

    def spend(self):
        self.used += 1
        if self.used > self.budget:
            print(f"\nERROR: stopping before exceeding the daily call budget ({self.budget}).")
            print(f"Used {self.used - 1} calls successfully before this one. Re-run tomorrow, or raise")
            print("BUFFER_DAILY_CALL_BUDGET in .env if your actual plan allows more.")
            sys.exit(1)
        remaining = self.budget - self.used
        if remaining in (10, 5, 1):
            print(f"  (call budget: {remaining} remaining today)")


def graphql_request(query: str, variables: dict, api_key: str, budget: CallBudget) -> dict:
    budget.spend()
    headers = dict(BROWSER_HEADERS)
    headers["Authorization"] = f"Bearer {api_key}"
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(API_ENDPOINT, data=payload, headers=headers, method="POST")

    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if "errors" in result:
                    msgs = "; ".join(e.get("message", str(e)) for e in result["errors"])
                    print(f"ERROR: GraphQL error(s): {msgs}")
                    sys.exit(1)
                return result.get("data", {})
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 429:
                try:
                    parsed = json.loads(body)
                    retry_after = parsed["errors"][0]["extensions"].get("retryAfter", 60)
                except Exception:
                    retry_after = 60
                if attempt < 3:
                    print(f"  Rate limited (429), waiting {retry_after}s before retry...")
                    time.sleep(retry_after)
                    continue
            if e.code == 401:
                print("ERROR: 401 Unauthorized -- your BUFFER_API_KEY is missing or invalid.")
                print("Get a fresh one from: https://publish.buffer.com/settings/api")
            else:
                print(f"ERROR: {e.code} -- {body}")
            sys.exit(1)
        except urllib.error.URLError as e:
            print(f"ERROR: could not reach {API_ENDPOINT} -- {e.reason}")
            sys.exit(1)

    print("ERROR: exceeded retry attempts on rate limiting.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# connect_status(): one-time diagnostic / validation function.
# Run manually with --connect-status. Not part of the normal merge flow.
# ---------------------------------------------------------------------------

INTROSPECT_TYPE_QUERY = """
query IntrospectType($typeName: String!) {
  __type(name: $typeName) {
    name
    fields {
      name
      type {
        name
        kind
        ofType { name kind }
      }
    }
  }
}
"""


def connect_status(api_key: str):
    budget = CallBudget(int(os.environ.get("BUFFER_DAILY_CALL_BUDGET", DEFAULT_DAILY_CALL_BUDGET)))
    dump = {"run_at": datetime.datetime.now().isoformat()}

    print("Step 1/4: validating the API key and fetching your account + organizations...")
    data = graphql_request(
        "query { account { id organizations { id name } } }", {}, api_key, budget
    )
    dump["account"] = data.get("account")
    print(f"  Connected. Account id: {data.get('account', {}).get('id')}")
    orgs = data.get("account", {}).get("organizations", [])
    print(f"  Organizations found: {len(orgs)}")
    for o in orgs:
        print(f"    - {o['name']} ({o['id']})")

    if not orgs:
        print("No organizations found -- nothing further to inspect.")
        write_schema_dump(dump)
        return

    org_id = orgs[0]["id"]
    print(f"\nStep 2/4: fetching channels for organization {orgs[0]['name']}...")
    data = graphql_request(
        "query($orgId: OrganizationId!) { channels(input: { organizationId: $orgId }) { id name service } }",
        {"orgId": org_id}, api_key, budget,
    )
    channels = data.get("channels", [])
    dump["channels"] = channels
    print(f"  Channels found: {len(channels)}")
    for c in channels:
        print(f"    - {c['name']} ({c['service']}) id={c['id']}")
    linkedin_channels = [c for c in channels if c.get("service") == "linkedin"]
    print(f"  LinkedIn channels: {len(linkedin_channels)}")

    print("\nStep 3/4: introspecting the Post type's full field list...")
    data = graphql_request(INTROSPECT_TYPE_QUERY, {"typeName": "Post"}, api_key, budget)
    post_type = data.get("__type")
    dump["post_type_schema"] = post_type
    field_names = [f["name"] for f in (post_type or {}).get("fields", [])]
    print(f"  Post type has {len(field_names)} fields: {', '.join(field_names)}")
    has_url_field = any("url" in f.lower() or "link" in f.lower() or "permalink" in f.lower() for f in field_names)
    print(f"  URL-shaped field present: {has_url_field}")
    if has_url_field:
        print("  -> A URL field exists! Consider upgrading merge_into_master() to match by")
        print("     LinkedIn ID instead of content text -- see extract_li_id() in the Taplio script")
        print("     for the regex pattern to adapt.")

    if linkedin_channels:
        print(f"\nStep 4/4: pulling a small sample of real posts (first 5) with every scalar field...")
        scalar_fields = [
            f["name"] for f in (post_type or {}).get("fields", [])
            if (f.get("type") or {}).get("kind") in ("SCALAR", "ENUM")
            or ((f.get("type") or {}).get("ofType") or {}).get("kind") in ("SCALAR", "ENUM")
        ]
        # Always include metrics explicitly since it's an object field the
        # scalar filter above would otherwise skip.
        field_selection = "\n        ".join(scalar_fields) + "\n        metrics { type name value unit }\n        metricsUpdatedAt"
        sample_query = f"""
        query($orgId: OrganizationId!, $channelId: ChannelId!) {{
          posts(first: 5, input: {{ organizationId: $orgId, filter: {{ status: [sent], channelIds: [$channelId] }} }}) {{
            edges {{ node {{ {field_selection} }} }}
            pageInfo {{ endCursor hasNextPage }}
          }}
        }}
        """
        data = graphql_request(
            sample_query,
            {"orgId": org_id, "channelId": linkedin_channels[0]["id"]},
            api_key, budget,
        )
        sample_posts = [e["node"] for e in data.get("posts", {}).get("edges", [])]
        dump["sample_linkedin_posts"] = sample_posts
        print(f"  Sample posts pulled: {len(sample_posts)}")
    else:
        print("\nStep 4/4: skipped -- no LinkedIn channel connected to this Buffer account yet.")

    dump["calls_used"] = budget.used
    write_schema_dump(dump)
    print(f"\nTotal API calls used this run: {budget.used} of {budget.budget} daily budget.")
    print(f"Full dump written to {SCHEMA_DUMP_PATH.resolve()} -- review it, then decide whether")
    print("to keep this function around for troubleshooting or strip it out.")


def write_schema_dump(dump: dict):
    SCHEMA_DUMP_PATH.write_text(json.dumps(dump, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# Normal merge flow
# ---------------------------------------------------------------------------

def fetch_linkedin_metrics(api_key: str, budget: CallBudget) -> list[dict]:
    data = graphql_request("query { account { organizations { id name } } }", {}, api_key, budget)
    orgs = data.get("account", {}).get("organizations", [])
    if not orgs:
        print("ERROR: no organizations found on this Buffer account.")
        sys.exit(1)
    org_id = orgs[0]["id"]

    data = graphql_request(
        "query($orgId: OrganizationId!) { channels(input: { organizationId: $orgId }) { id name service } }",
        {"orgId": org_id}, api_key, budget,
    )
    linkedin_channels = [c for c in data.get("channels", []) if c.get("service") == "linkedin"]
    if not linkedin_channels:
        print("ERROR: no LinkedIn channel connected to this Buffer account.")
        sys.exit(1)
    channel_ids = [c["id"] for c in linkedin_channels]
    print(f"LinkedIn channels: {[c['name'] for c in linkedin_channels]}")

    posts_query = """
    query($orgId: OrganizationId!, $channelIds: [ChannelId!], $after: String) {
      posts(
        first: 50
        after: $after
        input: { organizationId: $orgId, filter: { status: [sent], channelIds: $channelIds } }
      ) {
        edges {
          node {
            id
            text
            dueAt
            sentAt
            channelId
            externalLink
            shareMode
            metrics { type name value unit }
            metricsUpdatedAt
          }
        }
        pageInfo { endCursor hasNextPage }
      }
    }
    """

    all_posts, cursor, page = [], None, 1
    while True:
        print(f"  Fetching posts page {page}...")
        data = graphql_request(posts_query, {"orgId": org_id, "channelIds": channel_ids, "after": cursor}, api_key, budget)
        edges = data.get("posts", {}).get("edges", [])
        all_posts.extend(e["node"] for e in edges)
        print(f"    -> {len(edges)} posts (running total: {len(all_posts)})")
        page_info = data.get("posts", {}).get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break
        page += 1

    # Buffer's own feed genuinely contains near-duplicate entries for the
    # same real post -- confirmed against real data: identical content,
    # different IDs, sometimes as little as 20 seconds apart. Left
    # unhandled, this creates duplicate rows in master.csv once the first
    # duplicate claims an existing row and the second has nothing left to
    # match, getting wrongly added as a "new" post. Dedupe by normalized
    # content here, before any matching happens, keeping whichever entry
    # has the higher impressions (the more complete/later-synced one).
    deduped = {}
    for p in all_posts:
        key = normalize_text((p.get("text") or "")[:100])
        if not key:
            continue
        existing = deduped.get(key)
        if not existing or extract_metric(p.get("metrics"), "impressions") > extract_metric(existing.get("metrics"), "impressions"):
            deduped[key] = p
    removed = len(all_posts) - len(deduped)
    if removed:
        print(f"  Deduplicated {removed} near-duplicate entries from Buffer's own feed (same content, different IDs).")

    return list(deduped.values())


def extract_metric(metrics: list, metric_type: str) -> float:
    for m in metrics or []:
        if m.get("type") == metric_type:
            return m.get("value", 0)
    return 0  # absent means the network hasn't reported it, per Buffer's own docs -- not necessarily true zero


def write_raw_export(posts: list[dict]):
    RAW_EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RAW_EXPORT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RAW_EXPORT_FIELDNAMES)
        writer.writeheader()
        for p in posts:
            writer.writerow({
                "buffer_post_id": p.get("id", ""),
                "channel_id": p.get("channelId", ""),
                "created_at": p.get("sentAt") or p.get("dueAt", ""),
                "content_preview": (p.get("text") or "")[:200].replace("\n", " "),
                "impressions": extract_metric(p.get("metrics"), "impressions"),
                "reactions": extract_metric(p.get("metrics"), "reactions"),
                "comments": extract_metric(p.get("metrics"), "comments"),
                "shares": extract_metric(p.get("metrics"), "shares"),
                "external_link": p.get("externalLink", ""),
                "share_mode": p.get("shareMode", ""),
            })
    print(f"\nRaw export written: {RAW_EXPORT_PATH.resolve()} ({len(posts)} posts)")


# Best-effort mapping from Buffer's shareMode to master.csv's existing
# content_type convention. Unverified against real values yet -- Buffer's
# actual enum wasn't visible in the schema dump the way scalar fields
# were, so this defaults unrecognized values to "Original" rather than
# guessing wrong. First real run's output should be checked against
# actual shareMode values seen and this mapping adjusted if needed.
SHARE_MODE_TO_CONTENT_TYPE = {
    "REPOST": "Repost (plain)",
    "SHARE": "Repost (plain)",
    "REPOST_WITH_COMMENTARY": "Repost+Commentary",
    "QUOTE": "Repost+Commentary",
    "ORIGINAL": "Original",
    None: "Original",
    "": "Original",
}


def format_buffer_date(iso_str: str) -> str:
    """Buffer's ISO8601 timestamps -> master.csv's existing M/D/YYYY
    convention (no zero-padding), matching what's already in the file."""
    if not iso_str:
        return ""
    try:
        dt = datetime.datetime.strptime(iso_str[:10], "%Y-%m-%d")
        return f"{dt.month}/{dt.day}/{dt.year}"
    except ValueError:
        return ""


def merge_into_master(posts: list[dict]):
    if not MASTER_PATH.exists():
        print(f"ERROR: {MASTER_PATH} not found. Run this from the repo root.")
        sys.exit(1)

    enriched = [{
        "id": p.get("id", ""),
        "li_id": extract_li_id(p.get("externalLink", "")),
        "content_preview": (p.get("text") or "")[:200],
        "reactions": extract_metric(p.get("metrics"), "reactions"),
        "comments": extract_metric(p.get("metrics"), "comments"),
        "shares": extract_metric(p.get("metrics"), "shares"),
        "impressions": extract_metric(p.get("metrics"), "impressions"),
        "share_mode": p.get("shareMode"),
        "date": format_buffer_date(p.get("sentAt") or p.get("dueAt")),
        "external_link": p.get("externalLink", ""),
    } for p in posts]

    by_li_id = {e["li_id"]: e for e in enriched if e["li_id"]}
    by_content = [(normalize_text(e["content_preview"]), e) for e in enriched if e["content_preview"]]
    claimed = set()
    MIN_FUZZY_MATCH_LENGTH = 20

    with open(MASTER_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    matched_by_id, matched_by_content, skipped_too_short, unmatched = 0, 0, [], []

    for row in rows:
        # Exact match first, via the URL's LinkedIn ID -- much more
        # reliable than content matching, and immune to the
        # same-opening-template-phrase collisions found in real data
        # (e.g. six separate "I'm happy to share..." certification posts).
        row_li_id = extract_li_id(row.get("post_url", ""))
        entry = None
        if row_li_id and row_li_id in by_li_id and by_li_id[row_li_id]["id"] not in claimed:
            entry = by_li_id[row_li_id]
            claimed.add(entry["id"])
            matched_by_id += 1

        if not entry:
            topic_norm = normalize_text(row.get("post_topic", ""))
            if len(topic_norm) < MIN_FUZZY_MATCH_LENGTH:
                if topic_norm:
                    skipped_too_short.append(row.get("post_topic", "")[:50])
                continue
            for content_norm, candidate in by_content:
                if candidate["id"] in claimed:
                    continue
                if topic_norm in content_norm[:len(topic_norm) + 100]:
                    entry = candidate
                    claimed.add(candidate["id"])
                    matched_by_content += 1
                    break

        if entry:
            row["reactions"] = entry["reactions"]
            row["comments"] = entry["comments"]
            row["shares"] = entry["shares"]
        else:
            unmatched.append(row.get("post_topic", "")[:50])

    # Any Buffer post not claimed by an existing row is a genuinely new
    # post -- add it rather than silently dropping it, which is what was
    # happening before (new posts simply never appeared until the next
    # manual LinkedIn export).
    new_rows = []
    for e in enriched:
        if e["id"] in claimed or not e["content_preview"].strip():
            continue
        content_type = SHARE_MODE_TO_CONTENT_TYPE.get(e["share_mode"], "Original")
        total_engagements = e["reactions"] + e["comments"] + e["shares"]
        engagement_rate = round(total_engagements / e["impressions"], 4) if e["impressions"] else 0
        new_rows.append({
            "date": e["date"],
            "post_url": e["external_link"],
            "post_topic": e["content_preview"][:80].strip(),
            "content_type": content_type,
            "pillar": "",  # picked up automatically by classify_post_pillars.py, run right after this
            "impressions": e["impressions"],
            "reactions": e["reactions"],
            "comments": e["comments"],
            "shares": e["shares"],
            "total_engagements": total_engagements,
            "engagement_rate": engagement_rate,
            "new_followers": 0,
        })
        print(f"  [new] \"{e['content_preview'][:50]}\" (shareMode={e['share_mode']!r} -> {content_type})")

    rows.extend(new_rows)

    with open(MASTER_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nMerged into {MASTER_PATH.resolve()}:")
    print(f"  {matched_by_id} rows matched exactly by LinkedIn URL ID")
    print(f"  {matched_by_content} rows matched by fuzzy content fallback")
    print(f"  {len(new_rows)} new posts added")
    if skipped_too_short:
        print(f"  {len(skipped_too_short)} existing rows skipped (topic too short/generic to match safely)")
    if unmatched:
        print(f"  {len(unmatched)} existing rows had no match at all:")
        for topic in unmatched[:10]:
            print(f"    - {topic}")
        if len(unmatched) > 10:
            print(f"    ... and {len(unmatched) - 10} more")


def main():
    api_key = os.environ.get("BUFFER_API_KEY")
    if not api_key:
        print("ERROR: BUFFER_API_KEY not found.")
        print("Create a .env file in the repo root with: BUFFER_API_KEY=your-key-here")
        print("Get it from: https://publish.buffer.com/settings/api")
        sys.exit(1)

    if "--connect-status" in sys.argv:
        connect_status(api_key)
        return

    budget = CallBudget(int(os.environ.get("BUFFER_DAILY_CALL_BUDGET", DEFAULT_DAILY_CALL_BUDGET)))
    posts = fetch_linkedin_metrics(api_key, budget)
    print(f"\nTotal LinkedIn posts fetched: {len(posts)} (used {budget.used} API calls)")

    write_raw_export(posts)
    merge_into_master(posts)


if __name__ == "__main__":
    main()
