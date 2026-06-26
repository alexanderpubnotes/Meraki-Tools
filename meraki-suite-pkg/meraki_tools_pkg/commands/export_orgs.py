"""
export_orgs.py — list the organizations the API key can see.

Read-only, and unlike every other command it does NOT require an org ID — this
is the command you run BEFORE you have one, to discover it. Solves the
chicken-and-egg problem: every other command needs an org ID, this finds it.

Prints to the screen by default (the common case: "what's my org ID?"), and
can optionally write JSON/CSV like the other exporters.
"""

from merakicore import io as io_mod

CSV_FIELDS = ["id", "name", "url"]


def _row(org):
    return {"id": org.get("id", ""), "name": org.get("name", ""), "url": org.get("url", "")}


def run(dashboard, fmt=None, output=None):
    """
    Args:
        fmt:    None -> just print to screen. "json"/"csv" -> also write a file.
        output: output path when fmt is set (default: orgs.<fmt>)
    """
    orgs = dashboard.organizations.getOrganizations()
    orgs = sorted(orgs, key=lambda o: o.get("name", "").lower())
    print(f"  found {len(orgs)} organization(s):\n")
    for org in orgs:
        print(f"  {org['id']}   {org.get('name', '(unnamed)')}")

    if fmt:
        out = output or f"orgs.{fmt}"
        if fmt == "csv":
            io_mod.save_csv(out, [_row(o) for o in orgs], CSV_FIELDS)
        else:
            io_mod.save_json(out, orgs)
        return out
    return None
