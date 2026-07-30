import os
import json
import requests
import pandas as pd
from google import genai
from google.genai import types
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Initialize Gemini Client (requires GEMINI_API_KEY environment variable)
client = genai.Client()

# Set up relative paths based on repository layout
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))               # reporoot/scripts
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))           # reporoot

PILLARS_FILE = os.path.join(SCRIPT_DIR, "contentiq_pillars.json")     # reporoot/scripts/contentiq_pillars.json
CSV_FILE = os.path.join(REPO_ROOT, "src", "_data", "master.csv")      # reporoot/src/_data/master.csv

def load_pillars_criteria(json_file):
    """Load pillar rules and categories from contentiq_pillars.json."""
    if not os.path.exists(json_file):
        raise FileNotFoundError(f"Could not find {json_file}")
        
    with open(json_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # If JSON is a list of strings, e.g., ["1.0 Career", "2.0 Tech"]
    if isinstance(data, list):
        categories = data
        criteria_str = "\n".join([f"- {c}" for c in categories])
    # If JSON is a dict with descriptions, e.g., {"1.0 Career": "Posts about..."}
    elif isinstance(data, dict):
        categories = list(data.keys())
        criteria_str = json.dumps(data, indent=2)
    else:
        raise ValueError("Unsupported JSON format in contentiq_pillars.json")
        
    return categories, criteria_str

def fetch_linkedin_text(url):
    """Fetch text content from a public LinkedIn post using Jina AI Reader."""
    try:
        jina_url = f"https://r.jina.ai/{url}"
        headers = {"X-With-Generated-Alt": "true"}
        response = requests.get(jina_url, headers=headers, timeout=15)
        if response.status_code == 200:
            return response.text[:3000] # Limit tokens
    except Exception as e:
        print(f"Error fetching {url}: {e}")
    return None

def classify_text(text, categories, criteria_str):
    """Classify the text using Gemini based on rules from contentiq_pillars.json."""
    prompt = f"""
    Analyze the following LinkedIn post text and classify it into EXACTLY ONE category based on these rules/criteria from contentiq_pillars.json:

    CATEGORIES & CRITERIA:
    {criteria_str}

    POST TEXT TO CLASSIFY:
    \"\"\"{text}\"\"\"

    Respond ONLY with the exact matching category name from the list: [{', '.join(categories)}].
    Do not add extra commentary, quotes, markdown formatting, or punctuation.
    """
    
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.0
        )
    )
    
    category = response.text.strip()
    return category if category in categories else "Uncategorized"

def main():
    # 1. Load criteria from scripts/contentiq_pillars.json
    categories, criteria_str = load_pillars_criteria(PILLARS_FILE)
    print(f"Loaded {len(categories)} pillars from {PILLARS_FILE}")

    # 2. Load CSV from src/_data/master.csv
    if not os.path.exists(CSV_FILE):
        raise FileNotFoundError(f"Could not find CSV at {CSV_FILE}")

    df = pd.read_csv(CSV_FILE)
    
    # Identify rows where 'pillar' (Column E) is missing or blank
    missing_mask = df['pillar'].isna() | (df['pillar'].astype(str).str.strip() == '')
    missing_indices = df[missing_mask].index

    print(f"Found {len(missing_indices)} posts with missing pillars in {CSV_FILE}.")

    updated_count = 0
    for idx in missing_indices:
        post_url = df.loc[idx, 'post_url']
        print(f"Processing row {idx}: {post_url}")
        
        # Fetch post content
        content = fetch_linkedin_text(post_url)
        
        if content:
            # Classify content via Gemini using contentiq_pillars.json
            assigned_pillar = classify_text(content, categories, criteria_str)
            df.loc[idx, 'pillar'] = assigned_pillar
            print(f" -> Classified as: {assigned_pillar}")
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
