"""
export_firewall.py — export MX L3 firewall rules and switch ACL rules.

Read-only. Consolidates the two old L3 exporter scripts:
  - L3 rules with OBJ/GRP tokens resolved to names (display) AND expanded to
    underlying IPs/FQDNs (audit) — from the "redux" script.
  - Switch ACL rules — from the "acl" script.

Org-level policy lookups are built once and reused across all networks.
"""

from merakicore import networks as net_mod
from merakicore import resolve as rz
from merakicore import io as io_mod

L3_FIELDS = [
    "network_id", "network_name", "rule_index", "comment", "policy", "protocol",
    "src_cidr", "src_port", "dest_cidr", "dest_port", "syslog_enabled",
    "src_resolved", "dest_resolved",
]

SWITCH_ACL_FIELDS = [
    "network_id", "network_name", "rule_index", "comment", "policy",
    "ip_version", "protocol", "src_cidr", "src_port", "dest_cidr", "dest_port", "vlan",
]


def _get_l3_rules(dashboard, network_id):
    try:
        return dashboard.appliance.getNetworkApplianceFirewallL3FirewallRules(network_id).get("rules", [])
    except Exception as e:
        print(f"  warning: L3 skip {network_id}: {e}")
        return []


def _get_switch_acls(dashboard, network_id):
    try:
        return dashboard.switch.getNetworkSwitchAccessControlLists(network_id).get("rules", [])
    except Exception as e:
        print(f"  warning: ACL skip {network_id}: {e}")
        return []


def _flatten_l3(network, index, rule, net_gp, org_grp, org_obj):
    raw_src = rule.get("srcCidr", "")
    raw_dest = rule.get("destCidr", "")
    return {
        "network_id": network["id"],
        "network_name": network["name"],
        "rule_index": index,
        "comment": rule.get("comment", ""),
        "policy": rule.get("policy", ""),
        "protocol": rule.get("protocol", ""),
        "src_cidr": rz.resolve_tokens_for_display(raw_src, net_gp, org_grp, org_obj),
        "src_port": rule.get("srcPort", "").replace(",", " | "),
        "dest_cidr": rz.resolve_tokens_for_display(raw_dest, net_gp, org_grp, org_obj),
        "dest_port": rule.get("destPort", "").replace(",", " | "),
        "syslog_enabled": rule.get("syslogEnabled", False),
        "src_resolved": rz.expand_cidr_field(raw_src, net_gp, org_grp, org_obj),
        "dest_resolved": rz.expand_cidr_field(raw_dest, net_gp, org_grp, org_obj),
    }


def _flatten_acl(network, index, rule):
    return {
        "network_id": network["id"],
        "network_name": network["name"],
        "rule_index": index,
        "comment": rule.get("comment", ""),
        "policy": rule.get("policy", ""),
        "ip_version": rule.get("ipVersion", ""),
        "protocol": rule.get("protocol", ""),
        "src_cidr": rule.get("srcCidr", ""),
        "src_port": rule.get("srcPort", "").replace(",", " | "),
        "dest_cidr": rule.get("destCidr", ""),
        "dest_port": rule.get("destPort", "").replace(",", " | "),
        "vlan": rule.get("vlan", ""),
    }


def run(dashboard, org_id, network_ids=None, fmt="csv", output=None):
    """
    Export L3 + switch ACL rules for the targeted networks.

    Writes two files (one per rule type). Returns the list of paths written.
    """
    networks = net_mod.resolve_targets(dashboard, org_id, network_ids=network_ids)
    print(f"  processing {len(networks)} network(s)")

    print("  building org policy-object lookups...")
    org_obj = rz.build_object_lookup(dashboard, org_id)
    org_grp = rz.build_group_lookup(dashboard, org_id)

    l3_rows, acl_rows = [], []
    for net in networks:
        ptypes = net.get("productTypes", [])
        print(f"    -> {net['name']} ({net['id']})")
        if "appliance" in ptypes:
            net_gp = rz.build_network_group_policy_lookup(dashboard, net["id"])
            for i, rule in enumerate(_get_l3_rules(dashboard, net["id"])):
                l3_rows.append(_flatten_l3(net, i, rule, net_gp, org_grp, org_obj))
        if "switch" in ptypes:
            for i, rule in enumerate(_get_switch_acls(dashboard, net["id"])):
                acl_rows.append(_flatten_acl(net, i, rule))

    base = output or "firewall"
    paths = []
    if fmt == "json":
        p1 = f"{base}_l3.json"; io_mod.save_json(p1, l3_rows); paths.append(p1)
        p2 = f"{base}_switch_acl.json"; io_mod.save_json(p2, acl_rows); paths.append(p2)
    else:
        p1 = f"{base}_l3.csv"; io_mod.save_csv(p1, l3_rows, L3_FIELDS); paths.append(p1)
        p2 = f"{base}_switch_acl.csv"; io_mod.save_csv(p2, acl_rows, SWITCH_ACL_FIELDS); paths.append(p2)

    print(f"  L3 rules: {len(l3_rows)}   switch ACL rules: {len(acl_rows)}")
    return paths
