"""
io.py — output helpers (JSON / CSV).

Principle: JSON is the canonical, round-trippable format. CSV is a read-only
export VIEW for humans — convenient for spreadsheets, never a write-back path.
"""

import csv
import json
import os


def _ensure_parent(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def save_json(path, data):
    """Write data to a JSON file, creating parent directories as needed."""
    _ensure_parent(path)
    with open(path, "w") as fp:
        json.dump(data, fp, indent=2)
    print(f"  saved   {path}")


def load_json(path):
    """Read and parse a JSON file."""
    with open(path) as fp:
        return json.load(fp)


def save_csv(path, rows, fieldnames):
    """
    Write a list of dict rows to CSV.

    rows:       list[dict]
    fieldnames: column order; keys not listed are ignored, missing keys blank.
    """
    _ensure_parent(path)
    with open(path, "w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  saved   {path}")
