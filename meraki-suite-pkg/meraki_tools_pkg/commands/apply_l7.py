"""
apply_l7.py — propagate L7 firewall rules from a source JSON file onto networks.

This is a WRITE command. It is built entirely on the propagate model:
  - It never authors rules. It takes a JSON file produced by `export l7`
    (i.e. Meraki's own GET output) and pushes it to the targeted networks.
  - The Meraki update endpoint REPLACES the whole rules array (there is no
    server-side "add"), so both modes below are just different arrays we build:
      mode=replace : send the source rules as-is (target becomes the source).
      mode=append  : GET the target's current rules, add the source's rules
                     that aren't already present, then send the combined array.
  - Dry run is the default. Nothing changes without apply=True.

The per-network API error is surfaced (not swallowed) so a picky rule type on
one network is visible rather than silent.
"""

from merakicore import networks as net_mod
from merakicore import io as io_mod
from merakicore import safety


def _get_current(dashboard, network_id):
    return dashboard.appliance.getNetworkApplianceFirewallL7FirewallRules(network_id).get("rules", [])


def _rule_key(rule):
    """A hashable identity for a rule, so append can skip duplicates."""
    val = rule.get("value")
    if isinstance(val, dict):                 # application / applicationCategory
        val = val.get("id", str(val))
    elif isinstance(val, list):               # country-code arrays
        val = tuple(val)
    return (rule.get("policy"), rule.get("type"), val)


def _build_target_rules(dashboard, network_id, source_rules, mode, position):
    """Construct the full rules array to send for one network."""
    if mode == "replace":
        return list(source_rules), "replace"

    # append
    current = _get_current(dashboard, network_id)
    existing_keys = {_rule_key(r) for r in current}
    to_add = [r for r in source_rules if _rule_key(r) not in existing_keys]
    if not to_add:
        return current, "no-new-rules"
    combined = (to_add + current) if position == "top" else (current + to_add)
    return combined, f"+{len(to_add)} rule(s)"


def run(dashboard, org_id, source_file, network_ids=None,
        mode="replace", position="bottom", apply=False,
        progress_cb=None, cancel_event=None):
    """
    Args:
        source_file: path to JSON produced by `export l7`.
        network_ids: target network IDs (None = all appliance networks).
        mode:        "replace" or "append".
        position:    for append, "top" or "bottom".
        apply:       False = dry run (default). True = actually write.
    """
    data = io_mod.load_json(source_file)
    source_rules = data.get("rules", [])
    src_name = data.get("source_network_name", "?")
    print(f"  source file: {source_file}  ({len(source_rules)} rule(s) from '{src_name}')")
    print(f"  mode: {mode}" + (f" ({position})" if mode == "append" else ""))

    # Only appliance networks can have L7 rules.
    targets = net_mod.resolve_targets(
        dashboard, org_id, network_ids=network_ids, product_type="appliance"
    )

    # Don't let someone accidentally push a network's rules back onto itself in replace.
    src_id = data.get("source_network_id")
    targets = [t for t in targets if t["id"] != src_id] or targets

    print("\n" + net_mod.confirmation_readout(targets))

    dry_run = not apply

    def action(net, is_dry):
        new_rules, detail = _build_target_rules(
            dashboard, net["id"], source_rules, mode, position
        )
        if detail == "no-new-rules":
            return "unchanged", "all source rules already present"
        if is_dry:
            return "changed", f"{detail} -> would set {len(new_rules)} total rule(s)"
        dashboard.appliance.updateNetworkApplianceFirewallL7FirewallRules(
            net["id"], rules=new_rules
        )
        return "changed", f"{detail} -> set {len(new_rules)} total rule(s)"

    result = safety.run_write(targets, action, dry_run=dry_run,
                              progress_cb=progress_cb, cancel_event=cancel_event)
    result.print_summary(dry_run)
    return result
