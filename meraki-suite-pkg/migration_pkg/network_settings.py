"""
network_settings.py — backup and restore of networks and their MX
(appliance) settings.

Backup layout produced (inside the timestamped backup folder):

    networks.json                      <- list of networks in the source org
    networks/<network name>/
        vlans_settings.json
        vlans.json
        l3_firewall.json               <- outbound rules
        l3_inbound.json
        l7_firewall.json               <- includes geo blocking rules
        content_filtering.json
        port_forwarding.json
        one_to_one_nat.json
        one_to_many_nat.json

Restore logic, per network (matched between orgs BY NAME):
    1. Create the network in the target org if it doesn't exist.
    2. Apply settings in dependency order (VLANs enabled before VLANs, etc.)
    3. L3 rules: OBJ(id)/GRP(id) references are rewritten through the id map
       produced by the policy-object restore. The "Default rule" is stripped
       (the API appends its own; pushing it back duplicates it).
    4. Content filtering: the GET returns categories as {id, name} dicts but
       the PUT wants plain id lists, so we transform.
    5. 1:1 / 1:Many NAT rules contain PUBLIC IPs from the old environment —
       they are pushed as-is, with a loud warning to review them.

Everything is per-item error handled: one failure never stops the run.
"""

import os
import re

from common import save_json, load_json, announce

# Settings handled by the generic loop, in the order they must be restored.
# (name/filename, GET method on dashboard.appliance, UPDATE method, transform)
SETTINGS = [
    # single_lan only exists (GET succeeds) on networks with VLANs disabled —
    # it carries the one-subnet addressing those sites use instead of VLANs.
    ("single_lan",        "getNetworkApplianceSingleLan",                   "updateNetworkApplianceSingleLan",                   None),
    ("vlans_settings",    "getNetworkApplianceVlansSettings",               "updateNetworkApplianceVlansSettings",               None),
    ("vlans",             "getNetworkApplianceVlans",                       None,                                                "vlans"),
    ("static_routes",     "getNetworkApplianceStaticRoutes",                None,                                                "static_routes"),
    ("l3_firewall",       "getNetworkApplianceFirewallL3FirewallRules",     "updateNetworkApplianceFirewallL3FirewallRules",     "firewall"),
    ("l3_inbound",        "getNetworkApplianceFirewallInboundFirewallRules","updateNetworkApplianceFirewallInboundFirewallRules","firewall"),
    ("l7_firewall",       "getNetworkApplianceFirewallL7FirewallRules",     "updateNetworkApplianceFirewallL7FirewallRules",     None),
    ("content_filtering", "getNetworkApplianceContentFiltering",            "updateNetworkApplianceContentFiltering",            "content_filtering"),
    ("security_malware",  "getNetworkApplianceSecurityMalware",             "updateNetworkApplianceSecurityMalware",             None),
    ("security_intrusion","getNetworkApplianceSecurityIntrusion",           "updateNetworkApplianceSecurityIntrusion",           None),
    ("port_forwarding",   "getNetworkApplianceFirewallPortForwardingRules", "updateNetworkApplianceFirewallPortForwardingRules", None),
    ("one_to_one_nat",    "getNetworkApplianceFirewallOneToOneNatRules",    "updateNetworkApplianceFirewallOneToOneNatRules",    None),
    ("one_to_many_nat",   "getNetworkApplianceFirewallOneToManyNatRules",   "updateNetworkApplianceFirewallOneToManyNatRules",   None),
    # Backed up by this table, but restored in a SECOND PASS after every
    # network exists, because spokes reference hub networks by ID:
    ("site_to_site_vpn",  "getNetworkApplianceVpnSiteToSiteVpn",            "updateNetworkApplianceVpnSiteToSiteVpn",            "vpn_second_pass"),
]

NETWORK_CREATE_FIELDS = ["name", "productTypes", "timeZone", "tags", "notes"]
VLAN_READONLY_FIELDS = {"networkId", "interfaceId", "templateVlanType", "mask", "cidr"}

OBJ_REF = re.compile(r"(OBJ|GRP)\((\d+)\)")


def safe_name(name):
    """Network name -> filesystem-safe folder name."""
    return "".join(c if c.isalnum() or c in "-_ ." else "_" for c in name)


# ---------------------------------------------------------------- backup ---

def backup(dashboard, org_id, backup_dir, network_filter=None):
    """Save all networks (or only those named in network_filter) and their
    appliance settings to JSON files."""
    announce(f"Backing up networks from org {org_id}")

    networks = dashboard.organizations.getOrganizationNetworks(
        organizationId=org_id, total_pages="all"
    )
    if network_filter:
        networks = [n for n in networks if n["name"] in network_filter]
    save_json(f"{backup_dir}/networks.json", networks)

    import switch_settings

    for net in networks:
        product_types = net.get("productTypes", [])
        if "appliance" not in product_types and "switch" not in product_types:
            print(f"  SKIP    '{net['name']}' (no appliance or switch)")
            continue
        net_dir = f"{backup_dir}/networks/{safe_name(net['name'])}"
        print(f"\n  network '{net['name']}'")
        if "appliance" in product_types:
            for name, get_method, _update, _transform in SETTINGS:
                try:
                    data = getattr(dashboard.appliance, get_method)(networkId=net["id"])
                    save_json(f"{net_dir}/{name}.json", data)
                except Exception as e:
                    # Normal for some settings (e.g. VLANs not enabled on this
                    # network) — note it and move on.
                    print(f"  note    '{net['name']}' {name}: not backed up ({e})")
        if "switch" in product_types:
            switch_settings.backup(dashboard, net, net_dir)
    print(f"\n  {len(networks)} network(s) processed.")


# ------------------------------------------------------------ transforms ---

def rewrite_object_refs(text, id_map):
    """Rewrite OBJ(oldId)/GRP(oldId) inside a src/dest string to new-org IDs.
    Raises KeyError if a referenced ID is missing from the id map."""
    def repl(match):
        kind = "objects" if match.group(1) == "OBJ" else "groups"
        new_id = id_map.get(kind, {}).get(match.group(2))
        if not new_id:
            raise KeyError(
                f"{match.group(0)} has no mapping — run the policy-object "
                f"restore into this org first (it generates the id map)."
            )
        return f"{match.group(1)}({new_id})"
    return OBJ_REF.sub(repl, text)


def transform_firewall(data, id_map):
    """Strip the API-managed default rule and remap OBJ()/GRP() references."""
    rules = []
    for rule in data.get("rules", []):
        if rule.get("comment") == "Default rule":
            continue
        rule = dict(rule)
        for field in ("srcCidr", "destCidr"):
            value = rule.get(field)
            if isinstance(value, str) and ("OBJ(" in value or "GRP(" in value):
                rule[field] = rewrite_object_refs(value, id_map)
        rules.append(rule)
    out = dict(data)
    out["rules"] = rules
    # The inbound-rules GET includes syslogDefaultRule, which can be null
    # when never configured — the PUT only accepts a real boolean.
    if not isinstance(out.get("syslogDefaultRule"), bool):
        out.pop("syslogDefaultRule", None)
    return out


def transform_content_filtering(data, _id_map):
    """GET returns category dicts; PUT expects lists of category IDs."""
    out = dict(data)
    for field in ("blockedUrlCategories", "allowedUrlCategories"):
        if isinstance(out.get(field), list):
            out[field] = [c["id"] if isinstance(c, dict) else c for c in out[field]]
    return out


TRANSFORMS = {
    "firewall": transform_firewall,
    "content_filtering": transform_content_filtering,
}


# --------------------------------------------------------------- restore ---

def restore(dashboard, target_org_id, backup_dir, dry_run=True, network_filter=None,
            id_map=None, progress_cb=None, cancel_event=None):
    mode = "DRY RUN — no changes will be made" if dry_run else "applying changes"
    announce(f"Restoring networks into org {target_org_id} ({mode})")

    networks = load_json(f"{backup_dir}/networks.json")
    if network_filter:
        networks = [n for n in networks if n["name"] in network_filter]

    if not networks:
        print("  WARNING: no networks matched the filter — nothing will be restored. "
              "Network names must match the backup exactly.")
    else:
        print(f"  targeting {len(networks)} network(s): "
              + ", ".join(n["name"] for n in networks[:10])
              + (" ..." if len(networks) > 10 else ""))

    if id_map is None:
        id_map_path = f"{backup_dir}/idmap_org_{target_org_id}.json"
        id_map = load_json(id_map_path) if os.path.exists(id_map_path) else {}
    if not id_map:
        print("  note    no id map found for this target org — firewall rules "
              "that reference OBJ()/GRP() will fail until you run the "
              "policy-object restore into this org.")

    existing = dashboard.organizations.getOrganizationNetworks(
        organizationId=target_org_id, total_pages="all"
    )
    existing_by_name = {n["name"]: n for n in existing}
    name_to_target_id = {n["name"]: n["id"] for n in existing}
    failures = []

    cancelled = False
    total = len(networks)
    for i, net in enumerate(networks, 1):
        if cancel_event is not None and cancel_event.is_set():
            print(f"\n  Cancelled — stopped before network {i}/{total} "
                  f"('{net['name']}'). Networks already restored are unaffected.")
            cancelled = True
            break
        net_dir = f"{backup_dir}/networks/{safe_name(net['name'])}"
        if not os.path.isdir(net_dir):
            print(f"  SKIP    '{net['name']}' (no settings in backup)")
            if progress_cb:
                progress_cb(i, total)
            continue
        print(f"\n  network '{net['name']}'")

        # ---- 1. Create the network in the target org if missing ----------
        target_net = existing_by_name.get(net["name"])
        if target_net is None:
            payload = {k: net[k] for k in NETWORK_CREATE_FIELDS if net.get(k) is not None}
            if dry_run:
                print(f"  would create network '{net['name']}' ({payload.get('productTypes')})")
                target_net = {"id": f"(new id for {net['name']})"}
            else:
                try:
                    target_net = dashboard.organizations.createOrganizationNetwork(
                        organizationId=target_org_id, **payload
                    )
                    print(f"  created network '{net['name']}'")
                except Exception as e:
                    print(f"  ERROR   creating network '{net['name']}': {e}")
                    failures.append({"kind": "network", "name": net["name"], "error": str(e)})
                    continue
        else:
            print(f"  exists  network '{net['name']}'")
            # Keep functional metadata in sync — network tags drive IPsec
            # peer availability, tag-scoped admins, and firmware groups.
            backup_tags = sorted(net.get("tags") or [])
            target_tags = sorted(target_net.get("tags") or [])
            if backup_tags != target_tags:
                if dry_run:
                    print(f"  would sync network tags -> {backup_tags}")
                else:
                    try:
                        dashboard.networks.updateNetwork(
                            networkId=target_net["id"], tags=net.get("tags") or [])
                        print(f"  synced network tags -> {backup_tags}")
                    except Exception as e:
                        print(f"  ERROR   syncing tags on '{net['name']}': {e}")
                        failures.append({"kind": "network_tags", "name": net["name"],
                                         "error": str(e)})
        name_to_target_id[net["name"]] = target_net["id"]

        # ---- 2. Apply settings in order -----------------------------------
        for name, _get, update_method, transform in SETTINGS:
            path = f"{net_dir}/{name}.json"
            if not os.path.exists(path):
                continue
            if transform == "vpn_second_pass":
                continue  # handled after every network exists
            data = load_json(path)

            try:
                if transform == "vlans":
                    restore_vlans(dashboard, target_net["id"], net["name"], data,
                                  failures, dry_run)
                    continue
                if transform == "static_routes":
                    restore_static_routes(dashboard, target_net["id"], net["name"],
                                          data, failures, dry_run)
                    continue
                if transform:
                    data = TRANSFORMS[transform](data, id_map)
                if name in ("one_to_one_nat", "one_to_many_nat") and data.get("rules"):
                    print(f"  REVIEW  '{net['name']}' {name}: rules contain public IPs "
                          f"from the OLD environment — verify them after restore.")
                if dry_run:
                    print(f"  would apply {name}")
                else:
                    getattr(dashboard.appliance, update_method)(
                        networkId=target_net["id"], **data
                    )
                    print(f"  applied {name}")
            except Exception as e:
                print(f"  ERROR   '{net['name']}' {name}: {e}")
                failures.append({"kind": name, "name": net["name"], "error": str(e)})

        # ---- 2b. Network-scoped switch settings ---------------------------
        if "switch" in net.get("productTypes", []):
            import switch_settings
            switch_settings.restore_network(dashboard, target_net["id"], net["name"],
                                            net_dir, backup_dir, target_org_id,
                                            failures, dry_run)
        if progress_cb:
            progress_cb(i, total)

    # ---- 3. Second pass: site-to-site VPN (hubs first, then spokes) --------
    # Not gated by cancel_event — it's a short pass over whatever networks
    # were actually restored above, not a separate long-running phase.
    restore_vpn(dashboard, networks, backup_dir, name_to_target_id, failures, dry_run)

    # ---- Failure summary ---------------------------------------------------
    if failures:
        print(f"\n  {len(failures)} item(s) FAILED — every other item was still processed:")
        for f in failures:
            print(f"    - {f['kind']} on '{f['name']}'")
        if not dry_run:
            save_json(f"{backup_dir}/network_failures_org_{target_org_id}.json", failures)
            print("  Details saved. Fix the cause and re-run; the restore is idempotent.")
    elif dry_run:
        print("\n  Dry run complete — nothing was changed. Re-run with --apply to apply."
              + ("  (CANCELLED early)" if cancelled else ""))
    elif cancelled:
        print("\n  Cancelled early — safe to re-run; already-restored networks are idempotent.")


def restore_vpn_only(dashboard, target_org_id, backup_dir, network_filter=None,
                     dry_run=True, progress_cb=None, cancel_event=None):
    """Re-run just the site-to-site VPN pass — e.g. after the hub network's
    MX has been claimed, which is required before spokes can point at it."""
    mode = "DRY RUN — no changes will be made" if dry_run else "applying changes"
    announce(f"Restoring site-to-site VPN into org {target_org_id} ({mode})")

    networks = load_json(f"{backup_dir}/networks.json")
    if network_filter:
        networks = [n for n in networks if n["name"] in network_filter]
    if not networks:
        print("  WARNING: no networks matched the filter — nothing will be restored.")
    else:
        print(f"  targeting {len(networks)} network(s): "
              + ", ".join(n["name"] for n in networks[:10])
              + (" ..." if len(networks) > 10 else ""))
    target_nets = dashboard.organizations.getOrganizationNetworks(
        organizationId=target_org_id, total_pages="all")
    name_to_target_id = {n["name"]: n["id"] for n in target_nets}
    failures = []
    restore_vpn(dashboard, networks, backup_dir, name_to_target_id, failures, dry_run,
               progress_cb=progress_cb, cancel_event=cancel_event)
    if failures:
        print(f"\n  {len(failures)} item(s) FAILED:")
        for f in failures:
            print(f"    - {f['kind']} on '{f['name']}'")
        if not dry_run:
            save_json(f"{backup_dir}/vpn_failures_org_{target_org_id}.json", failures)


def restore_vpn(dashboard, networks, backup_dir, name_to_target_id, failures, dry_run,
                progress_cb=None, cancel_event=None):
    """Apply site-to-site (AutoVPN) settings after all networks exist.

    A spoke's config references its hubs by NETWORK ID, so each hubId is
    remapped old-org-id -> network name -> new-org-id. Hubs are applied
    before spokes, since the API rejects a spoke pointing at a network
    that isn't in hub mode yet.

    progress_cb/cancel_event are optional and only meaningful when this is the
    PRIMARY operation (i.e. called from restore_vpn_only) — when called as the
    tail end of a full restore(), the caller intentionally omits them so this
    short pass always runs to completion.
    """
    # Map old network IDs to names using the FULL backup network list —
    # a spoke's hub may not be part of a filtered restore.
    all_backup_nets = load_json(f"{backup_dir}/networks.json")
    old_id_to_name = {n["id"]: n["name"] for n in all_backup_nets}

    # Collect (network, vpn config) pairs that have a VPN backup file.
    pending = []
    for net in networks:
        path = f"{backup_dir}/networks/{safe_name(net['name'])}/site_to_site_vpn.json"
        if os.path.exists(path):
            pending.append((net, load_json(path)))
    if not pending:
        return
    pending.sort(key=lambda item: 0 if item[1].get("mode") == "hub" else 1)

    print("\n  --- site-to-site VPN (second pass) ---")
    total = len(pending)
    for i, (net, data) in enumerate(pending, 1):
        if cancel_event is not None and cancel_event.is_set():
            print(f"\n  Cancelled — stopped before VPN network {i}/{total}.")
            break
        target_id = name_to_target_id.get(net["name"])
        if not target_id:
            if progress_cb:
                progress_cb(i, total)
            continue  # network was never created; already reported above
        try:
            payload = dict(data)
            if payload.get("hubs"):
                new_hubs = []
                for hub in payload["hubs"]:
                    hub_name = old_id_to_name.get(hub.get("hubId"))
                    new_hub_id = name_to_target_id.get(hub_name)
                    if not new_hub_id:
                        raise KeyError(
                            f"hub network '{hub_name or hub.get('hubId')}' not found in "
                            f"target org — restore it (it must exist and be a hub) first."
                        )
                    new_hubs.append({**hub, "hubId": new_hub_id})
                payload["hubs"] = new_hubs
            if dry_run:
                print(f"  would apply site_to_site_vpn on '{net['name']}' "
                      f"(mode: {payload.get('mode')})")
            else:
                dashboard.appliance.updateNetworkApplianceVpnSiteToSiteVpn(
                    networkId=target_id, **payload
                )
                print(f"  applied site_to_site_vpn on '{net['name']}' "
                      f"(mode: {payload.get('mode')})")
        except Exception as e:
            print(f"  ERROR   '{net['name']}' site_to_site_vpn: {e}")
            failures.append({"kind": "site_to_site_vpn", "name": net["name"], "error": str(e)})
        if progress_cb:
            progress_cb(i, total)


def restore_static_routes(dashboard, net_id, net_name, routes, failures, dry_run):
    """Create or update each static route, matched by subnet. Created with
    the minimum fields, then updated with the full payload (fixed IP
    assignments / reserved ranges are update-only)."""
    if dry_run:
        existing = {}
    else:
        existing = {r["subnet"]: r for r in
                    dashboard.appliance.getNetworkApplianceStaticRoutes(networkId=net_id)}
    for route in routes:
        payload = {k: v for k, v in route.items() if k not in ("id", "networkId")}
        try:
            if route["subnet"] in existing:
                if dry_run:
                    print(f"  would update static route '{route.get('name')}' ({route['subnet']})")
                else:
                    dashboard.appliance.updateNetworkApplianceStaticRoute(
                        networkId=net_id, staticRouteId=existing[route["subnet"]]["id"],
                        **payload)
                    print(f"  updated static route '{route.get('name')}' ({route['subnet']})")
            else:
                if dry_run:
                    print(f"  would create static route '{route.get('name')}' ({route['subnet']})")
                else:
                    create_kwargs = {"name": route.get("name"), "subnet": route["subnet"]}
                    for k in ("gatewayIp", "gatewayVlanId"):
                        if route.get(k) is not None:
                            create_kwargs[k] = route[k]
                    created = dashboard.appliance.createNetworkApplianceStaticRoute(
                        networkId=net_id, **create_kwargs)
                    dashboard.appliance.updateNetworkApplianceStaticRoute(
                        networkId=net_id, staticRouteId=created["id"], **payload)
                    print(f"  created static route '{route.get('name')}' ({route['subnet']})")
        except Exception as e:
            print(f"  ERROR   static route '{route.get('name')}' on '{net_name}': {e}")
            failures.append({"kind": f"static route {route.get('subnet')}",
                             "name": net_name, "error": str(e)})


def restore_vlans(dashboard, net_id, net_name, vlans, failures, dry_run):
    """Create or update each VLAN. Assumes vlans_settings was applied first
    (it precedes vlans in SETTINGS), which enables VLANs on the network."""
    if dry_run:
        existing_ids = set()
    else:
        existing_ids = {str(v["id"]) for v in
                        dashboard.appliance.getNetworkApplianceVlans(networkId=net_id)}

    for vlan in vlans:
        payload = {k: v for k, v in vlan.items() if k not in VLAN_READONLY_FIELDS}
        if payload.get("groupPolicyId"):
            print(f"  WARN    VLAN {vlan['id']} '{vlan.get('name')}': groupPolicyId "
                  f"dropped (group policies are not migrated yet)")
            payload.pop("groupPolicyId")
        try:
            if str(vlan["id"]) in existing_ids:
                if dry_run:
                    print(f"  would update VLAN {vlan['id']} '{vlan.get('name')}'")
                else:
                    payload.pop("id", None)
                    dashboard.appliance.updateNetworkApplianceVlan(
                        networkId=net_id, vlanId=str(vlan["id"]), **payload
                    )
                    print(f"  updated VLAN {vlan['id']} '{vlan.get('name')}'")
            else:
                if dry_run:
                    print(f"  would create VLAN {vlan['id']} '{vlan.get('name')}'")
                else:
                    # Create with the minimum, then push the full payload —
                    # several DHCP fields are only accepted on update.
                    dashboard.appliance.createNetworkApplianceVlan(
                        networkId=net_id, id=str(vlan["id"]), name=vlan["name"],
                        subnet=vlan.get("subnet"), applianceIp=vlan.get("applianceIp"),
                    )
                    payload.pop("id", None)
                    dashboard.appliance.updateNetworkApplianceVlan(
                        networkId=net_id, vlanId=str(vlan["id"]), **payload
                    )
                    print(f"  created VLAN {vlan['id']} '{vlan.get('name')}'")
        except Exception as e:
            print(f"  ERROR   VLAN {vlan['id']} on '{net_name}': {e}")
            failures.append({"kind": f"vlan {vlan['id']}", "name": net_name, "error": str(e)})
