"""
refresh_data.py

Reads contentiq.config.json and runs whichever data source is currently
active, instead of a workflow or a person having to know which
merge_*_metrics.py script to call by hand.

Usage:
  python3 scripts/refresh_data.py

To switch data sources: edit contentiq.config.json's "active_source" field
to "buffer", "taplio", or "zernio" (once STRV-96 ships). Nothing else needs
to change -- not the workflow file, not this script.

Note on update_frequency_hours: this value is the intended cadence, but
GitHub Actions cron schedules are static YAML, not something a workflow can
read from a JSON file at trigger-decision time. Changing this number here
does NOT automatically change the real schedule -- you also need to update
the cron line in .github/workflows/data-refresh.yml to match. Run
`python3 scripts/refresh_data.py --show-cron` to get the correct cron
expression to paste in.
"""

import json
import subprocess
import sys
from pathlib import Path

CONFIG_PATH = Path("contentiq.config.json")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"ERROR: {CONFIG_PATH} not found. Run this from the repo root.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


def hours_to_cron(hours: int) -> str:
    """Turn '4' into '0 */4 * * *', matching how GitHub Actions cron syntax
    expresses an every-N-hours schedule."""
    if hours <= 0 or hours > 24:
        print(f"ERROR: update_frequency_hours must be between 1 and 24, got {hours}.")
        sys.exit(1)
    return f"0 */{hours} * * *"


def main():
    config = load_config()

    if "--show-cron" in sys.argv:
        cron = hours_to_cron(config["update_frequency_hours"])
        print(f"Cron expression for {config['update_frequency_hours']}h: {cron}")
        print("Paste this into the 'cron:' line in .github/workflows/data-refresh.yml")
        return

    active = config.get("active_source")
    sources = config.get("sources", {})

    if active not in sources:
        print(f"ERROR: active_source '{active}' is not defined under 'sources' in {CONFIG_PATH}.")
        print(f"Valid options: {', '.join(sources.keys())}")
        sys.exit(1)

    source = sources[active]

    if not source.get("enabled"):
        print(f"ERROR: '{active}' is set as active_source but is not enabled yet.")
        note = source.get("notes", "")
        if note:
            print(f"Note: {note}")
        sys.exit(1)

    script_path = Path(source["script"])
    if not script_path.exists():
        print(f"ERROR: configured script {script_path} does not exist.")
        sys.exit(1)

    print(f"Active data source: {active}")
    print(f"Running {script_path}...")
    print()

    result = subprocess.run([sys.executable, str(script_path)])
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
