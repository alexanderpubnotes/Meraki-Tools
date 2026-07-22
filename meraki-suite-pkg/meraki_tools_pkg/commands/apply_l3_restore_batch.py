"""
apply_l3_restore_batch.py — roll back a batch by restoring each network's
original L3 ruleset from a batch backup file (made by apply l3-insert-batch).

Restores each network's saved ruleset VERBATIM (exact reversal), rather than
trying to surgically remove the inserted rule. The API's implicit default rule
is stripped before PUT. Dry run by default.
"""

import json

from merakicore import safety


def load_backup(path):
    with open(path) as fh:
        return json.load(fh)


def run(dashboard, backup, apply=False, progress_cb=None, cancel_event=None):
    nets = backup.get("networks", {})
    if not nets:
        print("  backup file has no networks.")
        return None

    org_id = backup.get("org_id", "?")
    print(f"  batch backup from org {org_id}, created {backup.get('created','?')}")
    print(f"  {len(nets)} network(s) to restore:")
    for nid, info in list(nets.items())[:10]:
        print(f"    - {info.get('name','')} ({nid}): {len(info.get('rules',[]))} rule(s)")
    if len(nets) > 10:
        print(f"    ... and {len(nets) - 10} more")

    dry_run = not apply
    # Build a pseudo-target list for the shared write harness.
    targets = [{"id": nid, "name": info.get("name", "")} for nid, info in nets.items()]

    def action(net, is_dry):
        saved = nets[net["id"]]["rules"]
        body = [r for r in saved if r.get("comment") != "Default rule"]
        if is_dry:
            return "changed", f"would restore {len(body)} rule(s)"
        dashboard.appliance.updateNetworkApplianceFirewallL3FirewallRules(
            net["id"], rules=body)
        return "changed", f"restored {len(body)} rule(s)"

    result = safety.run_write(targets, action, dry_run=dry_run,
                              progress_cb=progress_cb, cancel_event=cancel_event)
    result.print_summary(dry_run)
    return result
