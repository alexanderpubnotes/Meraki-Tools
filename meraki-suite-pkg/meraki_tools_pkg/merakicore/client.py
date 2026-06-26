"""
client.py — the one and only place this project creates a Meraki API client.

Every command imports get_dashboard() from here so that authentication,
rate-limit handling, and retry behavior are identical everywhere.

The API key is NEVER stored in code or a config file. It is read from the
environment variable MERAKI_DASHBOARD_API_KEY, which is also the variable the
official Meraki SDK looks for on its own.

    Linux/macOS:   export MERAKI_DASHBOARD_API_KEY=yourkeyhere
    Windows (PS):  $env:MERAKI_DASHBOARD_API_KEY="yourkeyhere"
"""

import os

import meraki

# The SDK's own default env var name. Standardizing on this means the key works
# for our code AND for the SDK's internal auth without extra wiring.
ENV_VAR = "MERAKI_DASHBOARD_API_KEY"


class MissingApiKey(Exception):
    """Raised when MERAKI_DASHBOARD_API_KEY is not set in the environment."""


def have_api_key():
    """Return True if the API key is present in the environment."""
    return bool(os.environ.get(ENV_VAR))


def get_dashboard():
    """
    Create a Meraki Dashboard API client.

    Raises MissingApiKey (rather than exiting) so that callers — a CLI, a GUI,
    or a test — can each decide how to react. The CLI prints a friendly message
    and exits; a GUI can show a dialog and keep running.
    """
    if not have_api_key():
        raise MissingApiKey(
            f"Environment variable {ENV_VAR} is not set.\n"
            f"Set it first, for example:\n"
            f"  Linux/macOS:   export {ENV_VAR}=yourkeyhere\n"
            f'  Windows (PS):  $env:{ENV_VAR}="yourkeyhere"'
        )

    # The SDK reads MERAKI_DASHBOARD_API_KEY from the environment by itself,
    # so we don't pass the key explicitly.
    return meraki.DashboardAPI(
        suppress_logging=True,   # we do our own simpler logging
        maximum_retries=10,      # SDK auto-handles 429 rate limits
        wait_on_rate_limit=True,
    )
