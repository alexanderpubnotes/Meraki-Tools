#!/usr/bin/env python3
"""
flatten_probe.py  —  ONE-OFF DIAGNOSTIC. Not part of the suite.

Purpose: prove that Meraki accepts a LARGE flattened L3 rule (object/group
references expanded to literal IPs/CIDRs/FQDNs) on a real PUT, at full scale
(the Spamhaus rule), and that nothing is dropped. Also captures the structure
needed to test re-inflation later.

SAFETY:
  - Read-only unless you pass --apply.
  - With --apply it APPENDS one test rule (comment "FLATTEN PROBE - delete me")
    to the target network and leaves all existing rules untouched. Reversible:
    just delete that one rule afterward.
  - Targets ONLY the network you set below.

Setup:
  export MERAKI_DASHBOARD_API_KEY=...    (or it will read your env)
  Fill in ORG_ID and NETWORK_ID below.
  Run first with NO flag (preview), then with --apply once you're satisfied.
"""

import os
import re
import sys
import json

import meraki

# ---------------------------------------------------------------------------
# FILL THESE IN:
ORG_ID = "PUT_YOUR_TEST_ORG_ID_HERE"
NETWORK_ID = "PUT_YOUR_TEST_NETWORK_ID_HERE"
# The comment of the rule whose object/group refs we want to flatten & test.
# Defaults to the Spamhaus rule; change if you want to probe a different one.
SOURCE_RULE_COMMENT = "Deny Spamhaus"
# ---------------------------------------------------------------------------

PROBE_COMMENT = "FLATTEN PROBE - delete me"
GRP = re.compile(r"GRP\((\d+)\)")
OBJ = re.compile(r"OBJ\((\d+)\)")


def get_dashboard():
    key = os.environ.get("MERAKI_DASHBOARD_API_KEY")
    if not key:
        sys.exit("Set MERAKI_DASHBOARD_API_KEY first.")
    return meraki.DashboardAPI(key, suppress_logging=True, print_console=False)


def object_value(obj):
    """Return the literal value of a policy object (cidr or fqdn)."""
    if obj.get("type") == "fqdn":
        return obj.get("fqdn")
    return obj.get("cidr")


def build_indexes(dashboard):
    """Map object id -> value, and group id -> [object ids]."""
    objs = dashboard.organizations.getOrganizationPolicyObjects(ORG_ID, total_pages="all")
    grps = dashboard.organizations.getOrganizationPolicyObjectsGroups(ORG_ID, total_pages="all")
    obj_val = {}
    obj_name = {}
    for o in objs:
        obj_val[str(o["id"])] = object_value(o)
        obj_name[str(o["id"])] = o.get("name", "")
    grp_members = {}
    grp_name = {}
    for g in grps:
        grp_members[str(g["id"])] = [str(x) for x in (g.get("objectIds") or [])]
        grp_name[str(g["id"])] = g.get("name", "")
    return obj_val, obj_name, grp_members, grp_name


def flatten_dest(dest_cidr, obj_val, grp_members, grp_name, obj_name):
    """
    Expand GRP()/OBJ() refs in a destCidr string to literal values.
    Returns (flattened_list, structure) where structure records which values
    came from which group/object (for re-inflation testing).
    """
    values = []
    structure = {"groups": {}, "objects": {}, "literals": []}
    for token in dest_cidr.split(","):
        token = token.strip()
        mg = GRP.fullmatch(token)
        mo = OBJ.fullmatch(token)
        if mg:
            gid = mg.group(1)
            members = grp_members.get(gid, [])
            vals = [obj_val.get(m) for m in members if obj_val.get(m)]
            structure["groups"][grp_name.get(gid, gid)] = vals
            values.extend(vals)
        elif mo:
            oid = mo.group(1)
            v = obj_val.get(oid)
            if v:
                structure["objects"][obj_name.get(oid, oid)] = v
                values.append(v)
        else:
            structure["literals"].append(token)
            values.append(token)
    # de-dup preserving order
    seen, flat = set(), []
    for v in values:
        if v and v not in seen:
            seen.add(v); flat.append(v)
    return flat, structure


def main():
    apply = "--apply" in sys.argv
    if ORG_ID.startswith("PUT_") or NETWORK_ID.startswith("PUT_"):
        sys.exit("Edit ORG_ID and NETWORK_ID at the top of this script first.")

    dash = get_dashboard()
    print(f"Org {ORG_ID}, network {NETWORK_ID}")
    print("Building object/group indexes...")
    obj_val, obj_name, grp_members, grp_name = build_indexes(dash)

    rules = dash.appliance.getNetworkApplianceFirewallL3FirewallRules(NETWORK_ID)["rules"]
    src = next((r for r in rules if r.get("comment", "").strip() == SOURCE_RULE_COMMENT), None)
    if not src:
        sys.exit(f"No rule with comment '{SOURCE_RULE_COMMENT}' on this network.")

    print(f"\nSource rule: {src['policy']} '{src['comment'].strip()}'")
    print(f"  original destCidr refs: {src['destCidr'][:80]}...")

    flat, structure = flatten_dest(src["destCidr"], obj_val, grp_members, grp_name, obj_name)
    flat_str = ",".join(flat)

    print(f"\n=== FLATTEN RESULT ===")
    print(f"  entries after flatten : {len(flat)}")
    print(f"  destCidr length (chars): {len(flat_str)}")
    print(f"  first 3 : {flat[:3]}")
    print(f"  last 3  : {flat[-3:]}")
    print(f"  groups expanded: {len(structure['groups'])}, "
          f"objects: {len(structure['objects'])}, literals: {len(structure['literals'])}")

    # Save the structure sidecar for re-inflation testing
    with open("flatten_probe_structure.json", "w") as fh:
        json.dump(structure, fh, indent=2)
    print("  structure saved -> flatten_probe_structure.json")

    probe_rule = {
        "comment": PROBE_COMMENT,
        "policy": src["policy"],
        "protocol": src.get("protocol", "any"),
        "srcPort": src.get("srcPort", "Any"),
        "srcCidr": src.get("srcCidr", "Any"),
        "destPort": src.get("destPort", "Any"),
        "destCidr": flat_str,
        "syslogEnabled": src.get("syslogEnabled", False),
    }

    if not apply:
        print("\nPREVIEW ONLY — no changes written. Re-run with --apply to test the PUT.")
        return

    # APPEND the probe rule (strip the API default rule, keep everything else)
    body = [r for r in rules if r.get("comment") != "Default rule"
            and r.get("comment") != PROBE_COMMENT]  # avoid stacking on re-run
    new_rules = body + [probe_rule]
    print(f"\nAppending probe rule and PUTting ({len(new_rules)} rules total)...")
    try:
        dash.appliance.updateNetworkApplianceFirewallL3FirewallRules(NETWORK_ID, rules=new_rules)
    except Exception as e:
        print(f"\n*** PUT FAILED: {e}")
        print("This tells us Meraki rejected the flattened rule at this size.")
        return

    # Read back and verify nothing was dropped
    after = dash.appliance.getNetworkApplianceFirewallL3FirewallRules(NETWORK_ID)["rules"]
    stored = next((r for r in after if r.get("comment") == PROBE_COMMENT), None)
    if not stored:
        print("\n*** PUT succeeded but the probe rule isn't in the readback — investigate.")
        return
    stored_entries = [x for x in stored["destCidr"].split(",") if x]
    print(f"\n=== READBACK ===")
    print(f"  PUT accepted: YES")
    print(f"  entries sent    : {len(flat)}")
    print(f"  entries stored  : {len(stored_entries)}")
    if len(stored_entries) == len(flat):
        print("  >>> ALL ENTRIES PRESERVED — flatten survives a real PUT at this scale.")
    else:
        print("  >>> MISMATCH — Meraki dropped/changed entries. Note the counts above.")
    print(f"\nDone. Remember to delete the '{PROBE_COMMENT}' rule from {NETWORK_ID} when finished.")


if __name__ == "__main__":
    main()
