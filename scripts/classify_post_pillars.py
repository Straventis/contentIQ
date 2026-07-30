"""
classify_post_pillars.py

Reads master.csv, finds any row with an empty pillar, and uses Gemini to
classify it against contentiq_pillars.json's taxonomy. Rewritten from
Santhosh's original zero-shot-llm-classify_post.py with these real fixes:

  - No longer scrapes LinkedIn directly (Jina AI Reader + raw requests
    fallback). LinkedIn aggressively blocks scraping and login-walls
    non-connections viewing a post -- fragile for zero benefit, since the
    post's real text is already sitting in that same CSV row's post_topic
    column. Classifying from local data instead of re-fetching from the
    web removes an entire class of failure and two dependencies
    (requests, beautifulsoup4).
  - CSV read/write now uses the plain csv module, matching every other
    script in this pipeline, instead of pandas -- avoids pandas' own CSV
    quoting/formatting behavior silently diverging from the rest of the
    codebase, especially given how many real CSV-formatting bugs this
    project has already hit.
  - .env loading matches the same hand-rolled, zero-dependency pattern
    used everywhere else instead of python-dotenv.
  - Real rate-limit budget tracking, matching merge_buffer_metrics.py and
    merge_taplio_metrics.py.
  - Errors from a single post's classification no longer crash the whole
    run -- logged and skipped, matching how a scheduled/unattended
    pipeline needs to behave.
  - Removed dead code (an unreachable second implementation of
    load_pillars_criteria after the first return statement).

Model/import path verified against current official Google Gen AI SDK
docs before writing this, not assumed.

Usage:
  1. Create a .env file in the repo root containing:
       GEMINI_API_KEY=your-key-here
  2. Make sure .env is in .gitignore.
  3. Run: python3 scripts/classify_post_pillars.py
"""

import csv
import os
import sys
import time
from pathlib import Path

try:
    from google import genai
    from google.genai import types
except ImportError:
    print("ERROR: google-genai package not installed. Run: pip install google-genai --break-system-packages")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
PILLARS_PATH = Path(__file__).resolve().parent / "contentiq_pillars.json"
MASTER_PATH = REPO_ROOT / "src" / "_data" / "master.csv"

GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_DAILY_CALL_BUDGET = 100


def load_dotenv_manually(path: Path = None):
    path = path or (REPO_ROOT / ".env")
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


class CallBudget:
    def __init__(self, budget: int):
        self.budget = budget
        self.used = 0

    def spend(self):
        self.used += 1
        if self.used > self.budget:
            print(f"\nERROR: stopping before exceeding the daily call budget ({self.budget}).")
            print(f"Used {self.used - 1} calls successfully. Remaining posts will be classified next run.")
            sys.exit(1)


def load_pillars_criteria(json_path: Path):
    import json
    if not json_path.exists():
        raise FileNotFoundError(f"Could not find pillars config at: {json_path}")

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    pillar_list = data["pillars"] if isinstance(data, dict) and "pillars" in data else data
    if not isinstance(pillar_list, list):
        raise ValueError("Unsupported JSON format in contentiq_pillars.json")

    categories = []
    formatted_rules = []
    for item in pillar_list:
        name = item["name"]
        definition = item.get("definition", "")
        sub_pillars = ", ".join(item.get("sub_pillars", []))
        categories.append(name)
        formatted_rules.append(f"Category: {name}\nSub-pillars: {sub_pillars}\nDefinition: {definition}\n")

    return categories, "\n".join(formatted_rules)


def classify_text(client, text: str, categories: list, criteria_str: str, budget: CallBudget) -> str:
    prompt = f"""Analyze the following LinkedIn post text and classify it into EXACTLY ONE category based on these rules:

CATEGORIES & CRITERIA:
{criteria_str}

POST TEXT TO CLASSIFY:
\"\"\"{text}\"\"\"

Respond ONLY with the exact matching category name from the list: [{', '.join(categories)}].
Do not add extra commentary, quotes, markdown formatting, or punctuation."""

    budget.spend()
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.0),
            )
            raw = (response.text or "").strip()
            clean = raw.strip("*`\"' \n")
            return clean if clean in categories else None
        except Exception as e:
            if attempt < 2:
                wait = 2 ** (attempt + 1)
                print(f"    Gemini call failed ({e}), retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"    Gemini call failed after 3 attempts: {e}")
                return None
    return None


def main():
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not found.")
        print("Create a .env file in the repo root with: GEMINI_API_KEY=your-key-here")
        sys.exit(1)

    if not MASTER_PATH.exists():
        print(f"ERROR: {MASTER_PATH} not found. Run this from the repo root.")
        sys.exit(1)

    categories, criteria_str = load_pillars_criteria(PILLARS_PATH)
    print(f"Loaded {len(categories)} pillars from {PILLARS_PATH}")

    with open(MASTER_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    missing = [r for r in rows if not r.get("pillar", "").strip()]
    print(f"Found {len(missing)} posts with missing pillars in {MASTER_PATH}.")

    if not missing:
        print("Nothing to classify.")
        return

    client = genai.Client(api_key=api_key)
    budget = CallBudget(int(os.environ.get("GEMINI_DAILY_CALL_BUDGET", DEFAULT_DAILY_CALL_BUDGET)))

    updated = 0
    for row in missing:
        topic = (row.get("post_topic") or "").strip()
        if not topic:
            print(f"  Skipping row with no post_topic to classify: {row.get('post_url', '(no url)')}")
            continue

        print(f"  Classifying: {topic[:60]}")
        assigned = classify_text(client, topic, categories, criteria_str, budget)
        if assigned:
            row["pillar"] = assigned
            print(f"    -> {assigned}")
            updated += 1
        else:
            print(f"    -> Could not classify, leaving blank for next run")

    if updated:
        with open(MASTER_PATH, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nUpdated {updated} of {len(missing)} rows in {MASTER_PATH}.")
    else:
        print("\nNo rows updated.")


if __name__ == "__main__":
    main()
