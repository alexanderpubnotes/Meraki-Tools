"""
paths.py — shared default output locations for meraki_tools_pkg commands.

Every export/apply command writes into one repo-level `output/` tree by
default, organized by category, so a technician can always find what a run
produced without hunting through whatever directory they happened to launch
from. Anchored to this file's own location (not the current working
directory), so the location is the same no matter where a command is run
from. An explicit --output/output path always overrides this — these are
DEFAULTS only, used when the caller didn't ask for somewhere specific.
"""

import os

# merakicore/paths.py -> meraki_tools_pkg/ -> repo root
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTPUT_ROOT = os.path.join(_REPO_ROOT, "output")


def default_path(category, filename):
    """Return OUTPUT_ROOT/<category>/<filename>, creating the directory if needed."""
    directory = os.path.join(OUTPUT_ROOT, category)
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, filename)
