"""
switch_settings.py — backup and restore of MS (switch) configuration.

Two very different halves, because the hardware is being replaced and every
serial number changes:

NETWORK-SCOPED settings (pre-stageable into empty networks, restored by the
normal `restore` command alongside the MX settings):
    switch_settings, MTU, storm control, STP, ACL, port schedules, QoS,
    DSCP-to-CoS, DHCP server policy.

DEVICE-SCOPED settings (switch ports) — can only be applied AFTER the new
switches are claimed into the target network on swap day, via the separate
`restore-ports` command. Old device -> new device matching is BY DEVICE NAME:
name each replacement switch exactly like the switch it replaces.

Cross-org ID handling in this module:
  * Port schedules get new IDs in the target org. A name-based map is built
    during the network restore and saved to the backup folder; restore-ports
    uses it to rewrite each port's portScheduleId.
  * STP bridge priorities reference switch SERIALS, which are all changing —
    those entries are dropped with a warning (set priorities after swap).
  * Port access policies (802.1X/RADIUS) are not migrated (secrets aren't
    readable); ports referencing one are restored as open ports with a
    warning so you can re-attach policies manually.
"""

import os

from common import save_json, load_json, announce
from network_settings import safe_name

# (name/filename, GET method on dashboard.switch, UPDATE method, transform)
NETWORK_SETTINGS = [
    ("switch_settings",    "getNetworkSwitchSettings",           "updateNetworkSwitchSettings",           None),
    ("switch_mtu",         "getNetworkSwitchMtu",                "updateNetworkSwitchMtu",                None),
    ("switch_storm",       "getNetworkSwitchStormControl",       "updateNetworkSwitchStormControl",       None),
    ("switch_stp",         "getNetworkSwitchStp",                "updateNetworkSwitchStp",                "stp"),
    ("switch_acl",         "getNetworkSwitchAccessControlLists", "updateNetworkSwitchAccessControlLists", "acl"),
    ("switch_dscp",        "getNetworkSwitchDscpToCosMappings",  "updateNetworkSwitchDscpToCosMappings",  None),
    ("switch_dhcp_policy", "getNetworkSwitchDhcpServerPolicy",   "updateNetworkSwitchDhcpServerPolicy",   None),
    # port schedules and QoS rules are create-per-item, handled specially
]

PORT_READONLY_FIELDS = {
    "portId", "linkNegotiationCapabilities", "module", "schedule",
    "profile", "adaptivePolicyGroupId", "peerSgtCapable", "mirror",
    "stackwiseVirtual",
}


# ---------------------------------------------------------------- backup ---

def backup(dashboard, net, net_dir):
    """Back up switch settings for one network (called per network that has
    'switch' in its productTypes). Saves network-scoped settings plus a
    ports.json + info.json per MS device, keyed by device name."""
    for name, get_method, _update, _transform in NETWORK_SETTINGS:
        try:
            data = getattr(dashboard.switch, get_method)(networkId=net["id"])
            save_json(f"{net_dir}/{name}.json", data)
        except Exception as e:
            print(f"  note    '{net['name']}' {name}: not backed up ({e})")

    for name, get_method in (("switch_port_schedules", "getNetworkSwitchPortSchedules"),
                             ("switch_qos_rules", "getNetworkSwitchQosRules")):
        try:
            data = getattr(dashboard.switch, get_method)(networkId=net["id"])
            save_json(f"{net_dir}/{name}.json", data)
        except Exception as e:
            print(f"  note    '{net['name']}' {name}: not backed up ({e})")

    # Per-device port configs, keyed by device NAME.
    try:
        devices = dashboard.networks.getNetworkDevices(networkId=net["id"])
    except Exception as e:
        print(f"  note    '{net['name']}' devices: not backed up ({e})")
        return
    for dev in devices:
        if not dev.get("model", "").startswith("MS"):
            continue
        dev_name = dev.get("name") or dev["serial"]
        try:
            ports = dashboard.switch.getDeviceSwitchPorts(serial=dev["serial"])
            dev_dir = f"{net_dir}/switch_devices/{safe_name(dev_name)}"
            save_json(f"{dev_dir}/info.json",
                      {"name": dev_name, "model": dev["model"], "serial": dev["serial"],
                       "tags": dev.get("tags") or []})
            save_json(f"{dev_dir}/ports.json", ports)
        except Exception as e:
            print(f"  note    switch '{dev_name}' ports: not backed up ({e})")


# ------------------------------------------------------------ transforms ---

def transform_stp(data, _ctx):
    """Bridge priorities reference old serials/stacks — all changing. Drop
    them; keep rstpEnabled."""
    out = dict(data)
    if out.get("stpBridgePriority"):
        print("  WARN    STP bridge priorities reference old switch serials — "
              "dropped; set priorities after the new switches are claimed.")
        out.pop("stpBridgePriority")
    return out


def transform_acl(data, _ctx):
    """Strip the API-managed default allow rule, same as the MX L3 list."""
    out = dict(data)
    out["rules"] = [r for r in data.get("rules", [])
                    if r.get("comment") != "Default rule"]
    return out


TRANSFORMS = {"stp": transform_stp, "acl": transform_acl}


# ----------------------------------------- restore (network-scoped half) ---

def restore_network(dashboard, target_net_id, net_name, net_dir,
                    backup_dir, target_org_id, failures, dry_run):
    """Apply network-scoped switch settings. Builds and persists the port
    schedule name->id map for later use by restore-ports."""
    for name, _get, update_method, transform in NETWORK_SETTINGS:
        path = f"{net_dir}/{name}.json"
        if not os.path.exists(path):
            continue
        try:
            data = load_json(path)
            if transform:
                data = TRANSFORMS[transform](data, None)
            if dry_run:
                print(f"  would apply {name}")
            else:
                getattr(dashboard.switch, update_method)(networkId=target_net_id, **data)
                print(f"  applied {name}")
        except Exception as e:
            print(f"  ERROR   '{net_name}' {name}: {e}")
            failures.append({"kind": name, "name": net_name, "error": str(e)})

    restore_port_schedules(dashboard, target_net_id, net_name, net_dir,
                           backup_dir, target_org_id, failures, dry_run)
    restore_qos_rules(dashboard, target_net_id, net_name, net_dir, failures, dry_run)


def restore_port_schedules(dashboard, target_net_id, net_name, net_dir,
                           backup_dir, target_org_id, failures, dry_run):
    path = f"{net_dir}/switch_port_schedules.json"
    if not os.path.exists(path):
        return
    schedules = load_json(path)
    if not schedules:
        return
    schedule_map = {}  # old schedule id -> new schedule id
    try:
        existing = ({} if dry_run else
                    {s["name"]: s for s in
                     dashboard.switch.getNetworkSwitchPortSchedules(networkId=target_net_id)})
    except Exception:
        existing = {}
    for sched in schedules:
        try:
            payload = {k: v for k, v in sched.items()
                       if k not in ("id", "networkId")}
            if sched["name"] in existing:
                new_id = existing[sched["name"]]["id"]
                if dry_run:
                    print(f"  would update port schedule '{sched['name']}'")
                else:
                    dashboard.switch.updateNetworkSwitchPortSchedule(
                        networkId=target_net_id, portScheduleId=new_id, **payload)
                    print(f"  updated port schedule '{sched['name']}'")
            else:
                if dry_run:
                    print(f"  would create port schedule '{sched['name']}'")
                    new_id = f"(new id for {sched['name']})"
                else:
                    created = dashboard.switch.createNetworkSwitchPortSchedule(
                        networkId=target_net_id, **payload)
                    new_id = created["id"]
                    print(f"  created port schedule '{sched['name']}'")
            schedule_map[str(sched["id"])] = str(new_id)
        except Exception as e:
            print(f"  ERROR   port schedule '{sched.get('name')}': {e}")
            failures.append({"kind": "port_schedule", "name": net_name, "error": str(e)})
    if not dry_run:
        save_json(f"{net_dir}/schedule_idmap_org_{target_org_id}.json", schedule_map)


def restore_qos_rules(dashboard, target_net_id, net_name, net_dir, failures, dry_run):
    path = f"{net_dir}/switch_qos_rules.json"
    if not os.path.exists(path):
        return
    rules = load_json(path)
    if not rules:
        return

    def signature(r):
        return (r.get("vlan"), r.get("protocol"), r.get("srcPort"),
                r.get("srcPortRange"), r.get("dstPort"), r.get("dstPortRange"))

    try:
        existing_sigs = (set() if dry_run else
                         {signature(r) for r in
                          dashboard.switch.getNetworkSwitchQosRules(networkId=target_net_id)})
    except Exception:
        existing_sigs = set()
    for rule in rules:
        if signature(rule) in existing_sigs:
            print(f"  exists  QoS rule {signature(rule)}")
            continue
        try:
            payload = {k: v for k, v in rule.items() if k not in ("id", "networkId")}
            if dry_run:
                print(f"  would create QoS rule {signature(rule)}")
            else:
                dashboard.switch.createNetworkSwitchQosRule(
                    networkId=target_net_id, **payload)
                print(f"  created QoS rule {signature(rule)}")
        except Exception as e:
            print(f"  ERROR   QoS rule on '{net_name}': {e}")
            failures.append({"kind": "qos_rule", "name": net_name, "error": str(e)})


# ------------------------------------------- restore-ports (swap day) ------

def restore_ports(dashboard, target_org_id, backup_dir, network_filter=None,
                  dry_run=False):
    """Apply per-port switch configs. Run AFTER the new switches are claimed
    into the target networks. Matching is by DEVICE NAME: a new switch named
    exactly like the old one inherits its port config."""
    mode = "DRY RUN — no changes will be made" if dry_run else "applying changes"
    announce(f"Restoring switch ports into org {target_org_id} ({mode})")

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
    target_by_name = {n["name"]: n for n in target_nets}
    failures = []

    for net in networks:
        net_dir = f"{backup_dir}/networks/{safe_name(net['name'])}"
        dev_root = f"{net_dir}/switch_devices"
        if not os.path.isdir(dev_root):
            continue
        target_net = target_by_name.get(net["name"])
        if not target_net:
            print(f"  SKIP    '{net['name']}': network not found in target org")
            continue
        print(f"\n  network '{net['name']}'")

        # New devices claimed in this target network, by name.
        new_devices = {d.get("name"): d for d in
                       dashboard.networks.getNetworkDevices(networkId=target_net["id"])
                       if d.get("model", "").startswith("MS")}

        # Schedule id map persisted by the network-settings restore.
        map_path = f"{net_dir}/schedule_idmap_org_{target_org_id}.json"
        schedule_map = load_json(map_path) if os.path.exists(map_path) else {}

        for dev_folder in sorted(os.listdir(dev_root)):
            info = load_json(f"{dev_root}/{dev_folder}/info.json")
            ports = load_json(f"{dev_root}/{dev_folder}/ports.json")
            new_dev = new_devices.get(info["name"])
            if not new_dev:
                print(f"  SKIP    switch '{info['name']}' (old {info['model']}): no device "
                      f"with this name claimed in target network yet")
                continue
            print(f"  switch '{info['name']}': old {info['model']} -> new "
                  f"{new_dev['model']} ({new_dev['serial']}), {len(ports)} port configs")

            # Carry over device tags (used for SSID availability, tag-scoped
            # admin, reporting). Older backups may not contain them.
            old_tags = info.get("tags") or []
            if old_tags and sorted(old_tags) != sorted(new_dev.get("tags") or []):
                if dry_run:
                    print(f"  would apply device tags {old_tags}")
                else:
                    try:
                        dashboard.devices.updateDevice(serial=new_dev["serial"], tags=old_tags)
                        print(f"  applied device tags {old_tags}")
                    except Exception as e:
                        print(f"  WARN    could not apply device tags: {e}")

            for port in ports:
                payload = {k: v for k, v in port.items()
                           if k not in PORT_READONLY_FIELDS}
                # Rewrite port schedule reference to the target org's ID.
                if payload.get("portScheduleId"):
                    new_sched = schedule_map.get(str(payload["portScheduleId"]))
                    if new_sched:
                        payload["portScheduleId"] = new_sched
                    else:
                        print(f"  WARN    port {port['portId']}: schedule not mapped, dropped")
                        payload.pop("portScheduleId")
                # Access policies (802.1X/RADIUS) are not migrated.
                if payload.get("accessPolicyType") not in (None, "Open"):
                    print(f"  WARN    port {port['portId']}: access policy "
                          f"'{payload.get('accessPolicyType')}' dropped — re-attach manually")
                    for k in ("accessPolicyType", "accessPolicyNumber"):
                        payload.pop(k, None)
                try:
                    if dry_run:
                        continue
                    dashboard.switch.updateDeviceSwitchPort(
                        serial=new_dev["serial"], portId=str(port["portId"]), **payload)
                except Exception as e:
                    print(f"  ERROR   '{info['name']}' port {port['portId']}: {e}")
                    failures.append({"kind": f"port {port['portId']}",
                                     "name": info["name"], "error": str(e)})
            if dry_run:
                print(f"  would apply {len(ports)} port configs to '{info['name']}'")
            else:
                print(f"  done    '{info['name']}'")

    if failures:
        print(f"\n  {len(failures)} port(s) FAILED — all others were still applied:")
        for f in failures:
            print(f"    - {f['name']}: {f['kind']}")
        if not dry_run:
            save_json(f"{backup_dir}/port_failures_org_{target_org_id}.json", failures)
    elif dry_run:
        print("\n  Dry run complete — nothing was changed.")
