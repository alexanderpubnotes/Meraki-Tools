"""
apply_l3_flattened.py — push a FLATTENED L3 ruleset onto target networks.

This is the migration "online" step: the flattened ruleset (from
`export l3-migration`, the *_flattened.json file) is self-contained — every
srcCidr/destCidr is literal IPs/CIDRs/FQDNs (VLAN() refs kept), with NO object
or group references. So it can be applied to a target network that does not yet
have the policy objects/groups recreated, keeping the appliance's firewall
functional during the cutover.

Later, once the objects/groups are recreated in the target org, use
`apply l3-reinflate` to restore the object/group-referenced structure.

Sets the network's L3 ruleset to exactly the flattened rules (the API re-adds
its implicit default rule). Dry run by default.
"""

import json

from merakicore import networks as net_mod
from merakicore import safety


def load_ruleset(path):
    with open(path) as fh:
        data = json.load(fh)
    return data["rules"] if isinstance(data, dict) and "rules" in data else data


def _clean(rule):
    """Keep only the fields the update endpoint expects."""
    return {
        "comment": rule.get("comment", "").strip(),
        "policy": rule.get("policy", "allow"),
        "protocol": rule.get("protocol", "any"),
        "srcPort": rule.get("srcPort", "Any"),
        "srcCidr": rule.get("srcCidr", "Any"),
        "destPort": rule.get("destPort", "Any"),
        "destCidr": rule.get("destCidr", "Any"),
        "syslogEnabled": rule.get("syslogEnabled", False),
    }


def run(dashboard, org_id, ruleset, network_ids=None, apply=False,
        progress_cb=None, cancel_event=None):
    # Guard: refuse if the ruleset still contains OBJ()/GRP() refs — that means
    # it isn't actually flattened, and applying it to a target without those
    # objects would create broken references.
    bad = []
    for r in ruleset:
        for field in ("srcCidr", "destCidr"):
            v = r.get(field, "")
            if isinstance(v, str) and ("OBJ(" in v or "GRP(" in v):
                bad.append(f"{r.get('comment','')} ({field})")
    if bad:
        print("  REFUSING: this ruleset still has OBJ()/GRP() references, so it is "
              "not flattened:")
        for b in bad[:10]:
            print(f"    - {b}")
        print("  Use the *_flattened.json from `export l3-migration`, or use "
              "`apply l3-reinflate` for a name-referenced ruleset.")
        return None

    built = [_clean(r) for r in ruleset if r.get("comment") != "Default rule"]
    print(f"  flattened ruleset: {len(built)} rule(s), self-contained (no OBJ/GRP refs)")

    targets = net_mod.resolve_targets(dashboard, org_id, network_ids=network_ids,
                                      product_type="appliance")
    print("\n" + net_mod.confirmation_readout(targets))
    dry_run = not apply

    def action(net, is_dry):
        if is_dry:
            return "changed", f"would set {len(built)} flattened rule(s)"
        dashboard.appliance.updateNetworkApplianceFirewallL3FirewallRules(
            net["id"], rules=built)
        return "changed", f"set {len(built)} flattened rule(s)"

    result = safety.run_write(targets, action, dry_run=dry_run,
                              progress_cb=progress_cb, cancel_event=cancel_event)
    result.print_summary(dry_run)
    return result
