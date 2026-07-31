"""
classify_post_pillars.py

Reads master.csv, finds any row with an empty pillar, and uses an LLM to
classify it against contentiq_pillars.json's taxonomy. Which LLM provider
is used is controlled by contentiq.config.json's "classifier.active_model"
field -- gemini, claude, or openai today, with room to add more later
without touching this file's core logic, only the PROVIDERS registry
below.

Each provider's API call pattern was verified against real, current
documentation before being written here, not guessed:
  - Gemini: google-genai SDK, genai.Client(api_key=...).models.generate_content(...)
  - Claude: anthropic SDK, Anthropic(api_key=...).messages.create(...)
  - OpenAI: openai SDK, OpenAI(api_key=...).chat.completions.create(...),
    verified directly against the official openai-python GitHub README.

Usage:
  1. Create a .env file in the repo root containing the key for whichever
     provider is active in contentiq.config.json, e.g.:
       ANTHROPIC_API_KEY=your-key-here
  2. Make sure .env is in .gitignore.
  3. Run: python3 scripts/classify_post_pillars.py
"""

import csv
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PILLARS_PATH = Path(__file__).resolve().parent / "contentiq_pillars.json"
CONFIG_PATH = REPO_ROOT / "contentiq.config.json"
MASTER_PATH = REPO_ROOT / "src" / "_data" / "master.csv"

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


def load_classifier_config():
    if not CONFIG_PATH.exists():
        print(f"ERROR: {CONFIG_PATH} not found. Run this from the repo root.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    classifier = config.get("classifier")
    if not classifier:
        print(f"ERROR: no 'classifier' section in {CONFIG_PATH}.")
        sys.exit(1)

    active = classifier.get("active_model")
    models = classifier.get("models", {})
    if active not in models:
        print(f"ERROR: active_model '{active}' is not defined under classifier.models.")
        print(f"Valid options: {', '.join(models.keys())}")
        sys.exit(1)

    model_config = models[active]
    if not model_config.get("enabled"):
        print(f"ERROR: '{active}' is set as active_model but is not enabled.")
        note = model_config.get("notes", "")
        if note:
            print(f"Note: {note}")
        sys.exit(1)

    return active, model_config


# ---------------------------------------------------------------------------
# Provider registry -- each entry is (build_client, call_model).
# build_client(api_key) -> a provider-specific client object.
# call_model(client, model_name, prompt) -> raw response text string.
# Adding a new provider means adding one entry here, nothing else in this
# file needs to change.
# ---------------------------------------------------------------------------

def _build_gemini_client(api_key: str):
    from google import genai
    return genai.Client(api_key=api_key)


def _call_gemini(client, model_name: str, prompt: str) -> str:
    from google.genai import types
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.0),
    )
    return response.text or ""


def _build_claude_client(api_key: str):
    import anthropic
    return anthropic.Anthropic(api_key=api_key)


def _call_claude(client, model_name: str, prompt: str) -> str:
    response = client.messages.create(
        model=model_name,
        max_tokens=50,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text if response.content else ""


def _build_openai_client(api_key: str):
    from openai import OpenAI
    return OpenAI(api_key=api_key)


def _call_openai(client, model_name: str, prompt: str) -> str:
    response = client.chat.completions.create(
        model=model_name,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or ""


PROVIDERS = {
    "gemini": {"build_client": _build_gemini_client, "call_model": _call_gemini, "install": "google-genai"},
    "claude": {"build_client": _build_claude_client, "call_model": _call_claude, "install": "anthropic"},
    "openai": {"build_client": _build_openai_client, "call_model": _call_openai, "install": "openai"},
}


def classify_text(provider, client, model_name: str, text: str, categories: list, criteria_str: str, budget: CallBudget) -> str:
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
            raw = provider["call_model"](client, model_name, prompt)
            clean = raw.strip().strip("*`\"' \n")
            return clean if clean in categories else None
        except Exception as e:
            if attempt < 2:
                wait = 2 ** (attempt + 1)
                print(f"    {model_name} call failed ({e}), retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"    {model_name} call failed after 3 attempts: {e}")
                return None
    return None


def main():
    active_name, model_config = load_classifier_config()
    model_name = model_config["model_name"]
    api_key_env = model_config["api_key_env"]

    api_key = os.environ.get(api_key_env)
    if not api_key:
        print(f"ERROR: {api_key_env} not found for active classifier model '{active_name}'.")
        print(f"Create a .env file in the repo root with: {api_key_env}=your-key-here")
        sys.exit(1)

    provider = PROVIDERS.get(active_name)
    if not provider:
        print(f"ERROR: no provider implementation registered for '{active_name}'.")
        print(f"Known providers: {', '.join(PROVIDERS.keys())}")
        sys.exit(1)

    try:
        client = provider["build_client"](api_key)
    except ImportError:
        print(f"ERROR: {provider['install']} package not installed.")
        print(f"Run: pip install {provider['install']} --break-system-packages")
        sys.exit(1)

    if not MASTER_PATH.exists():
        print(f"ERROR: {MASTER_PATH} not found. Run this from the repo root.")
        sys.exit(1)

    categories, criteria_str = load_pillars_criteria(PILLARS_PATH)
    print(f"Loaded {len(categories)} pillars from {PILLARS_PATH}")
    print(f"Active classifier: {active_name} ({model_name})")

    with open(MASTER_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    missing = [r for r in rows if not r.get("pillar", "").strip()]
    print(f"Found {len(missing)} posts with missing pillars in {MASTER_PATH}.")

    if not missing:
        print("Nothing to classify.")
        return

    budget = CallBudget(int(os.environ.get("CLASSIFIER_DAILY_CALL_BUDGET", DEFAULT_DAILY_CALL_BUDGET)))

    updated = 0
    for row in missing:
        topic = (row.get("post_topic") or "").strip()
        if not topic:
            print(f"  Skipping row with no post_topic to classify: {row.get('post_url', '(no url)')}")
            continue

        print(f"  Classifying: {topic[:60]}")
        assigned = classify_text(provider, client, model_name, topic, categories, criteria_str, budget)
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
