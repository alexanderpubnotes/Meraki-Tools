"""
export_groups.py — export an org's policy object GROUPS to JSON/CSV.

Read-only. Discovery/verification tool: lists each policy object group's name,
ID, and member count so you can confirm which groups exist before referencing
them (e.g. in an L3-insert rule file, which references groups by NAME).

This is a human aid, not an input other commands consume — the apply commands
resolve group names to IDs live, so nothing here goes stale.
"""

from merakicore import policyobjects as po
from merakicore import io as io_mod
from merakicore import paths

CSV_FIELDS = ["id", "name", "member_count"]


def run(dashboard, org_id, fmt="json", output=None, show_members=False):
    groups = po.fetch_groups(dashboard, org_id)
    groups = sorted(groups, key=lambda g: g.get("name", "").lower())
    print(f"  {len(groups)} group(s) in org {org_id}:\n")
    print(f"  {'NAME':<28} {'MEMBERS':>7}   ID")
    print(f"  {'-'*28} {'-'*7}   {'-'*20}")
    rows = []
    for g in groups:
        members = g.get("objectIds") or []
        print(f"  {g.get('name',''):<28} {len(members):>7}   {g.get('id','')}")
        row = {"id": g.get("id", ""), "name": g.get("name", ""),
               "member_count": len(members)}
        if show_members:
            row["object_ids"] = members
        rows.append(row)

    if output or fmt:
        out = output or paths.default_path("exports", f"groups_{org_id}.{fmt}")
        if fmt == "csv":
            io_mod.save_csv(out, rows, CSV_FIELDS)
        else:
            io_mod.save_json(out, rows)
        return out
    return None
