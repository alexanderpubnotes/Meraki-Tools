"""
export_l7.py — export a network's MX L7 firewall rules to JSON.

Read-only. This is the "copy from" step of the propagate model: configure one
network in the dashboard, export its L7 rules here, then `apply l7 --from` that
JSON onto other networks.

The exported JSON is the exact rules array the API returns, so it can be fed
straight back into the update endpoint without reshaping.
"""

from merakicore import networks as net_mod
from merakicore import io as io_mod
from merakicore import paths


def _get_l7_rules(dashboard, network_id):
    return dashboard.appliance.getNetworkApplianceFirewallL7FirewallRules(network_id).get("rules", [])


def run(dashboard, org_id, source_network_id, output=None):
    """
    Export the L7 rules of ONE source network.

    Args:
        source_network_id: the network to copy rules FROM (must be appliance/MX).
        output:            output path (default: l7_<networkid>.json)
    """
    # Validate the source exists and is an appliance network.
    networks = net_mod.resolve_targets(
        dashboard, org_id, network_ids=[source_network_id], product_type="appliance"
    )
    source = networks[0]
    print(f"  source: {source['name']} ({source['id']})")

    rules = _get_l7_rules(dashboard, source["id"])
    print(f"  found {len(rules)} L7 rule(s)")

    payload = {
        "source_network_id": source["id"],
        "source_network_name": source["name"],
        "rules": rules,
    }
    out = output or paths.default_path("exports", f"l7_{source['id']}.json")
    io_mod.save_json(out, payload)
    return out
