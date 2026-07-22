"""
apply_l3_insert.py — insert ONE L3 firewall rule at position 1 across networks.

Unlike apply_l7 (which replaces the whole ruleset), L3 rulesets differ per
location, so this is SURGICAL: it inserts one rule at the top and leaves every
other rule on the network untouched.

Key safety design:
  - The rule's group/object references are given BY NAME, not by ID, because
    group IDs differ per org. For each target org we resolve those names to that
    org's real IDs. If any referenced group/object name is missing in the org,
    we REFUSE to touch that org's networks (enforces "create groups first").
  - The API's implicit trailing "Default rule" is stripped before PUT (it is
    re-added automatically); sending it back would duplicate it.
  - Dedup: if a rule with the same (normalized) comment already exists on a
    network, we skip it — re-running never stacks duplicates.
  - Dry run by default.
"""

import re

from merakicore import networks as net_mod
from merakicore import policyobjects as po
from merakicore import safety

GRP_TOKEN = re.compile(r"GRP\(([^)]+)\)")
OBJ_TOKEN = re.compile(r"OBJ\(([^)]+)\)")


def _build_name_to_id(dashboard, org_id):
    """Return (group_name->id, object_name->id) for an org."""
    groups = po.fetch_groups(dashboard, org_id)
    objects = po.fetch_objects(dashboard, org_id)
    gmap = {g["name"]: str(g["id"]) for g in groups}
    omap = {o["name"]: str(o["id"]) for o in objects}
    return gmap, omap


def _resolve_dest_by_name(dest_names, gmap, omap):
    """
    dest_names: list of ("GRP"|"OBJ"|"CIDR", value) — value is a NAME for GRP/OBJ,
                or a literal CIDR/'Any' for CIDR.
    Returns (destCidr_string, missing_list).
    """
    parts, missing = [], []
    for kind, value in dest_names:
        if kind == "GRP":
            gid = gmap.get(value)
            if gid is None:
                missing.append(f"GRP:{value}")
            else:
                parts.append(f"GRP({gid})")
        elif kind == "OBJ":
            oid = omap.get(value)
            if oid is None:
                missing.append(f"OBJ:{value}")
            else:
                parts.append(f"OBJ({oid})")
        else:  # literal
            parts.append(value)
    return ",".join(parts), missing


def _norm(comment):
    return (comment or "").strip().lower()


def load_rule_file(path):
    """
    Load a rule spec from JSON. Dest references are BY NAME (org-portable).

    Expected shape:
      {
        "comment": "Deny Spamhaus",
        "policy": "deny",
        "protocol": "any",
        "srcPort": "Any", "srcCidr": "Any", "destPort": "Any",
        "syslogEnabled": false,
        "dest_groups":  ["Spamhaus Group 1", "Spamhaus Group 2", ...],
        "dest_objects": ["Some Object Name", ...],
        "dest_literals":["203.0.113.0/24", "Any", ...]
      }
    Only the dest_* lists that apply need be present.
    """
    import json
    with open(path) as fh:
        data = json.load(fh)
    dest = []
    for name in data.get("dest_groups", []):
        dest.append(("GRP", name))
    for name in data.get("dest_objects", []):
        dest.append(("OBJ", name))
    for lit in data.get("dest_literals", []):
        dest.append(("CIDR", lit))
    if not dest:
        raise ValueError("rule file has no destination (need dest_groups / "
                         "dest_objects / dest_literals)")
    return {
        "comment": data.get("comment", "").strip(),
        "policy": data.get("policy", "deny"),
        "protocol": data.get("protocol", "any"),
        "srcPort": data.get("srcPort", "Any"),
        "srcCidr": data.get("srcCidr", "Any"),
        "destPort": data.get("destPort", "Any"),
        "syslogEnabled": data.get("syslogEnabled", False),
        "dest": dest,
    }


def run(dashboard, org_id, rule_spec, network_ids=None, apply=False,
        progress_cb=None, cancel_event=None):
    """
    rule_spec: dict describing the rule to insert, with dest references BY NAME:
      {
        "comment": "Deny Spamhaus",
        "policy": "deny",
        "protocol": "any",
        "srcPort": "Any", "srcCidr": "Any", "destPort": "Any",
        "dest": [("GRP","Spamhaus Group 1"), ("GRP","Spamhaus Group 2"), ...],
        "syslogEnabled": False,
      }
    """
    # Resolve group/object NAMES to THIS org's IDs.
    gmap, omap = _build_name_to_id(dashboard, org_id)
    dest_cidr, missing = _resolve_dest_by_name(rule_spec["dest"], gmap, omap)
    if missing:
        print(f"  REFUSING: these referenced groups/objects do not exist in org {org_id}:")
        for m in missing:
            print(f"    - {m}")
        print("  Create them first (they must exist before the rule can reference them).")
        return None

    built_rule = {
        "comment": rule_spec.get("comment", "").strip(),
        "policy": rule_spec.get("policy", "deny"),
        "protocol": rule_spec.get("protocol", "any"),
        "srcPort": rule_spec.get("srcPort", "Any"),
        "srcCidr": rule_spec.get("srcCidr", "Any"),
        "destPort": rule_spec.get("destPort", "Any"),
        "destCidr": dest_cidr,
        "syslogEnabled": rule_spec.get("syslogEnabled", False),
    }
    print(f"  rule resolved for org {org_id}: {built_rule['policy']} "
          f"{built_rule['comment']!r} -> {dest_cidr[:60]}{'...' if len(dest_cidr) > 60 else ''}")

    targets = net_mod.resolve_targets(dashboard, org_id, network_ids=network_ids,
                                      product_type="appliance")
    print("\n" + net_mod.confirmation_readout(targets))
    dry_run = not apply
    want_comment = _norm(built_rule["comment"])

    def action(net, is_dry):
        current = dashboard.appliance.getNetworkApplianceFirewallL3FirewallRules(
            net["id"]).get("rules", [])
        # dedup: already present at all?
        if any(_norm(r.get("comment")) == want_comment for r in current):
            return "unchanged", "rule already present"
        # strip the API's implicit default rule
        body = [r for r in current if r.get("comment") != "Default rule"]
        new_rules = [built_rule] + body
        # accurate counts: what the network shows now vs. after (API re-adds default)
        had_default = any(r.get("comment") == "Default rule" for r in current)
        after_visible = len(new_rules) + (1 if had_default else 0)
        if is_dry:
            return "changed", f"would insert at position 1 ({len(current)} -> {after_visible} rules)"
        dashboard.appliance.updateNetworkApplianceFirewallL3FirewallRules(
            net["id"], rules=new_rules)
        return "changed", f"inserted at position 1 (now ~{after_visible} rules)"

    result = safety.run_write(targets, action, dry_run=dry_run,
                              progress_cb=progress_cb, cancel_event=cancel_event)
    result.print_summary(dry_run)
    return result
