import os
import re
import json
import time
import requests
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# Path & Environment Setup
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

# Attempt loading .env from repo root, then script dir
ENV_PATH_ROOT = REPO_ROOT / ".env"
ENV_PATH_LOCAL = SCRIPT_DIR / ".env"

if ENV_PATH_ROOT.exists():
    load_dotenv(dotenv_path=ENV_PATH_ROOT)
elif ENV_PATH_LOCAL.exists():
    load_dotenv(dotenv_path=ENV_PATH_LOCAL)
else:
    load_dotenv()  # Fallback to system env vars

# Verify API key is present
api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
if not api_key:
    raise ValueError(
        f"No Gemini API key found! Please ensure GEMINI_API_KEY is set in environment or .env"
    )

# Initialize Gemini Client explicitly
client = genai.Client(api_key=api_key)

PILLARS_FILE = os.path.join(SCRIPT_DIR, "contentiq_pillars.json")
CSV_FILE = os.path.join(REPO_ROOT, "src", "_data", "master.csv")


# ---------------------------------------------------------------------------
# Core Functions
# ---------------------------------------------------------------------------
def load_pillars_criteria(json_file):
    """Load pillar rules and categories from contentiq_pillars.json."""
    if not os.path.exists(json_file):
        raise FileNotFoundError(f"Could not find pillars config at: {json_file}")
        
    with open(json_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    categories = []
    formatted_rules = []

    # Handle {"pillars": [{"name": "...", "definition": "..."}, ...]}
    if isinstance(data, dict) and "pillars" in data:
        pillar_list = data["pillars"]
    elif isinstance(data, list):
        pillar_list = data
    else:
        raise ValueError("Unsupported JSON format in contentiq_pillars.json")

    for item in pillar_list:
        if isinstance(item, dict):
            name = item.get("name")
            definition = item.get("definition", "")
            sub_pillars = ", ".join(item.get("sub_pillars", []))
            
            categories.append(name)
            formatted_rules.append(f"Category: {name}\nSub-pillars: {sub_pillars}\nDefinition: {definition}\n")
        elif isinstance(item, str):
            categories.append(item)
            formatted_rules.append(f"- {item}")

    criteria_str = "\n".join(formatted_rules)
    return categories, criteria_str    

    if not os.path.exists(json_file):
        raise FileNotFoundError(f"Could not find pillars config at: {json_file}")
        
    with open(json_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # Handle list vs dict configuration
    if isinstance(data, list):
        categories = data
        criteria_str = "\n".join([f"- {c}" for c in categories])
    elif isinstance(data, dict):
        categories = list(data.keys())
        criteria_str = json.dumps(data, indent=2)
    else:
        raise ValueError("Unsupported JSON format in contentiq_pillars.json")
        
    return categories, criteria_str


def fetch_linkedin_text(url):
    """Fetch text content from a public LinkedIn post with fallback strategies."""
    
    # Strategy 1: Try Jina AI Reader
    try:
        jina_url = f"https://r.jina.ai/{url}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "X-With-Generated-Alt": "true"
        }
        response = requests.get(jina_url, headers=headers, timeout=15)
        if response.status_code == 200 and len(response.text.strip()) > 100:
            if "Forbidden" not in response.text and "Captcha" not in response.text:
                return response.text[:3000]
    except Exception as e:
        print(f"  [Jina fetch failed]: {e}")

    # Strategy 2: Fallback to Meta Description Scraping
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9"
        }
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            og_desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
            if og_desc and og_desc.get("content"):
                desc_text = og_desc["content"].strip()
                if len(desc_text) > 20:
                    print("  [Fallback used]: Extracted text from Meta Tags.")
                    return desc_text
    except Exception as e:
        print(f"  [Meta tag fallback failed]: {e}")

    return None


def classify_text(text, categories, criteria_str):
   import time

def classify_text(text, categories, criteria_str):
    """Classify text using active Gemini models with backoff retry logic."""
    prompt = f"""
    Analyze the following LinkedIn post text and classify it into EXACTLY ONE category based on these rules/criteria from contentiq_pillars.json:

    CATEGORIES & CRITERIA:
    {criteria_str}

    POST TEXT TO CLASSIFY:
    \"\"\"{text}\"\"\"

    Respond ONLY with the exact matching category name from the list: [{', '.join(categories)}].
    Do not add extra commentary, quotes, markdown formatting, or punctuation.
    """
    
    # Updated candidate list matching active endpoints in your API account
    candidate_models = ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-3.6-flash"]
    
    for model_name in candidate_models:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.0
                    )
                )
                raw_category = response.text.strip()
                clean_category = re.sub(r'[*`"]', '', raw_category).strip()
                return clean_category if clean_category in categories else "Uncategorized"

            except Exception as e:
                err_str = str(e)
                if "503" in err_str or "UNAVAILABLE" in err_str or "429" in err_str:
                    wait_time = (attempt + 1) * 2
                    print(f"  [Model {model_name} busy. Retrying in {wait_time}s...]")
                    time.sleep(wait_time)
                elif "404" in err_str or "NOT_FOUND" in err_str:
                    print(f"  [Model {model_name} deprecated/not found. Trying next candidate...]")
                    break  # Skip to the next model in candidate_models immediately
                else:
                    print(f"  [Error on {model_name}: {e}]")
                    break

    print("  [All model attempts failed.]")
    return "Uncategorized"

# ---------------------------------------------------------------------------
# Main Execution Pipeline
# ---------------------------------------------------------------------------
def main():
    # 1. Load criteria
    categories, criteria_str = load_pillars_criteria(PILLARS_FILE)
    print(f"Loaded {len(categories)} pillars from {PILLARS_FILE}")

    # 2. Load CSV
    if not os.path.exists(CSV_FILE):
        raise FileNotFoundError(f"Could not find CSV at {CSV_FILE}")

    df = pd.read_csv(CSV_FILE)
    # FIX: Ensure 'pillar' column allows text/string values
    df['pillar'] = df['pillar'].astype(object)
    
    # Identify rows where 'pillar' (Column E) is missing or blank
    missing_mask = df['pillar'].isna() | (df['pillar'].astype(str).str.strip() == '')
    missing_indices = df[missing_mask].index

    print(f"Found {len(missing_indices)} posts with missing pillars in {CSV_FILE}.")

    updated_count = 0
    for idx in missing_indices:
        post_url = df.loc[idx, 'post_url']
        print(f"Processing row {idx}: {post_url}")
        
        content = fetch_linkedin_text(post_url)
        
        if content:
            assigned_pillar = classify_text(content, categories, criteria_str)
            df.loc[idx, 'pillar'] = assigned_pillar
            print(f" -> Classified as: {assigned_pillar}")
            df.to_csv(CSV_FILE, index=False)
            updated_count += 1
        else:
            print(" -> Failed to fetch content.")

    # Save updated CSV back to src/_data/master.csv
    if updated_count > 0:
        df.to_csv(CSV_FILE, index=False)
        print(f"Successfully updated and saved {updated_count} rows in {CSV_FILE}.")
    else:
        print("No updates made.")

if __name__ == "__main__":
    main()