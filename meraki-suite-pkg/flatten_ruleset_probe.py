#!/usr/bin/env python3
"""
flatten_ruleset_probe.py  —  ONE-OFF DIAGNOSTIC (v2). Not part of the suite.

Flattens the ENTIRE L3 ruleset of a test network: expands OBJ()/GRP() refs in
srcCidr AND destCidr to literal values, leaves VLAN() refs and raw literals
untouched, and captures a full structure sidecar for re-inflation.

MODES:
  (no flag)          Preview. Reads rules, flattens in memory, reports per-rule
                     stats + writes structure sidecar + backs up original rules.
                     Writes NOTHING to Meraki.
  --apply            Backs up the original ruleset to a file, then PUTs the
                     fully-flattened ruleset to the network, then reads back and
                     verifies per-rule entry counts survived.
  --restore FILE     Restores a previously backed-up ruleset from FILE (undo).

SAFETY:
  - Preview by default. --apply replaces the network's L3 rules with flattened
    equivalents; it ALWAYS writes a backup file first so you can --restore.
  - Targets ONLY the network you set below. Use a throwaway test network.

Setup:
  export MERAKI_DASHBOARD_API_KEY=...
  Fill in ORG_ID and NETWORK_ID below.
"""

import os
import re
import sys
import json
from datetime import datetime

import meraki

# ---------------------------------------------------------------------------
ORG_ID = "PUT_YOUR_TEST_ORG_ID_HERE"
NETWORK_ID = "PUT_YOUR_TEST_NETWORK_ID_HERE"
# ---------------------------------------------------------------------------

GRP = re.compile(r"GRP\((\d+)\)")
OBJ = re.compile(r"OBJ\((\d+)\)")
VLAN = re.compile(r"VLAN\(", re.IGNORECASE)


def get_dashboard():
    key = os.environ.get("MERAKI_DASHBOARD_API_KEY")
    if not key:
        sys.exit("Set MERAKI_DASHBOARD_API_KEY first.")
    return meraki.DashboardAPI(key, suppress_logging=True, print_console=False)


def object_value(obj):
    return obj.get("fqdn") if obj.get("type") == "fqdn" else obj.get("cidr")


def build_indexes(dashboard):
    objs = dashboard.organizations.getOrganizationPolicyObjects(ORG_ID, total_pages="all")
    grps = dashboard.organizations.getOrganizationPolicyObjectsGroups(ORG_ID, total_pages="all")
    obj_val = {str(o["id"]): object_value(o) for o in objs}
    obj_name = {str(o["id"]): o.get("name", "") for o in objs}
    grp_members = {str(g["id"]): [str(x) for x in (g.get("objectIds") or [])] for g in grps}
    grp_name = {str(g["id"]): g.get("name", "") for g in grps}
    return obj_val, obj_name, grp_members, grp_name


def flatten_field(field_value, idx):
    """
    Flatten one srcCidr/destCidr string.
    - GRP()/OBJ() -> expand to literal member values
    - VLAN(...)   -> passed through UNCHANGED (not a policy object)
    - literals    -> passed through
    Returns (flattened_string, field_structure, unresolved_list).
    """
    obj_val, obj_name, grp_members, grp_name = idx
    if not field_value or field_value == "Any":
        return field_value, None, []

    out_tokens = []
    fstruct = {"groups": {}, "objects": {}, "vlans": [], "literals": []}
    unresolved = []

    for token in field_value.split(","):
        token = token.strip()
        mg = GRP.fullmatch(token)
        mo = OBJ.fullmatch(token)
        if mg:
            gid = mg.group(1)
            if gid not in grp_members:
                unresolved.append(f"GRP({gid})")
                out_tokens.append(token)  # leave as-is; flag it
                continue
            vals = [obj_val.get(m) for m in grp_members[gid] if obj_val.get(m)]
            fstruct["groups"][grp_name.get(gid, gid)] = vals
            out_tokens.extend(vals)
        elif mo:
            oid = mo.group(1)
            v = obj_val.get(oid)
            if v is None:
                unresolved.append(f"OBJ({oid})")
                out_tokens.append(token)
                continue
            fstruct["objects"][obj_name.get(oid, oid)] = v
            out_tokens.append(v)
        elif VLAN.match(token):
            fstruct["vlans"].append(token)       # pass through unchanged
            out_tokens.append(token)
        else:
            fstruct["literals"].append(token)
            out_tokens.append(token)

    # de-dup preserving order
    seen, deduped = set(), []
    for t in out_tokens:
        if t and t not in seen:
            seen.add(t); deduped.append(t)
    return ",".join(deduped), fstruct, unresolved


def flatten_ruleset(rules, idx):
    """Flatten every rule. Returns (new_rules, full_structure, any_unresolved)."""
    new_rules = []
    full_structure = []
    any_unresolved = []
    for i, r in enumerate(rules):
        if r.get("comment") == "Default rule":
            continue  # API-managed; strip before PUT
        nr = dict(r)
        rec = {"index": i, "comment": r.get("comment", ""),
               "src": None, "dest": None}
        for field, key in (("srcCidr", "src"), ("destCidr", "dest")):
            flat, fstruct, unresolved = flatten_field(r.get(field, "Any"), idx)
            nr[field] = flat
            rec[key] = fstruct
            for u in unresolved:
                any_unresolved.append(f"rule {i} ({r.get('comment','')}): {u}")
        new_rules.append(nr)
        full_structure.append(rec)
    return new_rules, full_structure, any_unresolved


def main():
    if ORG_ID.startswith("PUT_") or NETWORK_ID.startswith("PUT_"):
        sys.exit("Edit ORG_ID and NETWORK_ID at the top first.")

    dash = get_dashboard()

    # --restore mode
    if "--restore" in sys.argv:
        i = sys.argv.index("--restore")
        if i + 1 >= len(sys.argv):
            sys.exit("Usage: --restore <backup_file.json>")
        path = sys.argv[i + 1]
        with open(path) as fh:
            saved = json.load(fh)
        rules = [r for r in saved["rules"] if r.get("comment") != "Default rule"]
        dash.appliance.updateNetworkApplianceFirewallL3FirewallRules(NETWORK_ID, rules=rules)
        print(f"Restored {len(rules)} rule(s) to {NETWORK_ID} from {path}.")
        return

    apply = "--apply" in sys.argv

    print(f"Org {ORG_ID}, network {NETWORK_ID}")
    idx = build_indexes(dash)
    original = dash.appliance.getNetworkApplianceFirewallL3FirewallRules(NETWORK_ID)["rules"]
    print(f"Original ruleset: {len(original)} rule(s)")

    new_rules, structure, unresolved = flatten_ruleset(original, idx)

    print("\n=== PER-RULE FLATTEN REPORT ===")
    for rec, nr in zip(structure, new_rules):
        dest_n = len([x for x in (nr.get("destCidr") or "").split(",") if x]) if nr.get("destCidr") not in (None, "Any") else 0
        src_n = len([x for x in (nr.get("srcCidr") or "").split(",") if x]) if nr.get("srcCidr") not in (None, "Any") else 0
        g = len((rec["dest"] or {}).get("groups", {})) + len((rec["src"] or {}).get("groups", {})) if (rec["dest"] or rec["src"]) else 0
        v = len((rec["dest"] or {}).get("vlans", [])) + len((rec["src"] or {}).get("vlans", [])) if (rec["dest"] or rec["src"]) else 0
        print(f"  [{rec['index']:>2}] {rec['comment'][:28]:<28} "
              f"dest={dest_n:<5} src={src_n:<3} groups_expanded={g} vlans_kept={v}")

    if unresolved:
        print("\n  !! UNRESOLVED references (left as-is, would NOT be portable):")
        for u in unresolved:
            print(f"     {u}")

    with open("flatten_ruleset_structure.json", "w") as fh:
        json.dump(structure, fh, indent=2)
    print("\n  full structure saved -> flatten_ruleset_structure.json")

    if not apply:
        print("\nPREVIEW ONLY — nothing written. Re-run with --apply to test the full PUT.")
        print("(--apply will back up the original ruleset first, then you can --restore it.)")
        return

    # Back up original BEFORE writing
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"l3_backup_{NETWORK_ID}_{stamp}.json"
    with open(backup_path, "w") as fh:
        json.dump({"network": NETWORK_ID, "rules": original}, fh, indent=2)
    print(f"\n  original ruleset backed up -> {backup_path}")
    print(f"  (undo with:  python {os.path.basename(sys.argv[0])} --restore {backup_path})")

    print(f"\n  PUTting flattened ruleset ({len(new_rules)} rules)...")
    try:
        dash.appliance.updateNetworkApplianceFirewallL3FirewallRules(NETWORK_ID, rules=new_rules)
    except Exception as e:
        print(f"\n*** PUT FAILED: {e}")
        print(f"  original is safe in {backup_path}; restore it with --restore.")
        return

    after = dash.appliance.getNetworkApplianceFirewallL3FirewallRules(NETWORK_ID)["rules"]
    after_body = [r for r in after if r.get("comment") != "Default rule"]
    print(f"\n=== READBACK ===")
    print(f"  rules sent  : {len(new_rules)}")
    print(f"  rules stored: {len(after_body)}")
    ok = len(after_body) == len(new_rules)
    # per-rule entry count check
    mism = 0
    for sent, got in zip(new_rules, after_body):
        s = len([x for x in (sent.get("destCidr") or "").split(",") if x])
        g = len([x for x in (got.get("destCidr") or "").split(",") if x])
        if s != g:
            mism += 1
            print(f"  MISMATCH rule '{sent.get('comment','')}': sent {s} dest entries, stored {g}")
    if ok and mism == 0:
        print("  >>> FULL RULESET FLATTENED AND STORED INTACT.")
    else:
        print(f"  >>> issues: rule-count ok={ok}, per-rule mismatches={mism}")
    print(f"\nDone. Restore the original when finished:  --restore {backup_path}")


if __name__ == "__main__":
    main()
