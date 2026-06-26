"""
export_policy-check (module: export_policy_check.py)
Read-only audit: for a set of FQDNs/IPs, report whether each already exists as a
policy object — in one org or across all orgs the key can see.

This is the read-only counterpart to `apply policy-group`. It writes no changes;
it just answers "do these objects exist, and where are they missing?"
"""

import sys

from merakicore import policyobjects as po


def _check_org(dashboard, org, entries):
    """Return a result dict for one org; never raises (errors captured)."""
    try:
        objects = po.fetch_objects(dashboard, org["id"])
    except Exception as e:
        return {"org_name": org["name"], "org_id": org["id"], "error": str(e)}

    by_fqdn, by_cidr, _ = po.build_indexes(objects)
    results = []
    for val, typ in entries:
        obj = po.find_existing(val, typ, by_fqdn, by_cidr)
        results.append({"entry": val, "found": obj is not None,
                        "obj_id": obj["id"] if obj else None})
    found = sum(1 for r in results if r["found"])
    return {"org_name": org["name"], "org_id": org["id"],
            "results": results, "found": found, "total": len(entries)}


def run(dashboard, entries, org_id=None):
    """
    Args:
        entries: list of (value, type) from po.parse_entries
        org_id:  a specific org to check; None => all orgs the key can see
    """
    if not entries:
        sys.exit("Nothing to check. Supply --fqdn / --ip / --from-file.")

    all_orgs = dashboard.organizations.getOrganizations()
    if org_id:
        orgs = [o for o in all_orgs if o["id"] == org_id]
        if not orgs:
            sys.exit(f"Org {org_id} not visible to this API key.")
    else:
        orgs = all_orgs

    print(f"  checking {len(entries)} entr(y/ies) across {len(orgs)} org(s)\n")
    multi = len(orgs) > 1
    summary = []

    for org in orgs:
        res = _check_org(dashboard, org, entries)
        summary.append(res)
        if "error" in res:
            print(f"  ERROR  {res['org_name']} ({res['org_id']}): {res['error']}")
            continue
        found, total = res["found"], res["total"]
        icon = "OK" if found == total else ("PARTIAL" if found else "MISSING")
        print(f"  [{icon}] {res['org_name']} ({found}/{total})")
        # In single-org mode show every entry; in multi-org show only what's missing.
        for r in res["results"]:
            if not multi or not r["found"]:
                mark = "found" if r["found"] else "MISSING"
                idhint = f" (ID: {r['obj_id']})" if r["obj_id"] else ""
                print(f"        {mark:>7}  {r['entry']}{idhint}")

    if multi:
        complete = sum(1 for r in summary if "error" not in r and r["found"] == r["total"])
        partial = sum(1 for r in summary if "error" not in r and 0 < r["found"] < r["total"])
        none = sum(1 for r in summary if "error" not in r and r["found"] == 0)
        errs = sum(1 for r in summary if "error" in r)
        print(f"\n  Total: {len(summary)} org(s) — {complete} complete, "
              f"{partial} partial, {none} none"
              + (f", {errs} error(s)" if errs else ""))
    return summary
