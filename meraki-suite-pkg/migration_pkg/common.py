"""
common.py — shared helpers for the migration tool.

The API key is NEVER stored in code or config files. It is read from the
environment variable MERAKI_DASHBOARD_API_KEY, which is also the variable
the official Meraki SDK looks for on its own.

    Linux/macOS:   export MERAKI_DASHBOARD_API_KEY=yourkeyhere
    Windows (PS):  $env:MERAKI_DASHBOARD_API_KEY="yourkeyhere"
"""

import json
import os
import sys

import meraki

ENV_VAR = "MERAKI_DASHBOARD_API_KEY"
BACKUP_ROOT = "./backups"


def get_dashboard():
    """Create a Meraki Dashboard API client using the API key from the environment."""
    if not os.environ.get(ENV_VAR):
        sys.exit(
            f"Error: environment variable {ENV_VAR} is not set.\n"
            f"Set it first, e.g.:  export {ENV_VAR}=yourkeyhere"
        )
    # The SDK reads MERAKI_DASHBOARD_API_KEY from the environment by itself.
    return meraki.DashboardAPI(
        suppress_logging=True,      # we do our own, simpler logging
        maximum_retries=10,         # SDK auto-handles 429 rate limits
        wait_on_rate_limit=True,
    )


def save_json(path, data):
    """Write data to a JSON file, creating parent directories as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fp:
        json.dump(data, fp, indent=2)
    print(f"  saved   {path}")


def load_json(path):
    with open(path) as fp:
        return json.load(fp)


def announce(msg):
    print(f"\n=== {msg} ===")
