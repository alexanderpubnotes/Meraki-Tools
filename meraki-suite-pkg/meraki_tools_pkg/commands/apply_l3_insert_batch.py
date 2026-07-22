"""
apply_l3_insert_batch.py — insert one L3 rule at position 1 across a BATCH of
networks, backing up each network's original ruleset first.

Built for high-blast-radius rollouts (e.g. a position-1 Deny Spamhaus across
production): do it in slices, keep a per-batch backup, verify each slice, then
proceed. Reuses the tested apply_l3_insert logic for the actual insert.

Batching is count-based over a DETERMINISTIC network order (sorted by network
id), so slices are reproducible:
    batch 1: --limit 250
    batch 2: --skip 250 --limit 250
    ...

Before writing anything, it GETs every target network's current L3 ruleset and
saves them all to one timestamped batch backup file. Roll the whole batch back
with apply l3-restore-batch.
"""

import json
from datetime import datetime

from merakicore import networks as net_mod
from merakicore import safety
from merakicore import paths
from commands import apply_l3_insert as l3


def run(dashboard, org_id, rule_spec, skip=0, limit=None,
        network_ids=None, apply=False, backup_prefix=None,
        progress_cb=None, cancel_event=None):
    # Resolve the rule (names -> this org's IDs); refuse if any missing.
    gmap, omap = l3._build_name_to_id(dashboard, org_id)
    dest_cidr, missing = l3._resolve_dest_by_name(rule_spec["dest"], gmap, omap)
    if missing:
        print(f"  REFUSING: these referenced groups/objects do not exist in org {org_id}:")
        for m in missing:
            print(f"    - {m}")
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

    # Resolve targets, sort deterministically, then take the slice.
    all_targets = net_mod.resolve_targets(dashboard, org_id, network_ids=network_ids,
                                          product_type="appliance")
    all_targets = sorted(all_targets, key=lambda n: n["id"])
    total = len(all_targets)
    end = (skip + limit) if limit is not None else total
    batch = all_targets[skip:end]

    print(f"  org {org_id}: {total} appliance network(s) total")
    print(f"  this batch: networks {skip}..{skip + len(batch) - 1} "
          f"({len(batch)} network(s))")
    print(f"  rule: {built_rule['policy']} {built_rule['comment']!r} at position 1")
    print("\n" + net_mod.confirmation_readout(batch))

    if not batch:
        print("  nothing in this slice — check --skip/--limit.")
        return None

    dry_run = not apply
    want_comment = l3._norm(built_rule["comment"])

    if dry_run:
        # Preview only: show what each would do, no backup written.
        print("\n  DRY RUN — no backup written, no changes made.")
        def action(net, is_dry):
            current = dashboard.appliance.getNetworkApplianceFirewallL3FirewallRules(
                net["id"]).get("rules", [])
            if any(l3._norm(r.get("comment")) == want_comment for r in current):
                return "unchanged", "rule already present"
            return "changed", "would insert at position 1"
        result = safety.run_write(batch, action, dry_run=True,
                                  progress_cb=progress_cb, cancel_event=cancel_event)
        result.print_summary(True)
        return result

    # APPLY: back up the whole batch FIRST, then insert.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = backup_prefix or paths.default_path("l3_batches", f"l3_batch_backup_{org_id}")
    backup_path = f"{prefix}_{stamp}.json"
    backup = {"org_id": org_id, "created": stamp, "networks": {}}
    print(f"\n  backing up {len(batch)} network(s) before changes...")
    for i, net in enumerate(batch, 1):
        if cancel_event is not None and cancel_event.is_set():
            print(f"  Cancelled during backup — stopped before network {i}/{len(batch)}. "
                  "No changes made (backup phase only).")
            return None
        try:
            rules = dashboard.appliance.getNetworkApplianceFirewallL3FirewallRules(
                net["id"]).get("rules", [])
            backup["networks"][net["id"]] = {"name": net.get("name", ""), "rules": rules}
        except Exception as e:
            print(f"    FAILED to back up {net.get('name','')} ({net['id']}): {e}")
            print("    Aborting before any changes — backup incomplete.")
            return None
        if progress_cb:
            progress_cb(i, len(batch))
    with open(backup_path, "w") as fh:
        json.dump(backup, fh, indent=2)
    print(f"  batch backup saved -> {backup_path}")
    print(f"  (roll back with:  apply l3-restore-batch --backup-file {backup_path} --apply)")

    def action(net, is_dry):
        current = dashboard.appliance.getNetworkApplianceFirewallL3FirewallRules(
            net["id"]).get("rules", [])
        if any(l3._norm(r.get("comment")) == want_comment for r in current):
            return "unchanged", "rule already present"
        body = [r for r in current if r.get("comment") != "Default rule"]
        new_rules = [built_rule] + body
        dashboard.appliance.updateNetworkApplianceFirewallL3FirewallRules(
            net["id"], rules=new_rules)
        return "changed", "inserted at position 1"

    print(f"\n  inserting into {len(batch)} network(s)...")
    result = safety.run_write(batch, action, dry_run=False,
                              progress_cb=progress_cb, cancel_event=cancel_event)
    result.print_summary(False)
    print(f"\n  batch backup for rollback: {backup_path}")
    return result
