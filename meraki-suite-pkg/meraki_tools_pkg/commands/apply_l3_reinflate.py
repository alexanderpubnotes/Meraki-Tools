"""
apply_l3_reinflate.py — rebuild object/group references in an L3 ruleset.

The migration round-trip:
  1. Source org: flatten rules (OBJ/GRP -> literal values) so the appliance can
     move online with self-contained rules.
  2. Recreate the objects/groups in the TARGET org (by name, e.g. apply
     policy-bulk).
  3. Reinflate: take a ruleset whose src/dest reference objects/groups BY NAME,
     resolve those names to the TARGET org's live IDs, and PUT the rebuilt rules.

Names are the portable key. IDs are always resolved live against the target
org — never carried from the source org. If a referenced name is missing in the
target org, we REFUSE (so you never write a rule pointing at the wrong/nonexistent
group).

Input ruleset format (JSON): a list of rules, where src/dest are given as ordered
token lists. Each token is one of:
    {"grp": "Spamhaus Group 1"}     -> resolves to GRP(<id>) in target org
    {"obj": "SMTP Object"}          -> resolves to OBJ(<id>) in target org
    {"lit": "203.0.113.0/24"}       -> literal, kept as-is (CIDR/FQDN/VLAN/Any)
Order is preserved. Example rule:
    {
      "comment": "Deny Spamhaus", "policy": "deny", "protocol": "any",
      "srcPort": "Any", "destPort": "Any", "syslogEnabled": false,
      "src": [{"lit": "Any"}],
      "dest": [{"grp": "Spamhaus Group 1"}, {"grp": "Spamhaus Group 2"}]
    }
"""

import json

from merakicore import networks as net_mod
from merakicore import policyobjects as po
from merakicore import safety


def _build_name_maps(dashboard, org_id):
    groups = po.fetch_groups(dashboard, org_id)
    objects = po.fetch_objects(dashboard, org_id)
    gmap = {g["name"]: str(g["id"]) for g in groups}
    omap = {o["name"]: str(o["id"]) for o in objects}
    return gmap, omap


def _resolve_field(tokens, gmap, omap, missing):
    """Rebuild a src/dest string from ordered name tokens; record any missing."""
    parts = []
    for tok in tokens:
        if "grp" in tok:
            name = tok["grp"]
            gid = gmap.get(name)
            if gid is None:
                missing.append(f"GRP:{name}")
            else:
                parts.append(f"GRP({gid})")
        elif "obj" in tok:
            name = tok["obj"]
            oid = omap.get(name)
            if oid is None:
                missing.append(f"OBJ:{name}")
            else:
                parts.append(f"OBJ({oid})")
        else:  # literal (CIDR / FQDN / VLAN(...) / "Any")
            parts.append(tok["lit"])
    return ",".join(parts)


def load_ruleset(path):
    with open(path) as fh:
        data = json.load(fh)
    # accept either a bare list or {"rules":[...]}
    return data["rules"] if isinstance(data, dict) and "rules" in data else data


def run(dashboard, org_id, ruleset, network_ids=None, apply=False,
        progress_cb=None, cancel_event=None):
    gmap, omap = _build_name_maps(dashboard, org_id)

    # First pass: resolve everything and collect any missing names BEFORE writing.
    missing = []
    built = []
    for r in ruleset:
        src = _resolve_field(r.get("src", [{"lit": "Any"}]), gmap, omap, missing)
        dest = _resolve_field(r.get("dest", [{"lit": "Any"}]), gmap, omap, missing)
        built.append({
            "comment": r.get("comment", "").strip(),
            "policy": r.get("policy", "allow"),
            "protocol": r.get("protocol", "any"),
            "srcPort": r.get("srcPort", "Any"),
            "srcCidr": src or "Any",
            "destPort": r.get("destPort", "Any"),
            "destCidr": dest or "Any",
            "syslogEnabled": r.get("syslogEnabled", False),
        })

    if missing:
        uniq = sorted(set(missing))
        print(f"  REFUSING: {len(uniq)} referenced name(s) do not exist in org {org_id}:")
        for m in uniq:
            print(f"    - {m}")
        print("  Recreate these objects/groups in the target org first "
              "(same names), then re-run.")
        return None

    print(f"  resolved {len(built)} rule(s); all group/object names found in org {org_id}")

    targets = net_mod.resolve_targets(dashboard, org_id, network_ids=network_ids,
                                      product_type="appliance")
    print("\n" + net_mod.confirmation_readout(targets))
    dry_run = not apply

    def action(net, is_dry):
        if is_dry:
            return "changed", f"would set {len(built)} rule(s) (reinflated references)"
        dashboard.appliance.updateNetworkApplianceFirewallL3FirewallRules(
            net["id"], rules=built)
        return "changed", f"set {len(built)} rule(s)"

    result = safety.run_write(targets, action, dry_run=dry_run,
                              progress_cb=progress_cb, cancel_event=cancel_event)
    result.print_summary(dry_run)
    return result
